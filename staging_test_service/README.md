# Serve Validation Service

Production-grade Ray Serve validation: 12 applications exercising autoscaling, DAGs, batching, streaming, multiplexing, and large-object transfer on a CPU-only Anyscale cluster (1–200 nodes, up to 4,096 replicas). See `design.md` for the full architecture.

## Project layout

```
serve_validation/          # Application code (12 Serve apps + shared utilities)
  apps/                    # One module per app (echo_baseline, nlp_chain, …)
  common.py                # Simulated compute helpers
  config.py                # Shared constants
  smoke_all.py             # Quick HTTP + gRPC smoke test
serve_config.yaml          # Local multi-app Serve config (serve run)
anyscale_service.yaml      # Anyscale Service config (deploy)
locustfile.py              # Locust load test — 8 personas, diurnal schedule
upgrade_service.py         # Weekly version-upgrade script
schedules/
  version_upgrade.yaml     # Anyscale Scheduled Job — weekly redeploy
  locust_loadtest.yaml     # Anyscale Scheduled Job — weekly load test
```

## Prerequisites

- Python 3.10+
- `ray[serve]` (nightly or release)
- Anyscale CLI (`pip install anyscale`) — for deploy/schedule commands
- Locust (`pip install locust`) — for load testing

```
pip install -r requirements.txt
```

## Local development

Start Ray and run all 12 apps locally:

```bash
serve run serve_config.yaml
```

Smoke test (HTTP + gRPC):

```bash
python -m serve_validation.smoke_all http://localhost:8000 localhost:9000
```

Quick Locust sanity check (15 users, 2 minutes):

```bash
locust -f locustfile.py --headless --host http://localhost:8000 \
    -u 15 -r 5 --run-time 2m
```

## Deploy to Anyscale

Deploy (or update) the service:

```bash
anyscale service deploy -f anyscale_service.yaml
```

Or via the upgrade script (waits for all apps to reach RUNNING, posts to Slack):

```bash
python upgrade_service.py --config-file anyscale_service.yaml --name serve-validation-service
```

## Load testing

Run the full diurnal load test against the live service (150 min, compressed 24-hour cycle):

```bash
export ANYSCALE_SERVICE_TOKEN=...   # service bearer token; do not paste literal tokens into source files
locust -f locustfile.py --run-time 150m \
    --host https://serve-validation-pyz23.cld-kvedzwag2qa8i5bj.s.anyscaleuserdata-staging.com --processes 16
```

Tune scale via environment variables:

| Variable | Default | Description |
|---|---|---|
| `LOCUST_RUN_MINUTES` | `150` | Total run duration (24-hour cycle compressed into this) |
| `LOCUST_PEAK_USERS` | `2000` | Locust user count at 1.0x load multiplier |
| `ANYSCALE_SERVICE_TOKEN` | — | Bearer token for Anyscale Service auth |

## Scheduled jobs

Register the weekly version upgrade (Mondays 02:00 UTC) and load test (Mondays 06:00 UTC):

```bash
anyscale schedule apply -f schedules/version_upgrade.yaml
anyscale schedule apply -f schedules/locust_loadtest.yaml
```

Trigger either job manually:

```bash
anyscale schedule run --name serve-validation-version-upgrade
anyscale schedule run --name serve-validation-locust
```

## Key environment variables

| Variable | Used by | Description |
|---|---|---|
| `ANYSCALE_SERVICE_CONFIG` | `upgrade_service.py` | Path to service YAML (default: `anyscale_service.yaml`) |
| `UPGRADE_WAIT_TIMEOUT_S` | `upgrade_service.py` | Timeout for `anyscale service wait` (default: `1200`) |
| `SLACK_WEBHOOK_URL` | `upgrade_service.py` | Slack incoming webhook for deploy notifications |
| `ANYSCALE_SERVICE_TOKEN` | `locustfile.py` | Bearer token injected into all Locust requests |
| `LOCUST_PEAK_USERS` | `locustfile.py` | User count at 1.0x load (default: `2000`) |
| `LOCUST_RUN_MINUTES` | `locustfile.py` | Run duration in minutes (default: `150`) |
