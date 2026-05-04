"""App 11: highscale-stress — minimal CPU work at high replica counts."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, sleep_ms
from serve_validation.config import _with_max, AUTOSCALE_SPIKY


@serve.deployment(
    name="highscale-stress",
    autoscaling_config=_with_max(AUTOSCALE_SPIKY, 1536),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class HighscaleStress:
    async def __call__(self, request: Request):
        await sleep_ms(10.0)
        return {"ok": True}


app = HighscaleStress.bind()
