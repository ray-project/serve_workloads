"""App 5: mux-model-router — HAProxy-safe chain: HTTP ingress → multiplexed worker.

Ray Serve's HAProxy path does not preserve multiplexed-model routing; the ingress
deployment receives HTTP from HAProxy and forwards to the worker with
``DeploymentHandle.options(multiplexed_model_id=...)`` so affinity stays correct.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Dict

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options
from serve_validation.config import _with_max, AUTOSCALE_STABLE


class _FakeModel:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self._buf = bytearray(random.randint(1024, 8192))

    def predict(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"model_id": self.model_id, "echo": payload.get("q", ""), "sz": len(self._buf)}


_ingress_opts = dict(
    name="mux-ingress",
    autoscaling_config=_with_max(AUTOSCALE_STABLE, 64),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)

_worker_opts = dict(
    name="mux-model-worker",
    autoscaling_config=_with_max(AUTOSCALE_STABLE, 64),
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
)


@serve.deployment(**_worker_opts)
class MuxModelWorker:
    @serve.multiplexed(max_num_models_per_replica=5)
    async def load_model(self, model_id: str) -> _FakeModel:
        await asyncio.sleep(random.uniform(0.01, 0.08))
        return _FakeModel(model_id)

    async def __call__(self, body: Dict[str, Any]) -> Dict[str, Any]:
        model_id = serve.get_multiplexed_model_id()
        model = await self.load_model(model_id)
        return model.predict(body)


@serve.deployment(**_ingress_opts)
class MuxIngress:
    def __init__(self, worker):
        self._worker = worker

    async def __call__(self, request: Request):
        model_id = request.headers.get("serve_multiplexed_model_id", "0")
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        handle = self._worker.options(multiplexed_model_id=model_id)
        return await handle.remote(body)


app = MuxIngress.bind(MuxModelWorker.bind())
