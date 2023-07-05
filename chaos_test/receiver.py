import os
import json
import time
import asyncio
import logging
import subprocess
from datetime import datetime
from starlette.requests import Request

from chaos_test.constants import NODE_KILLER_KEY, DISK_LEAKER_KEY, KillOptions

import ray
from ray import serve
from ray._private.utils import run_background_task
from ray.experimental.state.api import list_actors

logger = logging.getLogger("ray.serve")


@serve.deployment(
    num_replicas=3,
    ray_actor_options={
        "num_cpus": 0,
        # There should be 1 node_singleton per node to ensure each node has
        # 1 replica.
        "resources": {
            "node_singleton": 1,
        },
    },
)
class Receiver:
    def __init__(self, name, node_killer_handle, disk_leaker_handle):
        self.name = name
        self.node_killer_handle = node_killer_handle
        self.disk_leaker_handle = disk_leaker_handle
        print(
            f"Receiver actor starting on node {ray.get_runtime_context().get_node_id()}"
        )

    async def __call__(self, request: Request):
        request_json = await request.json()
        if DISK_LEAKER_KEY in request_json:
            print("Received disk leaker info request.")
            return await (await self.disk_leaker_handle.info.remote())
        kill_node = request_json.get(NODE_KILLER_KEY, KillOptions.SPARE)
        if kill_node == KillOptions.RAY_STOP:
            print("Received ray stop request. Attempting to kill a node.")
            await asyncio.wait(
                [
                    asyncio.wait(
                        [self.node_killer_handle.ray_stop_node.remote()], timeout=10
                    )
                ],
                timeout=10,
            )
        elif kill_node == KillOptions.NODE_HALT:
            print("Received node halt request. Attempting to kill a node.")
            await asyncio.wait(
                [
                    asyncio.wait(
                        [self.node_killer_handle.halt_node.remote()], timeout=10
                    )
                ],
                timeout=10,
            )
        return f"(PID: {os.getpid()}) {self.name} Receiver running!"


@serve.deployment(
    num_replicas=1,
    ray_actor_options={
        "num_cpus": 0,
        "resources": {
            "node_singleton": 1,
        },
    },
)
class NodeKiller:
    def ray_stop_node(self):
        try:
            actors = list_actors(filters=[("state", "=", "ALIVE")], timeout=3)
            print(f"Actor summary:\n{json.dumps(actors, indent=4)}")
        except Exception as e:
            print(f"Failed to get actor info. Got exception\n{e}")
        print(f"Killing node {ray.get_runtime_context().get_node_id()}")
        subprocess.call(["ray", "stop", "-f"])
        return ""

    def halt_node(self):
        try:
            actors = list_actors(filters=[("state", "=", "ALIVE")], timeout=3)
            print(f"Actor summary:\n{json.dumps(actors, indent=4)}")
        except Exception as e:
            print(f"Failed to get actor info. Got exception\n{e}")
        print(f"Killing node {ray.get_runtime_context().get_node_id()}")
        subprocess.call(["sudo", "halt", "--force"])
        return ""


@serve.deployment(
    num_replicas=1,
    ray_actor_options={
        "num_cpus": 0,
        "resources": {
            "leak_singleton": 1,
        },
    },
)
class DiskLeaker:
    def __init__(self):
        self.leak_dir = "/tmp/disk_leaker_files/"
        self.leak_file_name = "leak_file.log"
        self.num_writes_to_disk = 0
        os.makedirs(self.leak_dir, exist_ok=True)
        run_background_task(self.leak())

    def info(self):
        return self.num_writes_to_disk

    async def write_file(self):
        """Writes 10 GB of data over a period of time."""

        num_GB, GB = 10, (1024 * 1024 * 1024)
        time_period_m = 15

        for _ in range(time_period_m):
            write_start_time = time.time()
            print(
                f"{time.strftime('%b %d -- %l:%M%p: ')}Writing roughly "
                f"{num_GB / time_period_m}GB to log."
            )
            num_chars_to_write = int((num_GB / time_period_m) * GB)
            logger.info("0" * num_chars_to_write)
            write_duration_s = time.time() - write_start_time
            await asyncio.sleep(max(0, 60 - write_duration_s))

    async def leak(self):
        num_hours, hours = 0.5, 60 * 60
        while True:
            file_write_start_time = time.time()
            await self.write_file()
            file_write_duration_s = time.time() - file_write_start_time
            self.num_writes_to_disk += 1
            sleep_time = max(0, num_hours * hours - file_write_duration_s)
            print(f"Waiting {(sleep_time / hours):.2f} hours before writing again.")
            await asyncio.sleep(sleep_time)


alpha = Receiver.bind("Alpha", NodeKiller.bind(), DiskLeaker.bind())
beta = Receiver.bind("Beta", NodeKiller.bind(), DiskLeaker.bind())
