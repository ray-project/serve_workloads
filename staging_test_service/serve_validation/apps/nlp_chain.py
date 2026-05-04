"""App 2: nlp-chain — tokenizer (HTTP ingress) → encoder (sim GPU) → postprocessor."""

from __future__ import annotations

from ray import serve
from starlette.requests import Request

from serve_validation.common import actor_options, simulate_encoder_ms, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_GROWTH

tok_opts = dict(
    name="nlp-tokenizer",
    autoscaling_config=_with_max(AUTOSCALE_GROWTH, 80),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
enc_opts = dict(
    name="nlp-encoder",
    autoscaling_config=_with_max(AUTOSCALE_GROWTH, 160),
    ray_actor_options=actor_options(num_cpus=0.5, simulated_gpu=True),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
post_opts = dict(
    name="nlp-postprocessor",
    autoscaling_config=_with_max(AUTOSCALE_GROWTH, 80),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)


@serve.deployment(**enc_opts)
class Encoder:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_encoder_ms()
        return data + b"|enc"


@serve.deployment(**post_opts)
class Postprocessor:
    async def __call__(self, data: bytes) -> bytes:
        await simulate_short_cpu_ms(10, 40)
        return data + b"|post"


@serve.deployment(**tok_opts)
class Tokenizer:
    def __init__(self, encoder, postprocessor):
        # DeploymentResponse chaining (encoder→postprocessor): bypass gRPC so
        # the intermediate ObjectRef is forwarded without materialization.
        self.encoder = encoder.options(_by_reference=True)
        self.postprocessor = postprocessor.options(_by_reference=True)

    async def __call__(self, request: Request):
        body = await request.body()
        await simulate_short_cpu_ms(10, 40)
        tok = body + b"|tok"
        s2 = self.encoder.remote(tok)
        s3 = self.postprocessor.remote(s2)
        out = await s3
        return {"stages": 3, "len": len(out)}


app = Tokenizer.bind(Encoder.bind(), Postprocessor.bind())
