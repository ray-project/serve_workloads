"""App 3: batch-infer — @serve.batch on simulated-GPU deployment."""

from __future__ import annotations

from typing import List

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_batch_infer_ms
from serve_validation.config import _with_max, AUTOSCALE_SPIKY


@serve.deployment(
    name="batch-infer",
    autoscaling_config=_with_max(AUTOSCALE_SPIKY, 256),
    # Must be >= max_batch_size * max_concurrent_batches (32) or batching never fills.
    max_ongoing_requests=32,
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class BatchInfer:
    @serve.batch(max_batch_size=32, batch_wait_timeout_s=0.05)
    async def handle_batch(self, reqs: List[Request]) -> List[dict]:
        await simulate_batch_infer_ms(reqs)
        return [{"batch_size": len(reqs), "i": i} for i in range(len(reqs))]

    async def __call__(self, request: Request):
        return await self.handle_batch(request)


app = BatchInfer.bind()
