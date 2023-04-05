import time
import asyncio
import requests
import itertools
from typing import Dict
from fastapi import FastAPI
from aiohttp import TraceConfig
from aiohttp_retry import RetryClient, ExponentialRetry

from chaos_test.constants import RECEIVER_KILL_KEY, KillOptions

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._private.utils import run_background_task


CONFIG_DEFAULTS = {
    "receiver_url": "http://localhost:8000/",
    "receiver_service_name": "default",
    "receiver_service_id": "default",
    "bearer_token": "default",
    "cookie": "",
    "kill_interval_s": 300,  # in seconds
    "max_qps": 100,
    "update_interval_s": 18000,  # in seconds
}


app = FastAPI()


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0})
@serve.ingress(app)
class Router:
    def __init__(self, pinger_handle, reaper_handle, helmsman_handle):
        self.pinger = pinger_handle
        self.reaper = reaper_handle
        self.helmsman = helmsman_handle

    @app.get("/")
    def root(self) -> str:
        return "Hi there!"

    @app.get("/start")
    async def start(self):
        await self.start_pinger()
        await self.start_reaper()
        await self.start_helmsman()
        return "Started deployment loops!"

    @app.get("/start-pinger")
    async def start_pinger(self) -> str:
        await (await self.pinger.start.remote())
        return "Started Pinger!"

    @app.get("/start-reaper")
    async def start_reaper(self) -> str:
        await (await self.reaper.start.remote())
        return "Started Reaper!"

    @app.get("/start-helmsman")
    async def start_helmsman(self) -> str:
        await (await self.helmsman.start.remote())
        return "Started Helmsman!"

    @app.get("/stop")
    async def stop(self):
        await self.stop_pinger()
        await self.stop_reaper()
        await self.stop_helmsman()
        return "Stopped deployment loops!"

    @app.get("/stop-pinger")
    async def stop_pinger(self) -> str:
        await (await self.pinger.stop.remote())
        return "Stopped Pinger!"

    @app.get("/stop-reaper")
    async def stop_reaper(self) -> str:
        await (await self.reaper.stop.remote())
        return "Stopped Reaper!"

    @app.get("/stop-helmsman")
    async def stop_helmsman(self) -> str:
        await (await self.helmsman.stop.remote())
        return "Stopped Helmsman!"

    @app.get("/info")
    async def get_info(self):
        pinger_info = (await (await self.pinger.get_info.remote())).copy()
        reaper_info = (await (await self.reaper.get_info.remote())).copy()
        helmsman_info = (await (await self.helmsman.get_info.remote())).copy()
        pinger_info.update(reaper_info)
        pinger_info.update(helmsman_info)
        return pinger_info


