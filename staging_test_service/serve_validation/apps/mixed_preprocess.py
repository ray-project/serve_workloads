"""App 8: mixed-preprocess — CPU preprocessing (HTTP) → simulated-GPU inference."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_encoder_ms, simulate_short_cpu_ms
from serve_validation.config import _with_floor, _with_max, AUTOSCALE_DIURNAL


@serve.deployment(
    name="mixed-preprocess-gpu",
    # Floor 1 -> 2 (2026-08-31): simulated_gpu puts this on cpu-gpu-sim, which is
    # now spot. 27s mean startup, and it is the second hop of the chain -- a
    # reclaim at a floor of 1 fails every in-flight mixed-preprocess request.
    # mixed-preprocess-cpu below is still at 1; see the note there.
    autoscaling_config=_with_floor(AUTOSCALE_DIURNAL, 64, 2),
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=1000,
)
class InferGPU:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_encoder_ms()
        return data + b"|inf"


@serve.deployment(
    name="mixed-preprocess-cpu",
    # Left at the preset floor of 1 deliberately. cpu-general is on spot too, so
    # this carries the same single-warm-replica exposure as the three deployments
    # that were floored on 2026-08-31 -- it was simply not part of that change.
    # It is the HTTP entry point of this chain, so it is the stronger candidate
    # of the two if the floors are widened.
    autoscaling_config=_with_max(AUTOSCALE_DIURNAL, 128),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=1000,
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
