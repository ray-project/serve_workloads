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
run_locust_test.py         # Locust Scheduled Job wrapper — posts final results to Slack
upgrade_service.py         # Weekly version-upgrade script
traffic_model.py           # Shared request mix + payload factories (locust + baseline pinger)
baseline_pinger.py         # Always-on baseline traffic generator (separate Serve service)
baseline_pinger_service.yaml   # Anyscale Service config — deploy the baseline pinger
schedules/
  version_upgrade.yaml     # Anyscale Scheduled Job — weekly redeploy
  locust_loadtest.yaml       # Anyscale Scheduled Job — Tue/Thu 60-minute load test
  locust_loadtest_daily.yaml # Anyscale Scheduled Job — daily 15-minute load test
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
export SLACK_WEBHOOK_URL=...         # optional; posts final Locust summary to Slack
python run_locust_test.py \
    --host https://serve-validation-pyz23.cld-kvedzwag2qa8i5bj.s.anyscaleuserdata-staging.com \
    --processes 16
```

The wrapper runs Locust with CSV and HTML output enabled, then posts the aggregate
`run_stats.csv` result to Slack. Artifacts are written under
`/tmp/locust-results/<timestamp>/` by default, including `run_stats.csv`,
`run_failures.csv`, and `report.html`.

Tune scale via environment variables:

| Variable | Default | Description |
|---|---|---|
| `LOCUST_RUN_MINUTES` | `150` | Total run duration (24-hour cycle compressed into this) |
| `LOCUST_PEAK_USERS` | `2000` | Locust user count at 1.0x load multiplier |
| `ANYSCALE_SERVICE_TOKEN` | — | Bearer token for Anyscale Service auth |
| `SLACK_BOT_TOKEN` | — | Slack bot token (`xoxb-…`); enables the threaded result post (summary + acceptance-criteria, percentiles, and P50-ratio replies) |
| `SLACK_CHANNEL` | — | Slack channel ID (or public channel name) for the threaded result post |
| `SLACK_WEBHOOK_URL` | — | Fallback Slack incoming webhook; used only when the bot token/channel are unset (single message, no thread) |
| `LOCUST_RESULTS_ROOT` | `/tmp/locust-results` | Root directory for Locust CSV/HTML artifacts |
| `LOCUST_PROCESSES` | `16` | Default process count used by `run_locust_test.py` |

## Baseline traffic (always-on)

`baseline_pinger.py` is a separate Anyscale Service that sends a constant low QPS
(~18 by default) to the live service 24/7, mirroring the Locust persona mix via
the shared `traffic_model.py`. This keeps realistic, continuous traffic on the
deployments so the scheduled Locust load tests ride on top of a non-zero baseline
instead of starting from idle.

Deploy it once (separate from the serve-validation service itself):

```bash
export ANYSCALE_SERVICE_TOKEN=...   # serve-validation's bearer token
envsubst < baseline_pinger_service.yaml | anyscale service deploy -f -
```

Tune the rate with the `BASELINE_QPS` env var in `baseline_pinger_service.yaml`
(or live via the deployment's `total_qps` user_config). Metrics are exported as
`baseline_pinger_*` tagged `source=baseline` for Grafana. The request mix is ~98%
echo + highscale by design (it mirrors real-usage weights), so the always-on cost
is dominated by the cheap apps.

The baseline pinger is **not** scheduled — it runs continuously; the load tests
below spike on top of it.

## Scheduled jobs

Register the scheduled jobs:

```bash
anyscale schedule apply -f schedules/version_upgrade.yaml
anyscale schedule apply -f schedules/locust_loadtest.yaml
anyscale schedule apply -f schedules/locust_loadtest_daily.yaml
```

Schedule cadence:

| Schedule | Job name | Cadence | Duration |
|---|---|---|---|
| `schedules/version_upgrade.yaml` | `serve-validation-version-upgrade` | Mondays at 10:00 America/Los_Angeles | N/A |
| `schedules/locust_loadtest.yaml` | `serve-validation-locust` | Tuesdays and Thursdays at 10:00 America/Los_Angeles | 60 min |
| `schedules/locust_loadtest_daily.yaml` | `serve-validation-locust-daily` | Daily at 10:00 America/Los_Angeles | 15 min |

Trigger any job manually:

```bash
anyscale schedule run --name serve-validation-version-upgrade
anyscale schedule run --name serve-validation-locust
anyscale schedule run --name serve-validation-locust-daily
```

## Key environment variables

| Variable | Used by | Description |
|---|---|---|
| `ANYSCALE_SERVICE_CONFIG` | `upgrade_service.py` | Path to service YAML (default: `anyscale_service.yaml`) |
| `UPGRADE_WAIT_TIMEOUT_S` | `upgrade_service.py` | Timeout for `anyscale service wait` (default: `1200`) |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | `run_locust_test.py` | Slack Web API credentials for the threaded Locust result post |
| `SLACK_WEBHOOK_URL` | `upgrade_service.py`, `run_locust_test.py` | Slack incoming webhook for deploy notifications; Locust fallback when bot token unset |
| `ANYSCALE_SERVICE_TOKEN` | `locustfile.py` | Bearer token injected into all Locust requests |
| `LOCUST_PEAK_USERS` | `locustfile.py` | User count at 1.0x load (default: `2000`) |
| `LOCUST_RUN_MINUTES` | `locustfile.py` | Run duration in minutes (default: `150`) |
