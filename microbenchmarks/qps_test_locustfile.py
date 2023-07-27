from locust import FastHttpUser, task, constant, events


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
        help="Bearer token of Anyscale service.",
    )


@events.test_start.add_listener
def _(environment, **kw):
    payload_size: int = environment.parsed_options.payload_size
    environment.payload = "0" * payload_size


class ConstantUser(FastHttpUser):
    wait_time = constant(0)

    @task
    def hello_world(self):
        bearer_token = self.environment.parsed_options.bearer_token
        self.client.post(
            "/",
            json=self.environment.payload,
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
