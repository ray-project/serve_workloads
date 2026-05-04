"""App 12: grpc-canary — UserDefined gRPC surface (Ray built-in proto)."""

from __future__ import annotations

from ray import serve
from ray.serve.generated import serve_pb2

from serve_validation.common import actor_options, simulate_short_cpu_ms
from serve_validation.config import _with_max, AUTOSCALE_STABLE


@serve.deployment(
    name="grpc-canary",
    autoscaling_config=_with_max(AUTOSCALE_STABLE, 80),
    ray_actor_options=actor_options(num_cpus=0.5),
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class GrpcCanary:
    async def __call__(self, request: serve_pb2.UserDefinedMessage):
        await simulate_short_cpu_ms(15, 45)
        return serve_pb2.UserDefinedResponse(
            greeting=f"grpc-canary hello {request.name} via {request.foo}"
        )


app = GrpcCanary.bind()
