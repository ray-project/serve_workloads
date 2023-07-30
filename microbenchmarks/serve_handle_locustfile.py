import time

from locust import User, task, constant

import ray
from ray import serve


class LocustServeClient:
    def __init__(self, request_event, deployment_name: str):
        self._request_event = request_event
        self.handle = serve.get_deployment(deployment_name).get_handle()

    def request(self, *args, **kwargs):
        request_meta = {
            "request_type": "serve_handle",
            "name": "remote_call",
            "start_time": time.time(),
            "response_length": 0,  # Arbitrary value
            "response": None,  # Arbitrary value
            "context": {},  # Arbitrary value
            "exception": None,
        }
        start_perf_counter = time.perf_counter()
        try:
            request_meta["response"] = ray.get(self.handle.remote(*args, **kwargs))
        except Exception as e:
            request_meta["exception"] = e
        request_meta["response_time"] = (
            time.perf_counter() - start_perf_counter
        ) * 1000
        self._request_event.fire(**request_meta)  # Logs in Locust
        return request_meta["response"]


class ServeHandleUser(User):

    abstract = True  # Don't instantiate this as an actual user

    def __init__(self, environment):
        super().__init__(environment)

        # Change deployment_name here to use a different deployment
        deployment_name = "no_ops_NoOp"

        self.client = LocustServeClient(
            request_event=environment.events.request, deployment_name=deployment_name
        )


class ConstantUser(ServeHandleUser):

    wait_time = constant(0)
    network_timeout = None
    connection_timeout = None

    @task
    def hello_world(self):
        self.client.request()