@serve.deployment(
    num_replicas=1,
    user_config={
        "receiver_url": CONFIG_DEFAULTS["receiver_url"],
        "bearer_token": CONFIG_DEFAULTS["bearer_token"],
        "max_qps": CONFIG_DEFAULTS["max_qps"],
    },
    ray_actor_options={"num_cpus": 0},
)
class Pinger:
    def __init__(self):
        self.receiver_url = ""
        self.bearer_token = ""
        self.max_qps = 0
        self.request_loop_task = None
        self._initialize_stats()
        self._initialize_metrics()
        self.client = self._create_http_client()
        self.pending_requests = set()

    def reconfigure(self, config: Dict):
        config_variables = ["receiver_url", "bearer_token", "max_qps"]

        for var in config_variables:
            new_value = config.get(var, CONFIG_DEFAULTS[var])
            print(f'Changing {var} from "{getattr(self, var)}" to "{new_value}"')
            setattr(self, var, new_value)

        self.start()

    async def run_request_loop(self):
        await self._drain_requests()
        send_interval_s = 1 / self.max_qps

        while True:
            json_payload = {RECEIVER_KILL_KEY: KillOptions.SPARE}

            start_time = time.time()

            self.pending_requests.add(
                self.client.post(
                    self.receiver_url,
                    headers={"Authorization": f"Bearer {self.bearer_token}"},
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
                    print(
                        f"{time.strftime('%b %d – %l:%M%p: ')}"
                        f"Got exception: \n{repr(e)}"
                    )
                except Exception as e:
                    self._count_failed_request(-1, reason=repr(e))
                    self._increment_error_counter(-1)
                    self.fail_counter.inc()
                    print(
                        f"{time.strftime('%b %d – %l:%M%p: ')}"
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
                if self.current_num_requests % 100 == 0:
                    print(
                        f"{time.strftime('%b %d – %l:%M%p: ')}"
                        f"Sent {self.current_num_requests} "
                        f'requests to "{self.receiver_url}".'
                    )

            send_interval_remaining_s = send_interval_s - (time.time() - start_time)
            if send_interval_remaining_s > 0:
                await asyncio.sleep(send_interval_remaining_s)

    def start(self):
        if self.request_loop_task is not None:
            print("Called start() while Pinger is already running. Nothing changed.")
        else:
            self.request_loop_task = run_background_task(self.run_request_loop())
            print("Started Pinger. Call stop() to stop.")

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
        self.current_kill_requests = 0
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
            retry_options=ExponentialRetry(attempts=5, start_timeout=0.1, factor=2),
            trace_configs=[trace_config],
        )

    async def _drain_requests(self):
        await asyncio.gather(*self.pending_requests)
        self.pending_requests.clear()


@serve.deployment(
    num_replicas=1,
    user_config={
        "receiver_url": CONFIG_DEFAULTS["receiver_url"],
        "bearer_token": CONFIG_DEFAULTS["bearer_token"],
        "kill_interval_s": CONFIG_DEFAULTS["kill_interval_s"],
    },
    ray_actor_options={"num_cpus": 0},
)
class Reaper:
    def __init__(self):
        self.receiver_url = ""
        self.bearer_token = ""
        self.kill_interval_s = float("inf")
        self.kill_loop_task = None
        self._initialize_stats()
        self.kill_counter = Counter(
            "pinger_num_kill_requests_sent",
            description="Number of kill requests sent.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Reaper"})

    def reconfigure(self, config: Dict):
        config_variables = ["receiver_url", "bearer_token", "kill_interval_s"]

        for var in config_variables:
            new_value = config.get(var, CONFIG_DEFAULTS[var])
            print(f'Changing {var} from "{getattr(self, var)}" to "{new_value}"')
            setattr(self, var, new_value)

        self.start()

    async def kill_loop(self):
        while True:
            print(
                f"Sleeping for {self.kill_interval_s} seconds before sending "
                "next kill request."
            )
            await asyncio.sleep(self.kill_interval_s)
            json_payload = {RECEIVER_KILL_KEY: KillOptions.KILL}
            try:
                print("Sending kill request.")
                requests.post(
                    self.receiver_url,
                    headers={"Authorization": f"Bearer {self.bearer_token}"},
                    json=json_payload,
                    timeout=3,
                )
            except Exception as e:
                print(
                    "Got following exception when sending kill " f"request: {repr(e)}"
                )
            self.kill_counter.inc()

    def start(self):
        if self.kill_loop_task is not None:
            print("Called start() while Reaper is already running. Nothing changed.")
        else:
            self.kill_loop_task = run_background_task(self.kill_loop())
            print("Started Reaper. Call stop() to stop.")

    def stop(self):
        if self.kill_loop_task is not None:
            self.kill_loop_task.cancel()
            self.kill_loop_task = None
            print("Stopped Reaper. Call start() to start.")
        else:
            print("Called stop() while Reaper was already stopped. Nothing changed.")
        self.current_kill_requests = 0

    def get_info(self) -> Dict[str, int]:
        return {
            "Reaper running": self.kill_loop_task is not None,
            "Total kill requests": self.total_kill_requests,
            "Current kill requests": self.current_kill_requests,
        }

    def _initialize_stats(self):
        self.total_kill_requests = 0
        self.current_kill_requests = 0


@serve.deployment(
    num_replicas=1,
    user_config={
        "receiver_service_name": CONFIG_DEFAULTS["receiver_service_name"],
        "receiver_url": CONFIG_DEFAULTS["receiver_url"],
        "bearer_toker": CONFIG_DEFAULTS["bearer_token"],
        "receiver_service_id": CONFIG_DEFAULTS["receiver_service_id"],
        "cookie": CONFIG_DEFAULTS["cookie"],
        "update_interval_s": CONFIG_DEFAULTS["update_interval_s"],
    },
    ray_actor_options={"num_cpus": 0},
)
class ReceiverHelmsman:
    def __init__(self):
        self.receiver_service_name = ""
        self.receiver_url = ""
        self.bearer_token = ""
        self.receiver_service_id = ""
        self.cookie = ""
        self.update_interval_s = 0
        self.manage_loop_task = None
        self._initialize_metrics()
        self.latest_receiver_status = None

    def reconfigure(self, config: Dict):
        config_variables = [
            "receiver_service_name",
            "receiver_url",
            "bearer_token",
            "receiver_service_id",
            "cookie",
            "update_interval_s",
        ]

        for var in config_variables:
            new_value = config.get(var, CONFIG_DEFAULTS[var])
            print(f'Changing {var} from "{getattr(self, var)}" to "{new_value}"')
            setattr(self, var, new_value)

        self.start()

    async def run_manage_loop(self):
        while True:
            self._log_receiver_status()
            await asyncio.sleep(5)

    def start(self):
        if self.manage_loop_task is not None:
            print(
                "Called start() while ReceiverHelmsman is already running. Nothing changed."
            )
        else:
            self.manage_loop_task = run_background_task(self.run_manage_loop())
            print("Started ReceiverHelmsman. Call stop() to stop.")

    def stop(self):
        if self.manage_loop_task is not None:
            self.manage_loop_task.cancel()
            self.manage_loop_task = None
            print("Stopped ReceiverHelmsman. Call start() to start.")
        else:
            print(
                "Called stop() while ReceiverHelmsman was already stopped. Nothing changed."
            )
        self.current_kill_requests = 0

    def get_info(self):
        return {"Receiver status": self.latest_receiver_status}

    def _initialize_metrics(self):
        self.receiver_status_gauge = Gauge(
            "pinger_receiver_status",
            description="Status of the Receiver service.",
            tag_keys=("class", "status"),
        ).set_default_tags({"class": "ReceiverHelmsman"})

    def _log_receiver_status(self):
        get_url = f"https://console.anyscale-staging.com/api/v2/services-v2/{self.receiver_service_id}"
        try:
            response = requests.get(get_url, headers={"Cookie": self.cookie})
            receiver_status = response.json()["result"]["current_state"]
            if (
                self.latest_receiver_status is not None
                and self.latest_receiver_status != receiver_status
            ):
                self.receiver_status_gauge.set(
                    0,
                    tags={
                        "class": "ReceiverHelmsman",
                        "status": self.latest_receiver_status,
                    },
                )
            self.receiver_status_gauge.set(
                1, tags={"class": "ReceiverHelmsman", "status": receiver_status}
            )
            self.latest_receiver_status = receiver_status
        except Exception as e:
            print(f"Got exception when getting Receiver service's status: {repr(e)}")


graph = Router.bind(Pinger.bind(), Reaper.bind(), ReceiverHelmsman.bind())
