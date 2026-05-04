"""App 1: echo-baseline — minimal single-deployment canary."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_STABLE

_DEPLOY = dict(
    name="echo",
    autoscaling_config=_with_max(AUTOSCALE_STABLE, 64),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)


@serve.deployment(**_DEPLOY)
class EchoBaseline:
    async def __call__(self, request: Request):
        await simulate_short_cpu_ms(15, 45)
        return {"app": "echo-baseline", "path": str(request.url.path)}


app = EchoBaseline.bind()
