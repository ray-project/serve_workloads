import sys
import time
import json
import yaml
import asyncio
import requests
import itertools
import traceback
from pathlib import Path
from fastapi import FastAPI
from typing import Dict, Set, Optional, List

import aiohttp
from aiohttp import TraceConfig
from aiohttp_retry import RetryClient, ExponentialRetry

from chaos_test.constants import (
    RECEIVER_CONFIG_FILENAME,
    NODE_KILLER_KEY,
    DISK_LEAKER_KEY,
    KillOptions,
)
from utilities.metrics_utils import StringGauge
from utilities.deployment_utils import BaseReconfigurableDeployment

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._private.utils import run_background_task


app = FastAPI()


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0})
@serve.ingress(app)
class Router:
    def __init__(self, pinger_handles, reaper_handle, helmsman_handle):
        self.pinger_handles = pinger_handles
        self.reaper = reaper_handle
        self.helmsman = helmsman_handle

    @app.get("/")
    @app.post("/")
    def root(self) -> str:
        return "Hi there!"

    @app.get("/start")
    async def start(self):
        await self.start_pingers()
        await self.start_reaper()
        await self.start_helmsman()
        return "Started deployment loops!"

    @app.get("/start-pingers")
    async def start_pingers(self) -> str:
        for pinger_handle in self.pinger_handles:
            await (await pinger_handle.start.remote())
        return "Started Pingers!"

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
        await self.stop_pingers()
        await self.stop_reaper()
        await self.stop_helmsman()
        return "Stopped deployment loops!"

    @app.get("/stop-pingers")
    async def stop_pingers(self) -> str:
        for pinger_handle in self.pinger_handles:
            await (await pinger_handle.stop.remote())
        return "Stopped Pingers!"

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
        info = {}
        for i in range(len(self.pinger_handles)):
            info[f"pinger_{i}"] = (
                await (await self.pinger_handles[i].get_info.remote())
            ).copy()
        reaper_info = (await (await self.reaper.get_info.remote())).copy()
        helmsman_info = (await (await self.helmsman.get_info.remote())).copy()
        info.update(reaper_info)
        info.update(helmsman_info)
        return info


PINGER_OPTIONS = {
    "url": str,
    "bearer_token": str,
    "max_qps": int,
}


