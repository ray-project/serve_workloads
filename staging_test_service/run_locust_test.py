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


@dataclass(frozen=True)
class LocustStats:
    request_count: int
    failure_count: int
    avg_response_ms: Optional[float]
    requests_per_s: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]

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
                p95_ms=_float_value(row.get("95%")),
                p99_ms=_float_value(row.get("99%")),
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
    error: Optional[str] = None,
) -> str:
    status = "PASSED" if ok else "FAILED"
    lines = [
        f"Locust load test {status}",
        f"Host: {host}",
        f"Duration: {duration_s:.1f}s",
        f"Exit code: {exit_code}",
        f"Artifacts: {results_dir}",
    ]

    if stats is None:
        lines.append("No Locust stats CSV was produced.")
    else:
        lines.extend(
            [
                f"Requests: {stats.request_count}",
                f"Failures: {stats.failure_count} ({stats.failure_rate:.2%})",
                f"RPS: {_format_optional_rate(stats.requests_per_s)}",
                f"Avg latency: {_format_optional_ms(stats.avg_response_ms)}",
                f"P95/P99: {_format_percentile_pair(stats.p95_ms, stats.p99_ms)}",
            ]
        )

    if error:
        lines.append(f"Error: {error}")

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


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


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
        "--only-summary",
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
    ok = exit_code == 0
    text = build_slack_message(
        ok=ok,
        host=args.host,
        duration_s=duration_s,
        exit_code=exit_code,
        stats=stats,
        results_dir=results_dir,
        error=error,
    )

    print(text)
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        post_slack(webhook, text, ok=ok)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
