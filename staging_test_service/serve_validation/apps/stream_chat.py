"""App 4: stream-chat — SSE streaming (simulated token generation)."""

from __future__ import annotations

import asyncio
import json
import random

from ray import serve
from starlette.requests import Request
from starlette.responses import StreamingResponse

from serve_validation.common import actor_options
from serve_validation.config import _with_max, AUTOSCALE_DIURNAL


@serve.deployment(
    name="stream-chat",
    autoscaling_config=_with_max(AUTOSCALE_DIURNAL, 512),
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class StreamChat:
    async def __call__(self, request: Request):
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
        except Exception:
            payload = {}
        n_tokens = int(payload.get("tokens", random.randint(8, 40)))
        stream_s = float(payload.get("duration_s", random.uniform(2.0, 8.0)))
        delay = stream_s / max(n_tokens, 1)

        async def gen():
            for i in range(n_tokens):
                await asyncio.sleep(delay)
                chunk = {"t": i, "tok": f"x{i}"}
                yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")


app = StreamChat.bind()
