"""App 9: heavy-payload — single deployment, large request/response bodies."""

from __future__ import annotations

import json

from ray import serve
from starlette.requests import Request
from starlette.responses import Response

from serve_validation.common import actor_options, random_heavy_mb
from serve_validation.config import _with_max, AUTOSCALE_HEAVY


@serve.deployment(
    name="heavy-payload",
    autoscaling_config=_with_max(AUTOSCALE_HEAVY, 128),
    ray_actor_options=actor_options(num_cpus=2),
    health_check_period_s=10,
    health_check_timeout_s=30,
    max_ongoing_requests=5,
    graceful_shutdown_timeout_s=1200,
)
class HeavyPayload:
    async def __call__(self, request: Request):
        body = await request.body()
        try:
            spec = json.loads(body.decode("utf-8"))
            mb = float(spec.get("mb", random_heavy_mb()))
        except Exception:
            mb = random_heavy_mb()
        n = int(mb * 1024 * 1024)
        return Response(content=bytes(n), media_type="application/octet-stream")


app = HeavyPayload.bind()
