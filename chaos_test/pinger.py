import time
import json
import yaml
import asyncio
import requests
import itertools
from typing import Dict
from pathlib import Path
from fastapi import FastAPI
from aiohttp import TraceConfig
from aiohttp_retry import RetryClient, ExponentialRetry

from chaos_test.constants import (
    RECEIVER_CONFIG_FILENAME,
    RECEIVER_KILL_KEY,
    KillOptions,
)
from chaos_test.metrics_utils import StringGauge

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._private.utils import run_background_task


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
class Pinger:
    def __init__(self):
        self.config_options = PINGER_OPTIONS
        self.request_loop_task = None
        self._initialize_stats()
        self._initialize_metrics()
        self.client = self._create_http_client()
        self.pending_requests = set()

    def reconfigure(self, config: Dict):
        for option, type_cast in self.config_options.items():
            new_value = type_cast(config.get(option))
            if hasattr(self, option) and getattr(self, option) != new_value:
                print(
                    f'Changing {option} from "{getattr(self, option)}" to "{new_value}"'
                )
            else:
                print(f'Initializing {option} to "{new_value}"')
            setattr(self, option, new_value)
        self.stop()
        self.start()

    async def run_request_loop(self):
        await self._drain_requests()
        send_interval_s = 1 / self.max_qps

        while True:
            json_payload = {RECEIVER_KILL_KEY: KillOptions.SPARE}

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
                if self.current_num_requests % (self.max_qps * 10) == 0:
                    print(
                        f"{time.strftime('%b %d – %l:%M%p: ')}"
                        f"Sent {self.current_num_requests} "
                        f'requests to "{self.receiver_url}".'
                    )

            send_interval_remaining_s = send_interval_s - (
                asyncio.get_event_loop().time() - start_time
            )
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
                start_timeout=0.1,
                factor=2,
                exceptions=[asyncio.TimeoutError],
            ),
            trace_configs=[trace_config],
        )

    async def _drain_requests(self):
        await asyncio.gather(*self.pending_requests)
        self.pending_requests.clear()


REAPER_OPTIONS = {
    "receiver_url": str,
    "receiver_bearer_token": str,
    "kill_interval_s": float,
}


@serve.deployment(
    num_replicas=1,
    user_config={option: None for option in REAPER_OPTIONS.keys()},
    ray_actor_options={"num_cpus": 0},
)
class Reaper:
    def __init__(self):
        self.config_options = REAPER_OPTIONS
        self.kill_loop_task = None
        self._initialize_stats()
        self.kill_counter = Counter(
            "pinger_num_kill_requests_sent",
            description="Number of kill requests sent.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Reaper"})

    def reconfigure(self, config: Dict):
        for option, type_cast in self.config_options.items():
            new_value = type_cast(config.get(option))
            if hasattr(self, option) and getattr(self, option) != new_value:
                print(
                    f'Changing {option} from "{getattr(self, option)}" to "{new_value}"'
                )
            else:
                print(f'Initializing {option} to "{new_value}"')
            setattr(self, option, new_value)
        self.stop()
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
                    headers={"Authorization": f"Bearer {self.receiver_bearer_token}"},
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


RECEIVER_HELMSMAN_OPTIONS = {
    "project_id": str,
    "receiver_service_name": str,
    "receiver_service_id": str,
    "receiver_build_id": str,
    "receiver_compute_config_id": str,
    "receiver_gcs_external_storage_config": dict,
    "cookie": str,
    "upgrade_interval_s": float,
}


