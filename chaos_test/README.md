# Chaos Test

This test assesses Serve's availability under hostile conditions– where nodes
are intentionally killed periodically.

## Workload
* `Receiver` service
    * 3 Ray cluster nodes
        * 1 head node, 2 worker nodes
        * Each node must have the following custom resources:
            * 1 `"alpha_singleton"` custom resource
            * 1 `"beta_singleton"` custom resource
    * 3 `Receiver` replicas
        * Trivial workload (returns a string or asks `NodeKiller` to kill a node)
        * Assigns one replica per node using `custom_resources`
            * Prevents all replicas from getting stuck on one node
    * 1 `NodeKiller` replica
        * Kills its node with either `ray stop --head` or `sudo halt --force`
    * 1 `DiskLeaker` replica
        * Logs `num_GB` of data every 30 minutes, written as 1 MiB lines so the
          replica's peak memory stays small. `num_GB`, `chunk_MB` and
          `startup_delay_s` are tunable through `user_config`.
        * Pinned off the head node with the label selector
          `ray.io/node-group: "!head"`, which Ray derives from
          `RAY_NODE_TYPE_NAME`. A memory or disk spike on the head takes the
          Serve controller down with it. Rename the selector value if the head
          node type is not called `head` in your compute config.
* `Pinger` service
    * 1 Ray cluster node (just the head node)
    * 1 `Pinger` replica
        * Sends requests to the `Receiver` service at a constant QPS
    * 1 `Reaper` replica
        * Periodically sends a request to the `Receiver` service asking the
          `NodeKiller` to kill a node
    * 1 `ReceiverHelmsman` replica
        * Periodically upgrades the `Receiver` service with a new `import_path`. 
          This changes the string that the `Receiver` replica returns and the
          specific type of custom resource that the `Receiver` replicas use.
        * Watches the `Receiver` service's status.

## Instructions to run the workload

1. Launch the `Receiver` service. Its Serve config is in [`receiver_config.yaml`](receiver_config.yaml).
2. Get the `Receiver` service's URL and any authentication token needed to access it.
3. Fill in [`pinger_config.yaml`](pinger_config.yaml) with the `Receiver` service's info. You can omit the authentication token in the config if your `Receiver` doesn't need one.
4. Launch the `Pinger` service using [`pinger_config.yaml`](pinger_config.yaml).
5. Import the Grafana dashboard from [`grafana_dashboard.json`](grafana_dashboard.json) if you're running a Grafana server.
5. You can check `Pinger`'s metrics either through the Grafana dashboard or by sending a `GET` request to the `Pinger` service's `/info` endpoint.
