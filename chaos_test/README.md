# Chaos Test

This test assesses Serve's availability under hostile conditions– where nodes
are intentionally killed periodically.

## Workload
* `Receiver` service
    * 3 Ray cluster nodes
        * 1 head node, 2 worker nodes
        * Each node must have 1 `"node_singleton"` custom resource.
    * 3 `Receiver` replicas
        * Trivial workload (returns a string or asks `NodeKiller` to kill a node)
        * Assigns one replica per node using `custom_resources`
            * Prevents all replicas from getting stuck on one node
    * 1 `NodeKiller` replica
        * Kills its node with ray stop --head
* Pinger service
    * 1 Ray cluster node (just the head node)
    * 1 Pinger replica

## Behavior
* Pinger constantly sends requests to the Receiver
    * Every ~30 minutes: Pinger requests Receiver kill a random node
* Pinger logs:
    * `pinger_num_requests`: Number of requests sent by the `Pinger`.
    * `pinger_num_requests_succeeded`: Number of requests that succeeded.
    * `pinger_num_requests_failed`: Number of requests that failed.
    * `pinger_num_kill_requests_sent`: Number of kill requests sent by the `Pinger`.
    * `pinger_request_latency`: Latency of the latest `Pinger` request.
    * `pinger_404`: Number of 404 HTTP response errors the `Pinger` received.
    * `pinger_500`: Number of 500 HTTP response errors the `Pinger` received.
    * `pinger_502`: Number of 502 HTTP response errors the `Pinger` received.
    * `pinger_http_response_other_error`: Number of other HTTP response errors the `Pinger` received.

## Instructions to run the workload

1. Launch the `Receiver` service. Its Serve config is in `receiver_config.yaml`.
2. Get the `Receiver` service's URL and any authentication token needed to access it.
You can omit the authentication token in the config if your `Receiver` doesn't need one.
4. Launch the `Pinger` service using `pinger_config.yaml`.
5. Start the `Pinger` service by making a `GET` request to its `/start` endpoint. This request will hang, so you need to kill it after a couple seconds. The `Pinger` service should now be querying the `Receiver`.
6. You can check `Pinger`'s metrics either through a Grafana dashboard or by sending a `GET` request to its `/info` endpoint.
