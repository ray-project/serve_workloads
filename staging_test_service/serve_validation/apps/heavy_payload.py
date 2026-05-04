"""App 9: heavy-payload — single deployment, large request/response bodies."""

from __future__ import annotations

import json

from ray import serve
from starlette.requests import Request
from starlette.responses import Response

from serve_validation.common import actor_options, random_heavy_mb
from serve_validation.config import _with_max, AUTOSCALE_GROWTH


@serve.deployment(
    name="heavy-payload",
    autoscaling_config=_with_max(AUTOSCALE_GROWTH, 128),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
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
        buf = bytearray(n)
        return Response(content=bytes(buf), media_type="application/octet-stream")


app = HeavyPayload.bind()
