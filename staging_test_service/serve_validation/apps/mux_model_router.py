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

from serve_validation.common import actor_options, log_request
from serve_validation.config import _with_max, AUTOSCALE_STABLE, AUTOSCALE_STABLE_MUX_WORKER


class _FakeModel:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self._buf = bytearray(random.randint(1024, 8192))

    def predict(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"model_id": self.model_id, "echo": payload.get("q", ""), "sz": len(self._buf)}


_ingress_opts = dict(
    name="mux-ingress",
    autoscaling_config=_with_max(AUTOSCALE_STABLE, 72),
    ray_actor_options=actor_options(num_cpus=2),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=200,
)

_worker_opts = dict(
    name="mux-model-worker",
    autoscaling_config=_with_max(AUTOSCALE_STABLE_MUX_WORKER, 72),
    ray_actor_options=actor_options(num_cpus=2, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=100,
)


# Per-replica multiplexed cache size. Kept at 5 (clients address 20 Zipf-weighted
# ids), so the ~15-id tail still loads lazily and can evict a resident model.
_MODELS_PER_REPLICA = 20


@serve.deployment(**_worker_opts)
class MuxModelWorker:
    async def __init__(self):
        # Block readiness until every model is loaded: Ray Serve awaits an async
        # __init__, so this replica takes no traffic until its cache is warm.
        # _prewarm loads all models simultaneously (asyncio.gather); we just wait
        # for the whole set to finish before the replica starts serving.
        await self._prewarm()

    async def _prewarm(self):
        try:
            await asyncio.gather(
                *(self.load_model(str(i)) for i in range(_MODELS_PER_REPLICA))
            )
        except Exception:
            pass  # warm-up is best-effort; lazy load still covers a miss

    @serve.multiplexed(max_num_models_per_replica=_MODELS_PER_REPLICA)
    async def load_model(self, model_id: str) -> _FakeModel:
        await asyncio.sleep(random.uniform(0.01, 0.04))
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
        log_request(request, "mux-ingress")
        model_id = request.headers.get("serve_multiplexed_model_id", "0")
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = {}

        handle = self._worker.options(multiplexed_model_id=model_id)
        return await handle.remote(body)


app = MuxIngress.bind(MuxModelWorker.bind())