@serve.deployment(
    num_replicas=1,
    user_config={option: None for option in RECEIVER_HELMSMAN_OPTIONS},
    ray_actor_options={"num_cpus": 0},
)
class ReceiverHelmsman:
    def __init__(self):
        self.config_options = RECEIVER_HELMSMAN_OPTIONS
        self.tasks = []
        self._initialize_metrics()
        self._initialize_stats()
        self.receiver_config_template = self._parse_receiver_config_template()
        self.receiver_import_paths = itertools.cycle(
            ["chaos_test.receiver:beta", "chaos_test.receiver:alpha"]
        )
        self.receiver_singleton_resource = itertools.cycle(
            ["beta_singleton", "alpha_singleton"]
        )
        self.next_receiver_import_path = next(self.receiver_import_paths)
        self.next_singleton_resource = next(self.receiver_singleton_resource)
        self.latest_receiver_import_path = None
        self.latest_receiver_status = None
        self.latest_receiver_upgrade_type = None

    def reconfigure(self, config: Dict):
        for option, type_cast in self.config_options.items():
            new_value = type_cast(config.get(option))
            if hasattr(self, option) and getattr(self, option) != new_value:
                print(
                    f'Changing {option} from "{getattr(self, option)}" to "{new_value}"'
                )
            else:
                print(f'Initializing {option} to "{new_value}"')
            setattr(self, option, new_value)
        self._update_rest_api_urls()
        self.stop()
        self.start()

    async def run_status_check_loop(self):
        while True:
            self._log_receiver_status()
            await asyncio.sleep(5)

    async def run_upgrade_loop(self):
        while True:
            print(
                f"Waiting {self.upgrade_interval_s} seconds before upgrading Receiver."
            )
            await asyncio.sleep(self.upgrade_interval_s)
            self._in_place_update_receiver()

    def start(self):
        task_methods = [self.run_status_check_loop, self.run_upgrade_loop]
        if len(self.tasks) > 0:
            print(
                "Called start() while ReceiverHelmsman is already running. Nothing changed."
            )
        else:
            for method in task_methods:
                self.tasks.append(run_background_task(method()))
            print("Started ReceiverHelmsman. Call stop() to stop.")

    def stop(self):
        if len(self.tasks) == 0:
            print(
                "Called stop() while ReceiverHelmsman was already stopped. Nothing changed."
            )
        else:
            for task in self.tasks:
                task.cancel()

            self.tasks.clear()
            print("Stopped ReceiverHelmsman. Call start() to start.")

    def get_info(self):
        return {
            "Receiver status": self.latest_receiver_status,
            "Latest upgrade type": self.latest_receiver_upgrade_type,
            "Latest Receiver import path": self.latest_receiver_import_path,
            "Current number of in-place upgrade requests": self.current_in_place_upgrade_requests,
            "Total number of in-place upgrade requests": self.total_in_place_upgrade_requests,
        }

    def _initialize_metrics(self):
        self.receiver_status_gauge = StringGauge(
            label_name="status",
            name="pinger_receiver_status",
            description="Status of the Receiver service.",
            tag_keys=("class", "status"),
        ).set_default_tags({"class": "ReceiverHelmsman"})

        self.receiver_import_path_gauge = StringGauge(
            label_name="import_path",
            name="pinger_latest_receiver_import_path",
            description="Import path of latest config sent to Receiver service.",
            tag_keys=("class", "import_path"),
        ).set_default_tags({"class": "ReceiverHelmsman"})

        self.receiver_upgrade_gauge = StringGauge(
            label_name="upgrade_type",
            name="pinger_latest_receiver_upgrade_type",
            description="Latest type of upgrade issued to Receiver service.",
            tag_keys=("class", "upgrade_type"),
        ).set_default_tags({"class": "ReceiverHelmsman"})

    def _initialize_stats(self):
        self.total_in_place_upgrade_requests = 0
        self.current_in_place_upgrade_requests = 0

    def _update_rest_api_urls(self):
        self.status_get_url = f"https://console.anyscale-staging.com/api/v2/services-v2/{self.receiver_service_id}"
        self.update_put_url = (
            "https://console.anyscale-staging.com/api/v2/services-v2/apply"
        )

    def _log_receiver_status(self):
        try:
            response = requests.get(
                self.status_get_url, headers={"Cookie": self.cookie}
            )
            receiver_status = response.json()["result"]["current_state"]
            self.receiver_status_gauge.set(
                tags={"class": "ReceiverHelmsman", "status": receiver_status}
            )
            self.latest_receiver_status = receiver_status
        except Exception as e:
            print(f"Got exception when getting Receiver service's status: {repr(e)}")

    def _parse_receiver_config_template(self) -> Dict:
        cur_dir = Path(__file__).parent
        with open(f"{cur_dir}/{RECEIVER_CONFIG_FILENAME}") as f:
            receiver_config_template = yaml.safe_load(f)

        return receiver_config_template

    def _in_place_update_receiver(self):
        try:
            self.receiver_config_template[
                "import_path"
            ] = self.next_receiver_import_path
            self.receiver_config_template["deployments"][1]["ray_actor_options"][
                "resources"
            ] = {self.next_singleton_resource: 1}
            self.next_receiver_import_path = next(self.receiver_import_paths)
            self.next_singleton_resource = next(self.receiver_singleton_resource)
            request_data = {
                "name": self.receiver_service_name,
                "description": "Receives the pinger's pings.",
                "project_id": self.project_id,
                "ray_serve_config": self.receiver_config_template,
                "build_id": self.receiver_build_id,
                "compute_config_id": self.receiver_compute_config_id,
                "ray_gcs_external_storage_config": self.receiver_gcs_external_storage_config,
                "rollout_strategy": "IN_PLACE",
            }
            print(
                f"In-place upgrading Receiver to import path "
                f""""{self.receiver_config_template['import_path']}"."""
            )
            response = requests.put(
                self.update_put_url,
                data=json.dumps(request_data),
                headers={"Cookie": self.cookie},
            )
            self.latest_receiver_upgrade_type = "IN_PLACE"
            self.receiver_upgrade_gauge.set(
                {
                    "class": "ReceiverHelmsman",
                    "upgrade_type": self.latest_receiver_upgrade_type,
                }
            )
            self.latest_receiver_import_path = self.receiver_config_template[
                "import_path"
            ]
            self.receiver_import_path_gauge.set(
                {
                    "class": "ReceiverHelmsman",
                    "import_path": self.latest_receiver_import_path,
                }
            )
            self.total_in_place_upgrade_requests += 1
            self.current_in_place_upgrade_requests += 1
            response.raise_for_status()
            print(
                f"Finished in-place upgrading Receiver to import path "
                f""""{self.receiver_config_template['import_path']}"."""
            )
        except Exception as e:
            print(f"Got exception when in-place upgrading Receiver: {repr(e)}")


graph = Router.bind(Pinger.bind(), Reaper.bind(), ReceiverHelmsman.bind())
