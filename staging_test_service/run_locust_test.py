#!/usr/bin/env python3
"""Run the Serve validation Locust test and post final results to Slack."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence


# Notion doc defining the load-test acceptance criteria (linked in the Slack summary).
ACCEPTANCE_CRITERIA_URL = (
    "https://app.notion.com/p/anyscale-hq/"
    "Staging-Test-Service-in-review-375027c809cb80619eb0c0432c7519eb"
    "?source=copy_link#379027c809cb8066aa3afdb268e6b7f1"
)


@dataclass(frozen=True)
class LocustStats:
    request_count: int
    failure_count: int
    avg_response_ms: Optional[float]
    requests_per_s: Optional[float]

    @property
    def failure_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.failure_count / self.request_count


def _float_value(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value or value.upper() == "N/A":
        return None
    return float(value)


def _int_value(value: Optional[str]) -> int:
    parsed = _float_value(value)
    if parsed is None:
        return 0
    return int(parsed)


def parse_stats_csv(stats_path: Path) -> Optional[LocustStats]:
    """Return the aggregate row from a Locust ``--csv`` stats file."""
    if not stats_path.is_file():
        return None

    with stats_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("Name", "").strip().lower() != "aggregated":
                continue
            return LocustStats(
                request_count=_int_value(row.get("Request Count")),
                failure_count=_int_value(row.get("Failure Count")),
                avg_response_ms=_float_value(row.get("Average Response Time")),
                requests_per_s=_float_value(row.get("Requests/s")),
            )

    return None


def _format_optional_ms(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f} ms"


def _format_optional_rate(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _format_failure_rate(rate: float) -> str:
    return f"{rate * 100:.4f}%"


def build_slack_message(
    *,
    ok: bool,
    host: str,
    duration_s: float,
    exit_code: int,
    stats: Optional[LocustStats],
    results_dir: Path,
    artifact_uri: Optional[str] = None,
    error: Optional[str] = None,
    report_url: Optional[str] = None,
) -> str:
    """Compact summary: a monospace key/value table (pass/fail, duration,
    requests, failures, rps) plus a clickable detailed-report link. host,
    exit_code, artifact_uri and results_dir are accepted for call-site
    compatibility but intentionally not shown."""
    status = "PASSED" if ok else "FAILED"
    if stats is not None:
        requests = f"{stats.request_count:,}"
        failures = f"{stats.failure_count:,} ({_format_failure_rate(stats.failure_rate)})"
        rps = _format_optional_rate(stats.requests_per_s)
    else:
        requests = failures = rps = "n/a"

    rows = [
        ("Load test", status),
        ("Duration", f"{duration_s:.1f}s"),
        ("Requests", requests),
        ("Failures", failures),
        ("RPS", rps),
    ]
    width = max(len(label) for label, _ in rows)
    table = "\n".join(f"{label:<{width}}  {value}" for label, value in rows)

    lines = ["*Locust load test*", "```", table, "```"]
    # The link must sit outside the code block; Slack does not render links
    # inside a ``` block as clickable.
    if report_url:
        lines.append(f"Detailed report: <{report_url}|report_detailed.html>")
    if error:
        lines.append(f"*Error:* {error}")

    return "\n".join(lines)


# Latency-shape bands (each percentile as a ratio of the deployment's own
# P50) from the acceptance criteria doc: (label, attribute, warning threshold).
RATIO_BANDS = (
    ("P90", "p90_ms", 3.0),
    ("P95", "p95_ms", 4.0),
    ("P99", "p99_ms", 10.0),
    ("P99.9", "p999_ms", 50.0),
)
RATIO_LEGEND = (
    "Healthy: P90 1.2-1.5x, P95 1.5-2.5x, P99 3-5x, P99.9 10-20x of own P50; "
    "`!` marks warning (P90 >3x, P95 >4x, P99 >10x, P99.9 >50x)."
)


@dataclass(frozen=True)
class DeploymentStats:
    name: str
    requests: int
    failures: int
    p50_ms: Optional[float]
    p90_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    p999_ms: Optional[float]
    approx: bool = False  # True when merged from per-name CSV rows


def load_deployment_stats(json_path: Path) -> Optional[list[DeploymentStats]]:
    """Read the exact per-deployment percentiles the locustfile writes at
    test stop (run_deployments.json). Returns None if absent/unreadable."""
    if not json_path.is_file():
        return None
    try:
        raw = json.loads(json_path.read_text())
        deployments = [
            DeploymentStats(
                name=name,
                requests=int(d.get("requests", 0)),
                failures=int(d.get("failures", 0)),
                p50_ms=d.get("p50"),
                p90_ms=d.get("p90"),
                p95_ms=d.get("p95"),
                p99_ms=d.get("p99"),
                p999_ms=d.get("p99.9"),
            )
            for name, d in raw.items()
        ]
    except Exception as exc:
        print(f"Could not parse {json_path}: {exc}", file=sys.stderr)
        return None
    return sorted(deployments, key=lambda d: -d.requests) or None


def deployment_stats_from_csv(stats_path: Path) -> Optional[list[DeploymentStats]]:
    """Fallback when run_deployments.json is missing: group the stats CSV's
    per-name rows by deployment route prefix. Multi-row groups (e.g. mux's
    "[model=N]" rows) get request-count-weighted percentiles — an
    approximation (marked with ~), since percentiles cannot be merged
    exactly after the fact."""
    if not stats_path.is_file():
        return None
    groups: dict = {}
    with stats_path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            name = row.get("Name", "").strip()
            if not name or name.lower() == "aggregated":
                continue
            count = _int_value(row.get("Request Count"))
            if count <= 0:
                continue
            groups.setdefault(name.split(" [", 1)[0], []).append((count, row))

    deployments = []
    for name, rows in groups.items():
        total = sum(count for count, _ in rows)
        failures = sum(_int_value(row.get("Failure Count")) for _, row in rows)

        def merged(col: str) -> Optional[float]:
            acc = 0.0
            for count, row in rows:
                value = _float_value(row.get(col))
                if value is None:
                    return None
                acc += count * value
            return acc / total

        deployments.append(
            DeploymentStats(
                name=name,
                requests=total,
                failures=failures,
                p50_ms=merged("50%"),
                p90_ms=merged("90%"),
                p95_ms=merged("95%"),
                p99_ms=merged("99%"),
                p999_ms=merged("99.9%"),
                approx=len(rows) > 1,
            )
        )
    return sorted(deployments, key=lambda d: -d.requests) or None


def _name_width(deployments: list[DeploymentStats]) -> int:
    return max(12, max(len(d.name) + (1 if d.approx else 0) for d in deployments) + 2)


def _display_name(dep: DeploymentStats) -> str:
    return f"~{dep.name}" if dep.approx else dep.name


def _format_ms_cell(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _breaching_deployments(
    deployments: list[DeploymentStats],
) -> list[DeploymentStats]:
    """Deployments whose latency shape breaches a warning threshold (an '!'
    band): P90 >3x, P95 >4x, P99 >10x, or P99.9 >50x of their own P50.
    Deployments without a usable P50 cannot be evaluated and are skipped."""
    flagged = []
    for dep in deployments:
        if not dep.p50_ms or dep.p50_ms <= 0:
            continue
        for _label, attr, warn_threshold in RATIO_BANDS:
            value = getattr(dep, attr)
            if value is not None and value / dep.p50_ms > warn_threshold:
                flagged.append(dep)
                break
    return flagged


def build_criteria_reply() -> str:
    """Thread reply 1: acceptance criteria link."""
    return "\n".join(
        [
            f"Acceptance criteria: <{ACCEPTANCE_CRITERIA_URL}|Staging Test Service>",
            (
                "_Note: If the failure rate is >= 0.01%, this may signify "
                "a regression and should be investigated._"
            ),
        ]
    )


def build_percentiles_reply(deployments: Optional[list[DeploymentStats]]) -> str:
    """Thread reply 2: response-time percentiles, limited to deployments that
    breach a warning threshold (see RATIO_BANDS); healthy ones are omitted."""
    header = "*Response time percentiles* (ms) — deployments above the warning thresholds"
    if not deployments:
        return f"{header}\nNo Locust stats were produced."
    flagged = _breaching_deployments(deployments)
    if not flagged:
        return "*Response time percentiles by deployment*\nAll deployments are within the latency guidelines."
    width = _name_width(flagged)
    rows = [
        f"{'Deployment':<{width}}{'Requests':>10}{'P50':>9}{'P90':>9}"
        f"{'P95':>9}{'P99':>9}{'P99.9':>9}"
    ]
    for dep in flagged:
        rows.append(
            f"{_display_name(dep):<{width}}{dep.requests:>10,}"
            f"{_format_ms_cell(dep.p50_ms):>9}{_format_ms_cell(dep.p90_ms):>9}"
            f"{_format_ms_cell(dep.p95_ms):>9}{_format_ms_cell(dep.p99_ms):>9}"
            f"{_format_ms_cell(dep.p999_ms):>9}"
        )
    lines = [header, "```", *rows, "```"]
    if any(d.approx for d in flagged):
        lines.append("_~ approximated: merged from per-name CSV rows._")
    return "\n".join(lines)


def build_ratio_reply(deployments: Optional[list[DeploymentStats]]) -> str:
    """Thread reply 3: latency shape (each percentile as a ratio of the
    deployment's own P50), limited to deployments that breach a warning
    threshold; healthy ones are omitted."""
    header = "*Latency shape* (ratio to own P50) — deployments above the warning thresholds"
    if not deployments:
        return f"{header}\nNo Locust stats were produced."
    flagged = _breaching_deployments(deployments)
    if not flagged:
        return "*Latency shape by deployment*\nAll deployments are below the warning thresholds."

    width = _name_width(flagged)
    rows = [
        f"{'Deployment':<{width}}{'P50(ms)':>9}{'P90':>8}{'P95':>8}{'P99':>8}{'P99.9':>8}"
    ]
    breached = []
    for dep in flagged:
        cells = []
        dep_breaches = []
        for label, attr, warn_threshold in RATIO_BANDS:
            value = getattr(dep, attr)
            if not dep.p50_ms or dep.p50_ms <= 0 or value is None:
                cells.append("-")
                continue
            ratio = value / dep.p50_ms
            mark = ""
            if ratio > warn_threshold:
                mark = "!"
                dep_breaches.append(f"{label} {ratio:.1f}x (>{warn_threshold:g}x)")
            cells.append(f"{ratio:.1f}x{mark}")
        rows.append(
            f"{_display_name(dep):<{width}}{_format_ms_cell(dep.p50_ms):>9}"
            f"{cells[0]:>8}{cells[1]:>8}{cells[2]:>8}{cells[3]:>8}"
        )
        if dep_breaches:
            breached.append(f":warning: `{dep.name}`: {', '.join(dep_breaches)}")

    lines = [header, "```", *rows, "```", RATIO_LEGEND, *breached]
    return "\n".join(lines)


def post_slack(webhook: str, text: str, ok: bool) -> None:
    try:
        payload = json.dumps(
            {"text": text, "attachments": [{"color": "good" if ok else "danger"}]}
        ).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"Slack notification failed: {exc}", file=sys.stderr)


def _slack_api_post(token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_slack_thread(
    token: str, channel: str, text: str, replies: Sequence[str], ok: bool
) -> bool:
    """Post the summary, then each reply in its thread. Returns True if the
    summary was posted (replies are best-effort). Never raises: a Slack
    failure must not mask Locust's exit code."""
    try:
        main = _slack_api_post(
            token,
            {
                "channel": channel,
                "text": text,
                "attachments": [{"color": "good" if ok else "danger"}],
                "unfurl_links": False,
            },
        )
        if not main.get("ok"):
            print(f"Slack chat.postMessage failed: {main.get('error')}", file=sys.stderr)
            return False
        thread_ts = main["ts"]
        channel_id = main.get("channel", channel)
        for reply in replies:
            res = _slack_api_post(
                token,
                {
                    "channel": channel_id,
                    "thread_ts": thread_ts,
                    "text": reply,
                    "unfurl_links": False,
                },
            )
            if not res.get("ok"):
                print(f"Slack thread reply failed: {res.get('error')}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"Slack notification failed: {exc}", file=sys.stderr)
        return False


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def upload_artifacts(results_dir: Path) -> Optional[str]:
    """Best-effort copy of the results dir to durable Anyscale storage.

    Locust writes report.html / *.csv to the Job cluster's local disk, which
    is wiped when the scheduled job's cluster tears down. Copy them to the
    cloud object store at ``$ANYSCALE_ARTIFACT_STORAGE`` so the HTML charts and
    per-interval stats history survive and can be downloaded after the run.

    Returns the destination URI on success, else ``None``. Never raises: a
    failed upload must not mask Locust's exit code (the regression signal).
    """
    base = os.environ.get("ANYSCALE_ARTIFACT_STORAGE")
    if not base:
        print(
            f"ANYSCALE_ARTIFACT_STORAGE unset; results left in {results_dir} "
            "(ephemeral on a Job cluster).",
            file=sys.stderr,
        )
        return None

    dest = f"{base.rstrip('/')}/locust-results/{results_dir.name}"
    try:
        import pyarrow.fs as pafs

        dest_fs, dest_path = pafs.FileSystem.from_uri(dest)
        for path in sorted(results_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(results_dir).as_posix()
            # open_output_stream creates the S3 key (or local parent dirs).
            with dest_fs.open_output_stream(f"{dest_path}/{rel}") as out:
                out.write(path.read_bytes())
        print(f"Uploaded Locust artifacts to {dest}")
        return dest
    except Exception as exc:
        print(f"Artifact upload to {dest} failed: {exc}", file=sys.stderr)
        return None


def _build_locust_command(
    *,
    locustfile: Path,
    host: str,
    processes: int,
    exit_code_on_error: int,
    csv_prefix: Path,
    html_path: Path,
    extra_args: Sequence[str],
) -> list[str]:
    return [
        "locust",
        "-f",
        str(locustfile),
        "--headless",
        "--host",
        host,
        "--processes",
        str(processes),
        "--exit-code-on-error",
        str(exit_code_on_error),
        "--csv",
        str(csv_prefix),
        "--html",
        str(html_path),
        *extra_args,
    ]


def _parse_args(argv: Optional[Sequence[str]] = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run Locust and post final Serve validation results to Slack."
    )
    parser.add_argument("--host", required=True, help="Anyscale Service ingress URL.")
    parser.add_argument(
        "-f",
        "--locustfile",
        default="locustfile.py",
        type=Path,
        help="Locust file to run.",
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=int(os.environ.get("LOCUST_PROCESSES", "16")),
        help="Number of Locust worker processes.",
    )
    parser.add_argument(
        "--exit-code-on-error",
        type=int,
        default=int(os.environ.get("LOCUST_EXIT_CODE_ON_ERROR", "0")),
        help="Exit code Locust should use when requests fail.",
    )
    parser.add_argument(
        "--results-root",
        default=os.environ.get("LOCUST_RESULTS_ROOT", "/tmp/locust-results"),
        type=Path,
        help="Directory under which timestamped Locust artifacts are written.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Locust command and Slack message without executing Locust.",
    )
    return parser.parse_known_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args, extra_args = _parse_args(argv)
    results_dir = args.results_root / _timestamp()
    csv_prefix = results_dir / "run"
    html_path = results_dir / "report.html"
    stats_path = results_dir / "run_stats.csv"

    locust_cmd = _build_locust_command(
        locustfile=args.locustfile,
        host=args.host,
        processes=args.processes,
        exit_code_on_error=args.exit_code_on_error,
        csv_prefix=csv_prefix,
        html_path=html_path,
        extra_args=extra_args,
    )

    if args.dry_run:
        print("Would run:", subprocess.list2cmdline(locust_cmd))
        return 0

    results_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    exit_code = 1
    error = None

    try:
        completed = subprocess.run(locust_cmd, check=False)
        exit_code = completed.returncode
    except FileNotFoundError:
        error = "Locust executable not found."
        print(error, file=sys.stderr)

    duration_s = time.monotonic() - started
    stats = parse_stats_csv(stats_path)
    artifact_uri = upload_artifacts(results_dir)
    ok = exit_code == 0
    text = build_slack_message(
        ok=ok,
        host=args.host,
        duration_s=duration_s,
        exit_code=exit_code,
        stats=stats,
        results_dir=results_dir,
        artifact_uri=artifact_uri,
        error=error,
    )

    deployments = load_deployment_stats(
        results_dir / "run_deployments.json"
    ) or deployment_stats_from_csv(stats_path)
    replies = [
        build_criteria_reply(),
        build_percentiles_reply(deployments),
        build_ratio_reply(deployments),
    ]
    print("\n\n".join([text, *replies]))

    # Threaded posting (summary + 3 replies) needs the Web API; incoming
    # webhooks return no message ts, so they cannot start a thread.
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    posted = False
    if token and channel:
        posted = post_slack_thread(token, channel, text, replies, ok=ok)
    elif webhook:
        print(
            "SLACK_BOT_TOKEN/SLACK_CHANNEL unset; falling back to webhook "
            "(single message, no thread).",
            file=sys.stderr,
        )
    if not posted and webhook:
        post_slack(webhook, "\n\n".join([text, *replies]), ok=ok)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
