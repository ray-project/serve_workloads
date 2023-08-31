"""Locustfile that simulates users constantly sending requests to the Serve app.

CLI command:
    locust \
        --headless --html=results.html \
        -u 50 -r 10 \
        --host http://localhost:8000/ \
        -f constant_load.py
"""

import json
import base64
from locust import FastHttpUser, task, constant, events


IMAGE_FILE = "problem.png"


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "-b",
        "--bearer-token",
        type=str,
        default="",
        help="Bearer token of service.",
    )


@events.test_start.add_listener
def _(environment, **kw):
    with open(IMAGE_FILE, "rb") as f:
        image_bytes = f.read()
    
    image_bytes_utf8: str = base64.b64encode(image_bytes).decode("utf8")
    environment.payload = json.dumps({"image_bytes_utf8": image_bytes_utf8})


class ConstantUser(FastHttpUser):
    wait_time = constant(0)
    network_timeout = None
    connection_timeout = None

    @task
    def hello_world(self):
        bearer_token = self.environment.parsed_options.bearer_token
        self.client.post(
            "/",
            data=self.environment.payload,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-type": "application/json",
            },
        )
