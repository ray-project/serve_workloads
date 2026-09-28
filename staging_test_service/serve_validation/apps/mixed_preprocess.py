"""App 8: mixed-preprocess — CPU preprocessing (HTTP) → simulated-GPU inference."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_encoder_ms, simulate_short_cpu_ms
from serve_validation.config import _with_floor, AUTOSCALE_DIURNAL


@serve.deployment(
    name="mixed-preprocess-gpu",
    # Floor 1 -> 2: on spot, a floor of 1 loses the only warm replica to a reclaim.
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
    # Floor 1 -> 2 and one replica per node (2026-09-25). Same reason as the GPU
    # stage above; this stage was simply missed when the floors went in on
    # 08-30, and the 09-24 60-min run found it. A spot reclaim at 18:36:50 took
    # the node holding BOTH of this deployment's replicas (2 of 2 fleet-wide).
    # It sat at 0 healthy replicas for ~40s, 12 requests queued with nowhere to
    # go, and 10 aged out at request_timeout_s=26 -- the only user-visible
    # failures in 60,184,779 requests.
    #
    # max_replicas_per_node is the load-bearing half, and it is deliberately a
    # hard constraint rather than a placement preference. That run had compact
    # scheduling explicitly OFF (RAY_SERVE_USE_COMPACT_SCHEDULING_STRATEGY: "0"
    # on version v-cjcjse7fln) and spread placement still put both replicas on
    # one node: spread is best-effort and a saturated ramp leaves it nothing to
    # spread onto. The cap therefore holds either way, which matters more now
    # that compact scheduling is enabled in this same change.
    #
    # Why recovery was slow: Serve does migrate replicas off a draining node
    # before stopping them (PENDING_MIGRATION replicas stay routable), but the
    # cluster sat at 0 available CPU through the ramp, so the replacement could
    # not be placed at all and waited ~60s for a new node. Spreading survives
    # the reclaim without needing any spare capacity. Peak here is 7 replicas
    # against a ~57-node fleet, so the per-node cap costs nothing.
    autoscaling_config=_with_floor(AUTOSCALE_DIURNAL, 128, 2),
    max_replicas_per_node=1,
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
