"""Locustfile that simulates users constantly sending requests to the Serve app.

CLI command:
    locust \
        --headless --html=results.html \
        -u 50 -r 10 \
        --host http://localhost:8000/ \
        -f constant_load.py
"""

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


class ConstantUser(FastHttpUser):
    wait_time = constant(0)
    network_timeout = None
    connection_timeout = None

    @task
    def hello_world(self):
        with open(IMAGE_FILE, "rb") as f:
            files = {"image_file": f}
            bearer_token = self.environment.parsed_options.bearer_token
            self.client.post(
                "/",
                files=files,
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
