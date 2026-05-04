"""App 10: long-runner — 30–120s simulated CPU work per request."""

from __future__ import annotations

import asyncio
import json
import random

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options
from serve_validation.config import _with_max, AUTOSCALE_LONG_RUNNER


@serve.deployment(
    name="long-runner",
    autoscaling_config=_with_max(AUTOSCALE_LONG_RUNNER, 256),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class LongRunner:
    async def __call__(self, request: Request):
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            payload = {}
        seconds = float(payload.get("seconds", random.uniform(30.0, 120.0)))
        seconds = max(1.0, min(seconds, 125.0))
        await asyncio.sleep(seconds)
        return {"slept_s": seconds, "status": "done"}


app = LongRunner.bind()
