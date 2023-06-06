import time
import asyncio
import aiohttp
import itertools
from typing import Dict
from fastapi import FastAPI
from aiohttp import TraceConfig
from aiohttp_retry import RetryClient, ExponentialRetry

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._private.utils import run_background_task

from utilities.deployment_utils import BaseReconfigurableDeployment
from single_node_test.constants import ARTICLE_TEXT_KEY


app = FastAPI()


PINGER_OPTIONS = {
    "receiver_url": str,
    "receiver_bearer_token": str,
    "max_qps": int,
}


@serve.deployment(
    num_replicas=1,
    user_config={option: None for option in PINGER_OPTIONS.keys()},
    ray_actor_options={"num_cpus": 0},
)
@serve.ingress(app)
class Pinger(BaseReconfigurableDeployment):
    def __init__(self):
        super().__init__(PINGER_OPTIONS)
        self.request_loop_task = None
        self._initialize_stats()
        self._initialize_metrics()
        self.client = self._create_http_client()
        self.pending_requests = set()

    def reconfigure(self, config: Dict):
        super().reconfigure(config)
        self.stop()
        self.start()

    async def run_request_loop(self):
        await self._drain_requests()
        send_interval_s = 1 / self.max_qps

        while True:
            json_payload = self._get_json_payload()

            start_time = asyncio.get_event_loop().time()

            self.pending_requests.add(
                self.client.post(
                    self.receiver_url,
                    headers={"Authorization": f"Bearer {self.receiver_bearer_token}"},
                    json=json_payload,
                    timeout=10,
                )
            )

            # Spend half the send interval waiting for pending requests.
            # Spend the rest on processing the responses.
            done, pending = await asyncio.wait(
                self.pending_requests, timeout=send_interval_s / 2
            )
            self.pending_requests = pending
            self.num_pending_requests = len(self.pending_requests)
            self.pending_requests_gauge.set(len(self.pending_requests))

            for task in done:
                try:
                    response = await task
                    status_code = response.status
                except asyncio.TimeoutError as e:
                    self.request_timeout_error_counter.inc()
                    self._count_failed_request(-2, reason="TimeoutError")
                    self.fail_counter.inc()
                    print(
                        f"{time.strftime('%b %d -- %l:%M%p: ')}"
                        f"Got exception: \n{repr(e)}"
                    )
                except Exception as e:
                    self._count_failed_request(-1, reason=repr(e))
                    self._increment_error_counter(-1)
                    self.fail_counter.inc()
                    print(
                        f"{time.strftime('%b %d -- %l:%M%p: ')}"
                        f"Got exception: \n{repr(e)}"
                    )
                else:
                    if status_code == 200:
                        self._count_successful_request()
                        self.success_counter.inc()
                    else:
                        self._count_failed_request(
                            status_code, reason=(await response.text())
                        )
                        self.fail_counter.inc()
                        self._increment_error_counter(status_code)
                if self.current_num_requests % (self.max_qps * 10) == 0:
                    print(
                        f"{time.strftime('%b %d -- %l:%M%p: ')}"
                        f"Sent {self.current_num_requests} "
                        f'requests to "{self.receiver_url}".'
                    )

            send_interval_remaining_s = send_interval_s - (
                asyncio.get_event_loop().time() - start_time
            )
            if send_interval_remaining_s > 0:
                await asyncio.sleep(send_interval_remaining_s)

    @app.get("/")
    def root(self) -> str:
        return "Hi there!"

    @app.get("/start")
    def start(self):
        if self.request_loop_task is not None:
            print("Called start() while Pinger is already running. Nothing changed.")
        else:
            self.request_loop_task = run_background_task(self.run_request_loop())
            print("Started Pinger. Call stop() to stop.")

    @app.get("/stop")
    async def stop(self):
        if self.request_loop_task is not None:
            self.request_loop_task.cancel()
            self.request_loop_task = None
            await self._drain_requests()
            print("Stopped Pinger. Call start() to start.")
        else:
            print("Called stop() while Pinger was already stopped. Nothing changed.")
        self._reset_current_counters()

    def get_info(self):
        info = {
            "Pinger running": self.request_loop_task is not None,
            "Target URL": self.receiver_url,
            "Total number of requests": self.total_num_requests,
            "Total successful requests": self.total_successful_requests,
            "Total failed requests": self.total_failed_requests,
            "Current number of requests": self.current_num_requests,
            "Current successful requests": self.current_successful_requests,
            "Current failed requests": self.current_failed_requests,
            "Number of pending requests": self.num_pending_requests,
            "Failed response counts": self.failed_response_counts,
            "Failed response reasons": self.failed_response_reasons,
        }
        return info

    def _initialize_stats(self):
        self.total_num_requests = 0
        self.total_successful_requests = 0
        self.total_failed_requests = 0
        self.current_num_requests = 0
        self.current_successful_requests = 0
        self.current_failed_requests = 0
        self.num_pending_requests = 0
        self.failed_response_counts = dict()
        self.failed_response_reasons = dict()

    def _reset_current_counters(self):
        self.current_num_requests = 0
        self.current_successful_requests = 0
        self.current_failed_requests = 0
        self.num_pending_requests = 0

    def _initialize_metrics(self):
        self.request_counter = Counter(
            "pinger_num_requests",
            description="Number of requests.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.success_counter = Counter(
            "pinger_num_requests_succeeded",
            description="Number of successful requests.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.fail_counter = Counter(
            "pinger_num_requests_failed",
            description="Number of failed requests.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.latency_gauge = Gauge(
            "pinger_request_latency",
            description="Latency of last successful request.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.pending_requests_gauge = Gauge(
            "pinger_pending_requests",
            description="Number of requests being processed by the Receiver.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.http_error_counters = dict()

        for error_code in itertools.chain(range(400, 419), range(500, 512)):
            self.http_error_counters[error_code] = Counter(
                f"pinger_{error_code}_http_error",
                description=f"Number of {error_code} HTTP response errors received.",
                tag_keys=("class",),
            ).set_default_tags({"class": "Pinger"})

        self.request_timeout_error_counter = Counter(
            "pinger_num_request_timeout",
            description="Number of requests that timed out after retries.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        self.http_error_fallback_counter = Counter(
            "pinger_fallback_http_error",
            description="Number of any other errors when processing responses.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

    def _increment_error_counter(self, status_code: int):
        """Increments the error counter corresponding to the status_code."""

        if status_code in self.http_error_counters:
            self.http_error_counters[status_code].inc()
        else:
            self.http_error_fallback_counter.inc()

    def _count_successful_request(self):
        self.total_num_requests += 1
        self.total_successful_requests += 1
        self.current_num_requests += 1
        self.current_successful_requests += 1

    def _count_failed_request(self, status_code: int, reason: str = ""):
        self.total_num_requests += 1
        self.total_failed_requests += 1
        self.current_num_requests += 1
        self.current_failed_requests += 1
        self.failed_response_counts[status_code] = (
            self.failed_response_counts.get(status_code, 0) + 1
        )
        if status_code in self.failed_response_reasons:
            self.failed_response_reasons[status_code].add(reason)
        else:
            self.failed_response_reasons[status_code] = set([reason])

    def _create_http_client(self):

        # From https://stackoverflow.com/a/63925153
        async def on_request_start(session, trace_config_ctx, params):
            self.request_counter.inc()
            trace_config_ctx.start = asyncio.get_event_loop().time()

        async def on_request_end(session, trace_config_ctx, params):
            latency = asyncio.get_event_loop().time() - trace_config_ctx.start
            self.latency_gauge.set(latency)

        trace_config = TraceConfig()
        trace_config.on_request_start.append(on_request_start)
        trace_config.on_request_end.append(on_request_end)

        return RetryClient(
            retry_options=ExponentialRetry(
                attempts=5,
                start_timeout=1,
                factor=2,
                exceptions=[asyncio.TimeoutError, aiohttp.ServerDisconnectedError],
            ),
            trace_configs=[trace_config],
        )

    async def _drain_requests(self):
        await asyncio.gather(*self.pending_requests)
        self.pending_requests.clear()

    def _get_json_payload(self) -> Dict:
        article_text = (
            "HOUSTON -- Men have landed and walked on the moon. "
            "Two Americans, astronauts of Apollo 11, steered their fragile "
            "four-legged lunar module safely and smoothly to the historic "
            "landing yesterday at 4:17:40 P.M., Eastern daylight time. Neil "
            "A. Armstrong, the 38-year-old commander, radioed to earth and "
            'the mission control room here: "Houston, Tranquility Base '
            'here. The Eagle has landed." The first men to reach the moon '
            "-- Armstrong and his co-pilot, Col. Edwin E. Aldrin Jr. of the "
            "Air Force -- brought their ship to rest on a level, "
            "rock-strewn plain near the southwestern shore of the arid "
            "Sea of Tranquility. About six and a half hours later, "
            "Armstrong opened the landing craft's hatch, stepped slowly "
            "down the ladder and declared as he planted the first human "
            "footprint on the lunar crust: \"That's one small step for "
            'man, one giant leap for mankind." His first step on the moon '
            "came at 10:56:20 P.M., as a television camera outside the "
            "craft transmitted his every move to an awed and excited "
            "audience of hundreds of millions of people on earth."
        )

        return {ARTICLE_TEXT_KEY: article_text}


graph = Pinger.bind()
