"""App 7: cpu-fanout — router fans out to 4 CPU workers, then aggregator."""

from __future__ import annotations

import asyncio

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_SPIKY

router_opts = dict(
    name="cpu-fanout-router",
    autoscaling_config=_with_max(AUTOSCALE_SPIKY, 64),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=60,
    max_ongoing_requests=200,
    graceful_shutdown_timeout_s=1200,
)

worker_opts = dict(
    autoscaling_config=_with_max(AUTOSCALE_SPIKY, 64),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=60,
    max_ongoing_requests=20,
    graceful_shutdown_timeout_s=1200,
)

agg_opts = dict(
    name="cpu-fanout-agg",
    autoscaling_config=_with_max(AUTOSCALE_SPIKY, 64),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=60,
    max_ongoing_requests=20,
    graceful_shutdown_timeout_s=1200,
)


@serve.deployment(**{**worker_opts, "name": "cpu-fanout-worker-0"})
class Worker0:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(10, 35)
        return data + b"|w0"


@serve.deployment(**{**worker_opts, "name": "cpu-fanout-worker-1"})
class Worker1:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(10, 35)
        return data + b"|w1"


@serve.deployment(**{**worker_opts, "name": "cpu-fanout-worker-2"})
class Worker2:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(10, 35)
        return data + b"|w2"


@serve.deployment(**{**worker_opts, "name": "cpu-fanout-worker-3"})
class Worker3:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(10, 35)
        return data + b"|w3"


@serve.deployment(**agg_opts)
class Aggregator:
    async def __call__(self, parts: list) -> dict:
        await simulate_short_cpu_ms(5, 20)
        joined = sum(len(p) for p in parts)
        return {"parts": len(parts), "bytes": joined}


@serve.deployment(**router_opts)
class Router:
    def __init__(self, w0, w1, w2, w3, agg):
        self.w0 = w0
        self.w1 = w1
        self.w2 = w2
        self.w3 = w3
        self.agg = agg

    async def __call__(self, request: Request):
        body = await request.body() or b"ping"
        results = await asyncio.gather(
            self.w0.remote(body),
            self.w1.remote(body),
            self.w2.remote(body),
            self.w3.remote(body),
            return_exceptions=True,
        )
        # Filter out failed ones; pass surviving results to aggregator.
        successful = [r for r in results if not isinstance(r, BaseException)]
        if not successful:
            # If everyone failed, surface a 503 instead of a vague 500
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="all workers failed")
        return await self.agg.remote(successful)


app = Router.bind(
    Worker0.bind(),
    Worker1.bind(),
    Worker2.bind(),
    Worker3.bind(),
    Aggregator.bind(),
)
