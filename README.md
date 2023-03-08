# Serve Workloads

This repo contains long-running Serve workloads that are meant to test important
Serve features such as autoscaling, deployment graphs, and high availability.

These workloads are generally split into two applications– a `Pinger` and a
`Receiver`. The `Pinger` app sends queries to the `Receiver` app and emits logs
to track success rates and latency. The `Receiver` tests particular Serve
features such as autoscaling or deployment graphs.

The `Pinger`'s metrics can be viewed in a Grafana dashboard, letting users
monitor the workload over long periods of time.
