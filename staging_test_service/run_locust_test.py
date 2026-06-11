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
    p50_ms: Optional[float]
    p90_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    p999_ms: Optional[float]

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
                p50_ms=_float_value(row.get("50%")),
                p90_ms=_float_value(row.get("90%")),
                p95_ms=_float_value(row.get("95%")),
                p99_ms=_float_value(row.get("99%")),
                p999_ms=_float_value(row.get("99.9%")),
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


def _format_percentile_pair(p95_ms: Optional[float], p99_ms: Optional[float]) -> str:
    if p95_ms is None or p99_ms is None:
        return f"{_format_optional_ms(p95_ms)} / {_format_optional_ms(p99_ms)}"
    return f"{p95_ms:.2f} / {p99_ms:.2f} ms"


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
) -> str:
    status = "PASSED" if ok else "FAILED"
    if artifact_uri:
        artifacts_line = f"- Artifacts: {artifact_uri}"
    else:
        artifacts_line = f"- Artifacts: `{results_dir}` (local, ephemeral)"
    lines = [
        f"*Locust load test {status}*",
        f"Host: {host}",
        "",
        "*Run*",
        f"- Duration: {duration_s:.1f}s",
        f"- Exit code: {exit_code}",
        artifacts_line,
    ]

    if stats is None:
        lines.extend(["", "*Results*", "- No Locust stats CSV was produced."])
    else:
        lines.extend(
            [
                "",
                "*Results*",
                f"- Requests: {stats.request_count:,}",
                (
                    f"- Failures: {stats.failure_count:,} "
                    f"({_format_failure_rate(stats.failure_rate)})"
                ),
                f"- RPS: {_format_optional_rate(stats.requests_per_s)}",
                f"- Avg latency: {_format_optional_ms(stats.avg_response_ms)}",
                f"- P95/P99: {_format_percentile_pair(stats.p95_ms, stats.p99_ms)}",
            ]
        )

    if error:
        lines.extend(["", f"*Error:* {error}"])

    return "\n".join(lines)


# Latency-shape bands (ratio to P50) from the acceptance criteria doc:
# (label, stats attribute, healthy range, warning threshold).
RATIO_BANDS = (
    ("P50", "p50_ms", "baseline", None),
    ("P90", "p90_ms", "1.2x - 1.5x", 3.0),
    ("P95", "p95_ms", "1.5x - 2.5x", 4.0),
    ("P99", "p99_ms", "3x - 5x", 10.0),
    ("P99.9", "p999_ms", "10x - 20x", 50.0),
)


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


def build_percentiles_reply(stats: Optional[LocustStats]) -> str:
    """Thread reply 2: response-time percentiles of the aggregated run."""
    if stats is None:
        return "*Response time percentiles*\nNo Locust stats CSV was produced."
    lines = ["*Response time percentiles* (aggregated)"]
    for label, attr, _, _ in RATIO_BANDS:
        lines.append(f"- {label}: {_format_optional_ms(getattr(stats, attr))}")
    return "\n".join(lines)


def build_ratio_reply(stats: Optional[LocustStats]) -> str:
    """Thread reply 3: latency shape as ratio-to-P50, warnings marked."""
    header = "*Latency shape* (ratio to P50)"
    if stats is None:
        return f"{header}\nNo Locust stats CSV was produced."
    if not stats.p50_ms or stats.p50_ms <= 0:
        return f"{header}\nP50 unavailable; cannot compute ratios."

    rows = [f"{'Percentile':<11}{'Actual':>12}{'Ratio':>9}  {'Healthy':<13}{'Warning':<9}"]
    breached = []
    for label, attr, healthy, warn_threshold in RATIO_BANDS:
        value = getattr(stats, attr)
        if value is None:
            rows.append(f"{label:<11}{'N/A':>12}{'N/A':>9}  {healthy:<13}{'-':<9}")
            continue
        ratio = value / stats.p50_ms
        warn_col = f">{warn_threshold:g}x" if warn_threshold else "-"
        mark = ""
        if warn_threshold and ratio > warn_threshold:
            mark = "  << WARNING"
            breached.append(f"{label} is {ratio:.1f}x P50 (warning threshold >{warn_threshold:g}x)")
        rows.append(
            f"{label:<11}{_format_optional_ms(value):>12}{f'{ratio:.1f}x':>9}"
            f"  {healthy:<13}{warn_col:<9}{mark}"
        )

    lines = [header, "```", *rows, "```"]
    if breached:
        lines.extend(f":warning: {msg}" for msg in breached)
    else:
        lines.append("All percentile ratios are below their warning thresholds.")
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

    replies = [
        build_criteria_reply(),
        build_percentiles_reply(stats),
        build_ratio_reply(stats),
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
