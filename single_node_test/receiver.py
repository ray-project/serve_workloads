import os
from starlette.requests import Request

from single_node_test.constants import ARTICLE_TEXT_KEY

import ray
from ray import serve

from transformers import pipeline


@serve.deployment(
    num_replicas=1,
    ray_actor_options={
        "num_cpus": 0,
        # There should be 1 node_singleton per node to ensure each node has
        # 1 replica.
        "resources": {
            "node_singleton": 1,
        },
    },
)
class Receiver:
    def __init__(self):
        self.summarizer = pipeline("summarization", model="t5-small")
        print(
            f"Receiver actor starting on node {ray.get_runtime_context().get_node_id()}"
        )

    async def __call__(self, request: Request):
        request_json = await request.json()
        if ARTICLE_TEXT_KEY in request_json:
            article_text = request_json[ARTICLE_TEXT_KEY]
            summary = self.summarizer(article_text)
            return {"summary": summary}
        else:
            return f"(PID: {os.getpid()}) Received request!"


graph = Receiver.bind()
