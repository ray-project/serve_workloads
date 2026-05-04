"""App 8: mixed-preprocess — CPU preprocessing (HTTP) → simulated-GPU inference."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_encoder_ms, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_DIURNAL


@serve.deployment(
    name="mixed-preprocess-gpu",
    autoscaling_config=_with_max(AUTOSCALE_DIURNAL, 64),
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class InferGPU:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_encoder_ms()
        return data + b"|inf"


@serve.deployment(
    name="mixed-preprocess-cpu",
    autoscaling_config=_with_max(AUTOSCALE_DIURNAL, 128),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class PreprocessCPU:
    def __init__(self, infer):
        self.infer = infer

    async def __call__(self, request: Request):
        body = await request.body() or b"data"
        await simulate_short_cpu_ms(20, 80)
        staged = body + b"|pre"
        return {"out_len": len(await self.infer.remote(staged))}


app = PreprocessCPU.bind(InferGPU.bind())