@serve.deployment(
    num_replicas=1,
    user_config={option: None for option in PINGER_OPTIONS.keys()},
    ray_actor_options={"num_cpus": 0},
)
class Pinger(BaseReconfigurableDeployment):
    def __init__(
        self,
        target_tag: str,
        payload: Dict,
    ):
        super().__init__(PINGER_OPTIONS)
        self.target_tag = target_tag
        self.payload = payload
        self.run_request_loop_task = None
        self._initialize_stats()
        self._initialize_metrics()
        self._request_id = 0
        self.pending_request_metadata = dict()
        self.pending_requests = set()

    def reconfigure(self, config: Dict):
        super().reconfigure(config)
        self.stop()
        self.start()

    async def run_request_loop(
        self,
        target_tag: str,
        payload: Optional[Dict] = None,
    ):
        try:
            await self._drain_requests()
            send_interval_s = 1 / self.max_qps
            metric_tags = {"class": "Pinger", "target": target_tag}

            client = self._create_http_client(metric_tags)

            qps_warning_grace_number = 1
            while True:
                start_time = asyncio.get_event_loop().time()

                self.pending_requests.add(
                    self._make_request(
                        client=client,
                        target_url=self.url,
                        target_bearer_token=self.bearer_token,
                        payload=payload,
                        tags=metric_tags,
                    )
                )

                done, pending = await asyncio.wait(self.pending_requests, timeout=0)
                self.pending_requests = pending
                self.num_pending_requests = len(self.pending_requests)
                self.pending_requests_gauge.set(
                    len(self.pending_requests), tags=metric_tags
                )

                num_successful_requests = 0
                for task in done:
                    try:
                        response = await task
                        status_code = response.status
                    except asyncio.TimeoutError as e:
                        self.request_timeout_error_counter.inc(tags=metric_tags)
                        self._count_failed_request(-2, reason="TimeoutError")
                        self.fail_counter.inc(tags=metric_tags)
                        print(
                            f"{time.strftime('%b %d -- %l:%M%p: ')}"
                            f"Got exception from request: \n{repr(e)}"
                        )
                    except Exception as e:
                        self._count_failed_request(-1, reason=repr(e))
                        self._increment_error_counter(-1, tags=metric_tags)
                        self.fail_counter.inc(tags=metric_tags)
                        print(
                            f"{time.strftime('%b %d -- %l:%M%p: ')}"
                            f"Got exception from request: \n{repr(e)}"
                        )
                    if status_code == 200:
                        num_successful_requests += 1
                    else:
                        response_text = await response.text()
                        print(
                            f"{time.strftime('%b %d -- %l:%M%p: ')}"
                            f"Got failed request: \n{response_text}"
                        )
                        self._count_failed_request(status_code, reason=response_text)
                        self.fail_counter.inc(tags=metric_tags)
                        self._increment_error_counter(status_code, tags=metric_tags)

                    if self.current_num_requests % (self.max_qps * 10) == 0:
                        print(
                            f"{time.strftime('%b %d -- %l:%M%p: ')}"
                            f"Sent {self.current_num_requests} "
                            f'requests to "{self.url}".'
                        )

                # Count successful requests in a batch for efficiency
                if num_successful_requests > 0:
                    self._count_successful_request(num_successful_requests)
                    self.success_counter.inc(
                        value=num_successful_requests, tags=metric_tags
                    )

                send_interval_remaining_s = send_interval_s - (
                    asyncio.get_event_loop().time() - start_time
                )
                if send_interval_remaining_s > 0:
                    await asyncio.sleep(send_interval_remaining_s)
                else:
                    qps_warning_grace_number -= 1
                    if qps_warning_grace_number == 0:
                        print(
                            "Warning: max_qps is too high. Request processing "
                            "time exceeded send interval by "
                            f"{send_interval_remaining_s} seconds."
                        )
                        qps_warning_grace_number = 10
        except Exception as e:
            print(
                f"{time.strftime('%b %d -- %l:%M%p: ')}"
                f"run_request_loop for target_url {self.url} crashed."
            )
            traceback.print_exc(file=sys.stdout)

    def start(self):
        if self.run_request_loop_task is not None:
            print("Called start() while Pinger is already running. Nothing changed.")
        else:
            self.run_request_loop_task = run_background_task(
                self.run_request_loop(
                    target_tag=self.target_tag,
                    payload=self.payload,
                )
            )
            print("Started Pinger. Call stop() to stop.")

    async def stop(self):
        if self.run_request_loop_task is None:
            print("Called stop() while Pinger was already stopped. Nothing changed.")
        else:
            self.run_request_loop_task.cancel()
            await self._drain_requests()
            print("Stopped Pinger. Call start() to start.")
        self._reset_current_counters()

    def get_info(self):
        info = {
            "Pinger running": self.run_request_loop_task is not None,
            "Target URL": self.url,
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
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.success_counter = Counter(
            "pinger_num_requests_succeeded",
            description="Number of successful requests.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.fail_counter = Counter(
            "pinger_num_requests_failed",
            description="Number of failed requests.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.latency_gauge = Gauge(
            "pinger_request_latency",
            description="Latency of last request.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.attempts_gauge = Gauge(
            "pinger_request_num_attempts",
            description="Number of attempts of last request.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.pending_requests_gauge = Gauge(
            "pinger_pending_requests",
            description="Number of requests being processed by the Receiver.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.http_error_counters = dict()

        for error_code in itertools.chain(range(400, 419), range(500, 512)):
            self.http_error_counters[error_code] = Counter(
                f"pinger_{error_code}_http_error",
                description=f"Number of {error_code} HTTP response errors received.",
                tag_keys=("class", "target"),
            ).set_default_tags({"class": "Pinger"})

        self.request_timeout_error_counter = Counter(
            "pinger_num_request_timeout",
            description="Number of requests that timed out after retries.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

        self.http_error_fallback_counter = Counter(
            "pinger_fallback_http_error",
            description="Number of any other errors when processing responses.",
            tag_keys=("class", "target"),
        ).set_default_tags({"class": "Pinger"})

    def _increment_error_counter(self, status_code: int, tags: Dict[str, str]):
        """Increments the error counter corresponding to the status_code."""

        if status_code in self.http_error_counters:
            self.http_error_counters[status_code].inc(tags=tags)
        else:
            self.http_error_fallback_counter.inc(tags=tags)

    def _count_successful_request(self, num_successful_requests: int):
        self.total_num_requests += num_successful_requests
        self.total_successful_requests += num_successful_requests
        self.current_num_requests += num_successful_requests
        self.current_successful_requests += num_successful_requests

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

    def _create_http_client(self, metric_tags: Dict):
        max_attempts = 5

        # From https://stackoverflow.com/a/63925153
        async def on_request_start(session, trace_config_ctx, params):
            request_id = trace_config_ctx.trace_request_ctx["request_id"]
            if self.pending_request_metadata[request_id]["num_attempts"] == 0:
                self.request_counter.inc(tags=metric_tags)
            self.pending_request_metadata[request_id]["num_attempts"] += 1
            trace_config_ctx.start = asyncio.get_event_loop().time()

        def update_request_latency(trace_config_ctx):
            request_id = trace_config_ctx.trace_request_ctx["request_id"]
            attempt_latency = asyncio.get_event_loop().time() - trace_config_ctx.start
            self.pending_request_metadata[request_id][
                "total_latency"
            ] += attempt_latency

        async def on_request_end(session, trace_config_ctx, params):
            update_request_latency(trace_config_ctx)

        async def on_request_exception(session, trace_config_ctx, params):
            update_request_latency(trace_config_ctx)

        trace_config = TraceConfig()
        trace_config.on_request_start.append(on_request_start)
        trace_config.on_request_end.append(on_request_end)
        trace_config.on_request_exception.append(on_request_exception)

        return RetryClient(
            retry_options=ExponentialRetry(
                attempts=max_attempts,
                start_timeout=1,
                factor=2,
                exceptions=[asyncio.TimeoutError, aiohttp.ServerDisconnectedError],
            ),
            trace_configs=[trace_config],
        )

    async def _drain_requests(self):
        await asyncio.gather(*self.pending_requests)
        self.pending_requests.clear()

    async def _make_request(
        self,
        client,
        target_url: str,
        target_bearer_token: str,
        payload: Dict,
        tags: Dict,
    ):
        request_id = self._get_next_request_id()
        self.pending_request_metadata[request_id] = {
            "total_latency": 0,
            "num_attempts": 0,
        }
        try:
            return await client.post(
                target_url,
                headers={"Authorization": f"Bearer {target_bearer_token}"},
                json=payload,
                timeout=10,
                trace_request_ctx={"request_id": request_id},
            )
        finally:
            self.latency_gauge.set(
                self.pending_request_metadata[request_id]["total_latency"], tags=tags
            )
            self.attempts_gauge.set(
                self.pending_request_metadata[request_id]["num_attempts"], tags=tags
            )
            del self.pending_request_metadata[request_id]

    def _get_next_request_id(self):
        """Gets the next request's ID.

        Request IDs cycle between 0 to 999,999.
        """

        self._request_id += 1
        self._request_id %= 1000000
        return self._request_id


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
class Reaper(BaseReconfigurableDeployment):
    def __init__(self):
        super().__init__(REAPER_OPTIONS)
        self.kill_loop_task = None
        self._initialize_stats()
        self.kill_options = itertools.cycle(KillOptions.kill_types())
        self.next_kill_option = next(self.kill_options)
        self._initialize_metrics()

    def reconfigure(self, config: Dict):
        super().reconfigure(config)
        self.stop()
        self.start()

    async def kill_loop(self):
        while True:
            print(
                f"Sleeping for {self.kill_interval_s} seconds before sending "
                "next kill request."
            )
            await asyncio.sleep(self.kill_interval_s)
            json_payload = {NODE_KILLER_KEY: self.next_kill_option}
            try:
                print(
                    f'Sending kill request with method "{self.next_kill_option.value}".'
                )
                requests.post(
                    self.receiver_url,
                    headers={"Authorization": f"Bearer {self.receiver_bearer_token}"},
                    json=json_payload,
                    timeout=3,
                )
            except Exception as e:
                print(f"Got following exception when sending kill request: {repr(e)}")
            self.kill_counter.inc()
            self.latest_kill_method.set(
                tags={"class": "Reaper", "method": self.next_kill_option.value}
            )
            self.next_kill_option = next(self.kill_options)
            self.current_kill_requests += 1
            self.total_kill_requests += 1

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

    def _initialize_metrics(self):
        self.kill_counter = Counter(
            "pinger_num_kill_requests_sent",
            description="Number of kill requests sent.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Reaper"})

        self.latest_kill_method = StringGauge(
            label_name="method",
            name="pinger_latest_kill_method",
            description="Latest method used to kill a Receiver node.",
            tag_keys=("class", "method"),
        ).set_default_tags({"class": "Reaper"})


RECEIVER_HELMSMAN_OPTIONS = {
    "project_id": str,
    "receiver_service_name": str,
    "receiver_service_id": str,
    "receiver_build_id": str,
    "receiver_compute_config_id": str,
    "receiver_gcs_external_storage_config": dict,
    "receiver_url": str,
    "receiver_bearer_token": str,
    "cookie": str,
    "upgrade_interval_s": float,
}


@serve.deployment(
    num_replicas=1,
    user_config={option: None for option in RECEIVER_HELMSMAN_OPTIONS},
    ray_actor_options={"num_cpus": 0},
)
class ReceiverHelmsman(BaseReconfigurableDeployment):
    def __init__(self):
        super().__init__(RECEIVER_HELMSMAN_OPTIONS)
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
        super().reconfigure(config)
        self._update_rest_api_urls()
        self.stop()
        self.start()

    async def run_status_check_loop(self):
        while True:
            self._log_receiver_status()
            await asyncio.sleep(5)

    async def run_upgrade_loop(self):
        upgrade_type_iter = itertools.cycle(["IN_PLACE"])
        while True:
            next_upgrade_type = next(upgrade_type_iter)
            print(
                f"{time.strftime('%b %d -- %l:%M%p: ')}Waiting "
                f"{self.upgrade_interval_s} seconds before "
                f"{next_upgrade_type} upgrading Receiver."
            )
            await asyncio.sleep(self.upgrade_interval_s)
            self._upgrade_receiver(next_upgrade_type)

    async def run_disk_leaker_monitoring(self):
        while True:
            try:
                json_payload = {DISK_LEAKER_KEY: "arbitrary"}
                response = requests.post(
                    self.receiver_url,
                    headers={"Authorization": f"Bearer {self.receiver_bearer_token}"},
                    json=json_payload,
                    timeout=10,
                )
                self.num_writes_to_disk = int(response.text)
                self.num_writes_to_disk_gauge.set(self.num_writes_to_disk)
            except Exception as e:
                print(f"Got exception when checking disk writes: {repr(e)}")
            await asyncio.sleep(5 * 60)

    def start(self):
        task_methods = [
            self.run_status_check_loop,
            self.run_upgrade_loop,
            self.run_disk_leaker_monitoring,
        ]
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
            "Current in-place upgrade requests": self.current_upgrade_requests,
            "Total in-place upgrade requests": self.total_upgrade_requests,
            "Number of writes to disk": self.num_writes_to_disk,
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

        self.num_writes_to_disk_gauge = Gauge(
            "pinger_num_writes_to_disk_receiver",
            description="Number of writes to disk by Receiver's DiskLeaker.",
            tag_keys=("class",),
        ).set_default_tags({"class": "Pinger"})

        # TODO (shrekris-anyscale): Make this metric track any upgrades.
        self.upgrade_counter = Counter(
            "pinger_receiver_num_upgrade_requests_sent",
            description="Number of upgrade requests sent.",
            tag_keys=("class",),
        ).set_default_tags({"class": "ReceiverHelmsman"})

        self.upgrade_failure_counter = Counter(
            "pinger_receiver_upgrade_requests_failed",
            description="Number of upgrade requests of any type that failed.",
            tag_keys=("class",),
        ).set_default_tags({"class": "ReceiverHelmsman"})

    def _initialize_stats(self):
        self.total_upgrade_requests = 0
        self.current_upgrade_requests = 0
        self.num_writes_to_disk = 0

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
        cur_dir = str(Path(__file__).parent)
        aliased_path_prefix = "/tmp/ray/session_latest/runtime_resources"
        aliased_dir = aliased_path_prefix + cur_dir.split("runtime_resources")[1]
        receiver_config_file_path = f"{aliased_dir}/{RECEIVER_CONFIG_FILENAME}"
        print(f'Using Receiver config at "{receiver_config_file_path}"')
        with open(receiver_config_file_path) as f:
            receiver_config_template = yaml.safe_load(f)

        return receiver_config_template

    def _upgrade_receiver(self, upgrade_type: str):
        try:
            self.receiver_config_template["applications"][0][
                "import_path"
            ] = self.next_receiver_import_path
            self.receiver_config_template["applications"][0]["deployments"][0][
                "ray_actor_options"
            ]["resources"] = {self.next_singleton_resource: 1}
            request_data = {
                "name": self.receiver_service_name,
                "description": "Receives the pinger's pings.",
                "project_id": self.project_id,
                "ray_serve_config": self.receiver_config_template,
                "build_id": self.receiver_build_id,
                "compute_config_id": self.receiver_compute_config_id,
                "ray_gcs_external_storage_config": self.receiver_gcs_external_storage_config,
                "rollout_strategy": upgrade_type,
            }
            print(
                f"{upgrade_type} upgrading Receiver using request data: \n"
                + json.dumps(request_data, indent=4)
            )
            response = requests.put(
                self.update_put_url,
                data=json.dumps(request_data),
                headers={"Cookie": self.cookie},
            )
            self.latest_receiver_upgrade_type = upgrade_type
            self.receiver_upgrade_gauge.set(
                {
                    "class": "ReceiverHelmsman",
                    "upgrade_type": self.latest_receiver_upgrade_type,
                }
            )
            self.latest_receiver_import_path = self.receiver_config_template[
                "applications"
            ][0]["import_path"]
            self.receiver_import_path_gauge.set(
                {
                    "class": "ReceiverHelmsman",
                    "import_path": self.latest_receiver_import_path,
                }
            )
            self.total_upgrade_requests += 1
            self.current_upgrade_requests += 1
            self.upgrade_counter.inc()
            if response.status_code in [200, 202]:
                print(
                    f"{upgrade_type} upgrade request succeeded. New import "
                    f'path: "{self.next_receiver_import_path}".'
                )
                self.next_receiver_import_path = next(self.receiver_import_paths)
                self.next_singleton_resource = next(self.receiver_singleton_resource)
            else:
                self.upgrade_failure_counter.inc()
                error_message = (
                    "Upgrading request failed.\n"
                    f"* Status code: {response.status_code}"
                )
                if "application/json" in response.headers.get("Content-Type", ""):
                    error_message += f"\n* JSON: {response.json()}"
                print(error_message)
        except Exception as e:
            print(
                f"Got exception when {upgrade_type} upgrading Receiver: " f"{repr(e)}"
            )


graph = Router.bind(
    [
        Pinger.options(name="receiver_Pinger").bind(
            target_tag="Receiver", payload={NODE_KILLER_KEY: KillOptions.SPARE}
        ),
        Pinger.options(name="self_Pinger").bind(target_tag="Pinger", payload=None),
    ],
    Reaper.bind(),
    ReceiverHelmsman.bind(),
)
