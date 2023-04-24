import os
import json
import asyncio
import subprocess
from starlette.requests import Request

from chaos_test.constants import RECEIVER_KILL_KEY, KillOptions

import ray
from ray import serve
from ray.experimental.state.api import list_actors


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
    def __init__(self, name, node_killer_handle):
        self.name = name
        self.node_killer_handle = node_killer_handle
        print(
            f"Receiver actor starting on node {ray.get_runtime_context().get_node_id()}"
        )

    async def __call__(self, request: Request):
        request_json = await request.json()
        kill_node = request_json.get(RECEIVER_KILL_KEY, KillOptions.SPARE)
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


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0})
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


alpha = Receiver.bind("Alpha", NodeKiller.bind())
beta = Receiver.bind("Beta", NodeKiller.bind())
