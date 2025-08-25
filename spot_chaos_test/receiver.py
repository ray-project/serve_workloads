import os
import json
import time
import asyncio
import logging
import subprocess
from typing import Dict
from starlette.requests import Request

import ray
from ray import serve
from ray._common.utils import run_background_task
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
    def __init__(self, name):
        self.name = name
        print(
            f"Receiver actor starting on node {ray.get_runtime_context().get_node_id()}"
        )

    async def __call__(self, request: Request):
        return f"(PID: {os.getpid()}) {self.name} Receiver running!"


alpha = Receiver.bind("Alpha")
beta = Receiver.bind("Beta")
