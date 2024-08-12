import logging
from typing import Dict

from ray import serve
from ray.serve import Application
from ray.serve.handle import DeploymentHandle


logger = logging.getLogger("ray.serve")


@serve.deployment(max_ongoing_requests=10**9)
class Forward:
    def __init__(self, handle: DeploymentHandle):
        self.handle = handle

    async def __call__(self, *args, **kwargs):
        return await (await self.handle.remote())


@serve.deployment(max_ongoing_requests=10**9)
class NoOp:
    def __call__(self, *args):
        return "No-op"


def app_builder(args: Dict[str, str]) -> Application:
    valid_arg_keys = set(["num_forwards"])
    if not valid_arg_keys.issuperset(args.keys()):
        raise ValueError(
            f"Got invalid args: {args.keys() - valid_arg_keys}. "
            f"Valid args are {valid_arg_keys}."
        )
    num_forwards = int(args.get("num_forwards", 0))
    if num_forwards == 0:
        return NoOp.bind()
    else:
        app = Forward.bind(NoOp.bind())
        for _ in range(num_forwards - 1):
            app = Forward.bind(app)
        return app
