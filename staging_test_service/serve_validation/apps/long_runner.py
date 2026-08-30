"""App 10: long-runner — 8–20s simulated CPU work per request (20s hard cap)."""

from __future__ import annotations

import asyncio
import json
import random

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options
from serve_validation.config import _with_floor, AUTOSCALE_LONG_RUNNER


@serve.deployment(
    name="long-runner",
    autoscaling_config=_with_floor(AUTOSCALE_LONG_RUNNER, 256, 2),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=50,
    graceful_shutdown_timeout_s=25,
)
class LongRunner:
    async def __call__(self, request: Request):
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            payload = {}
        seconds = float(payload.get("seconds", random.uniform(8.0, 20.0)))
        # Clamp 65 -> 20 (2026-08-29 spot experiment; was 125 -> 65 on
        # 2026-08-21): this is the hard ceiling on request duration for the
        # whole service, and every drain/stop timeout is sized from it (see
        # traffic_model.long_payload and RAY_SERVE_HAPROXY_HARD_STOP_AFTER_S).
        # A client asking for more is capped here rather than silently
        # widening those budgets. 20s is what lets hard-stop reach 30s and so
        # fit inside AWS's 120s spot notice. Revert to 65 with the rest of the
        # ladder when the experiment ends.
        seconds = max(1.0, min(seconds, 20.0))
        await asyncio.sleep(seconds)
        return {"slept_s": seconds, "status": "done"}


app = LongRunner.bind()
