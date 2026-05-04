"""App 6: image-dag — ingest → resize → classify → annotate (DeploymentResponse chaining)."""

from __future__ import annotations

import random

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, random_image_mb, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_DECLINE


def _opts(name: str, max_r: int, sim_gpu: bool):
    return dict(
        name=name,
        autoscaling_config=_with_max(AUTOSCALE_DECLINE, max_r),
        ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=sim_gpu),
        health_check_period_s=10,
        health_check_timeout_s=30,
    )


@serve.deployment(**_opts("image-dag-resize", 60, False))
class Resize:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(20, 60)
        return data[: max(1, len(data) // 2)]


@serve.deployment(**_opts("image-dag-classify", 60, True))
class Classify:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(30, 90)
        return data + b"|cls"


@serve.deployment(**_opts("image-dag-annotate", 60, False))
class Annotate:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(15, 40)
        return data + b"|ann"


@serve.deployment(**_opts("image-dag-ingest", 60, False))
class Ingest:
    def __init__(self, resize, classify, annotate):
        # Large payloads (2-10 MB) + DeploymentResponse chaining: bypass gRPC,
        # use actor RPC so ObjectRefs are forwarded without materialization.
        self.resize = resize.options(_by_reference=True)
        self.classify = classify.options(_by_reference=True)
        self.annotate = annotate.options(_by_reference=True)

    async def __call__(self, request: Request):
        body = await request.body()
        if not body:
            mb = random_image_mb()
            body = bytes(int(mb * 1024 * 1024))
        r1 = self.resize.remote(body)
        r2 = self.classify.remote(r1)
        r3 = self.annotate.remote(r2)
        out = await r3
        return {"bytes": len(out)}


app = Ingest.bind(Resize.bind(), Classify.bind(), Annotate.bind())
