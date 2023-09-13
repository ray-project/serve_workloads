from random import choice
from string import printable
from locust import FastHttpUser, task, constant, events
import os


def generate_random_string(size: int):
    """Generate a string of length `size`."""

    return "".join(choice(printable) for _ in range(size))


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "-p", "--payload-size", type=int, default=0, help="Request body size."
    )
    parser.add_argument(
        "-b",
        "--bearer-token",
        type=str,
        default="",
        help="Bearer token of service.",
    )


@events.test_start.add_listener
def _(environment, **kw):
    payload_size: int = environment.parsed_options.payload_size
    environment.payload = generate_random_string(payload_size)


class ConstantUser(FastHttpUser):
    wait_time = constant(float(os.environ.get("LOCUS_WAIT_TIME", "0")))
    network_timeout = None
    connection_timeout = None

    @task
    def hello_world(self):
        bearer_token = self.environment.parsed_options.bearer_token
        self.client.post(
            "/",
            json=self.environment.payload,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
