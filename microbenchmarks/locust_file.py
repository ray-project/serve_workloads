from locust import HttpUser, task, constant


class ConstantUser(HttpUser):
    wait_time = constant(0)

    @task
    def hello_world(self):
        self.client.get("/")
