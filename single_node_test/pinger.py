import time
import asyncio
import requests
from typing import Dict
from fastapi import FastAPI

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._private.utils import run_background_task

from single_node_test.constants import ARTICLE_TEXT_KEY


# These options should get overwritten in the Serve config with a real
# authentication token and URL.
DEFAULT_BEARER_TOKEN = "default"
DEFAULT_TARGET_URL = "http://localhost:8000/"

app = FastAPI()


@serve.deployment(
    num_replicas=1,
    route_prefix="/",
    user_config={
        "target_url": DEFAULT_TARGET_URL,
        "bearer_token": DEFAULT_BEARER_TOKEN,
        "default_on": 0,  # 1 to enable, 0 to disable
    },
)
@serve.ingress(app)
class Pinger:
    def __init__(self):
        self.target_url = ""
        self.bearer_token = ""
        self.live = False
        self.total_num_requests = 0
        self.total_successful_requests = 0
        self.total_failed_requests = 0
        self.current_num_requests = 0
        self.current_successful_requests = 0
        self.current_failed_requests = 0
        self.failed_response_counts = dict()
        self.failed_response_reasons = dict()

        self.request_counter = Counter(
            "pinger_num_requests",
            description="Number of requests.",
            tag_keys=("class",),
        )
        self.request_counter.set_default_tags({"class": "Pinger"})

        self.success_counter = Counter(
            "pinger_num_requests_succeeded",
            description="Number of successful requests.",
            tag_keys=("class",),
        )
        self.success_counter.set_default_tags({"class": "Pinger"})

        self.fail_counter = Counter(
            "pinger_num_requests_failed",
            description="Number of failed requests.",
            tag_keys=("class",),
        )
        self.fail_counter.set_default_tags({"class": "Pinger"})

        self.latency_gauge = Gauge(
            "pinger_request_latency",
            description="Latency of last successful request.",
            tag_keys=("class",),
        )
        self.latency_gauge.set_default_tags({"class": "Pinger"})

        self.error404 = Counter(
            "pinger_404",
            description="Number of 404 HTTP response errors received.",
            tag_keys=("class",),
        )
        self.error404.set_default_tags({"class": "Pinger"})

        self.error500 = Counter(
            "pinger_500",
            description="Number of 500 HTTP response errors received.",
            tag_keys=("class",),
        )
        self.error500.set_default_tags({"class": "Pinger"})

        self.error502 = Counter(
            "pinger_502",
            description="Number of 502 HTTP response errors received.",
            tag_keys=("class",),
        )
        self.error502.set_default_tags({"class": "Pinger"})

        self.error_other = Counter(
            "pinger_http_response_other_error",
            description="Number of other HTTP response errors received.",
            tag_keys=("class",),
        )
        self.error_other.set_default_tags({"class": "Pinger"})

    def reconfigure(self, config: Dict):
        self.stop_requesting()

        new_target_url = config.get("target_url", DEFAULT_TARGET_URL)
        print(f'Changing target URL from "{self.target_url}" to "{new_target_url}"')
        self.target_url = new_target_url

        new_bearer_token = config.get("bearer_token", DEFAULT_BEARER_TOKEN)
        print(
            f'Changing bearer token from "{self.bearer_token}" to "{new_bearer_token}"'
        )
        self.bearer_token = new_bearer_token

        if config.get("default_on", 0) == 1:
            print("Pinger is default on.")
            self.live = True
            run_background_task(self.run_request_loop())
        else:
            print("Pinger is default off.")

    @app.get("/")
    def root(self):
        return "Hi, I'm a pinger!"

    @app.get("/start")
    async def start_requesting(self):
        if self.live:
            return "Already sending requests."
        else:
            print(f'Starting to send requests to URL "{self.target_url}"')
            self.live = True
            await self.run_request_loop()

    async def run_request_loop(self):
        while self.live:
            json_payload = self.get_json_payload()

            start_time = time.time()
            try:
                response = requests.post(
                    self.target_url,
                    headers={"Authorization": f"Bearer {self.bearer_token}"},
                    json=json_payload,
                    timeout=5,
                )
                latency = time.time() - start_time

                self.request_counter.inc()
                if response.status_code == 200:
                    self.count_successful_request()
                    self.latency_gauge.set(latency)
                    self.success_counter.inc()
                else:
                    self.count_failed_request(
                        response.status_code, reason=response.text
                    )
                    self.fail_counter.inc()
                    self.increment_error_counter(response.status_code)
                if self.current_num_requests % 3 == 0:
                    print(
                        f"{time.strftime('%b %d – %l:%M%p: ')}"
                        f"Sent {self.current_num_requests} "
                        f'requests to "{self.target_url}".'
                    )
            except Exception as e:
                self.count_failed_request(-1, reason=repr(e))
                self.fail_counter.inc()
                print(
                    f"{time.strftime('%b %d – %l:%M%p: ')}"
                    f"Got exception: \n{repr(e)}"
                )

            await asyncio.sleep(5)

    @app.get("/stop")
    def stop_requesting(self):
        print(f'Stopping requests to URL "{self.target_url}".')
        self.live = False
        self.reset_current_counters()
        return "Stopped."

    @app.get("/info")
    def get_info(self):
        info = {
            "Live": self.live,
            "Target URL": self.target_url,
            "Total number of requests": self.total_num_requests,
            "Total successful requests": self.total_successful_requests,
            "Total failed requests": self.total_failed_requests,
            "Current number of requests": self.current_num_requests,
            "Current successful requests": self.current_successful_requests,
            "Current failed requests": self.current_failed_requests,
            "Failed response counts": self.failed_response_counts,
            "Failed response reasons": self.failed_response_reasons,
        }
        return info

    def reset_current_counters(self):
        self.current_num_requests = 0
        self.current_successful_requests = 0
        self.current_failed_requests = 0

    def count_successful_request(self):
        self.total_num_requests += 1
        self.total_successful_requests += 1
        self.current_num_requests += 1
        self.current_successful_requests += 1

    def count_failed_request(self, status_code: int, reason: str = ""):
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
            self.failed_response_reasons[status_code] = set(reason)

    def increment_error_counter(self, status_code: int):
        """Increments the error counter corresponding to the status_code."""

        counters = {
            404: self.error404,
            500: self.error500,
            502: self.error502,
        }

        if status_code in counters:
            counters[status_code].inc()
        else:
            self.error_other.inc()

    def get_json_payload(self) -> Dict:
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
