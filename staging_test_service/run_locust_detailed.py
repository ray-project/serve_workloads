#!/usr/bin/env python3
"""Detailed-report variant of run_locust_test.py: runs the same load test via
locustfile_detailed.py (which also writes run_timeseries.csv, run_users.csv,
and run_meta.json), renders report_detailed.html with per-deployment
latency-over-time charts, and uploads all artifacts to
$ANYSCALE_ARTIFACT_STORAGE/locust-results-detailed/<timestamp>/."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

import run_locust_test as base


def _inject_default_locustfile(argv: Optional[Sequence[str]]) -> list[str]:
    """Default -f to locustfile_detailed.py without modifying base's parser."""
    raw = list(sys.argv[1:] if argv is None else argv)
    has_f = any(
        a in ("-f", "--locustfile") or a.startswith("--locustfile=") for a in raw
    )
    return raw if has_f else ["-f", "locustfile_detailed.py", *raw]


def _upload_artifacts_detailed(results_dir: Path) -> Optional[str]:
    """Best-effort copy of the results dir to durable Anyscale storage under
    locust-results-detailed/ (base.upload_artifacts hardcodes locust-results,
    so it cannot be reused). Returns the destination URI on success, else
    None. Never raises: a failed upload must not mask Locust's exit code."""
    storage = os.environ.get("ANYSCALE_ARTIFACT_STORAGE")
    if not storage:
        print(
            f"ANYSCALE_ARTIFACT_STORAGE unset; results left in {results_dir} "
            "(ephemeral on a Job cluster).",
            file=sys.stderr,
        )
        return None

    dest = f"{storage.rstrip('/')}/locust-results-detailed/{results_dir.name}"
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


# Pre-signed GET URL lifetime. SigV4 caps this at 7 days; when the job runs
# under temporary (instance-role/STS) credentials the link stops working once
# those credentials expire, which can be sooner than this.
PRESIGN_EXPIRE_S = 7 * 24 * 3600


def _presigned_report_url(artifact_uri: Optional[str]) -> Optional[str]:
    """Best-effort S3 pre-signed GET URL for the uploaded report_detailed.html,
    so the Slack thread carries a clickable HTTPS link (the artifact_uri itself
    is an s3:// path, not openable in a browser). Returns None for non-S3
    storage or on any error; never raises."""
    if not artifact_uri or not artifact_uri.startswith("s3://"):
        return None
    try:
        import boto3
        from botocore.client import Config

        bucket, _, prefix = artifact_uri[len("s3://"):].partition("/")
        if not bucket or not prefix:
            return None
        key = f"{prefix.rstrip('/')}/report_detailed.html"

        # Sign in the bucket's own region: SigV4 rejects the URL if the
        # credential-scope region differs from the bucket's (e.g. signing
        # us-east-1 for a us-west-2 bucket). get_bucket_location is
        # region-agnostic; fall back to the env region if it is not permitted.
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        try:
            loc = boto3.client("s3", region_name="us-east-1").get_bucket_location(
                Bucket=bucket
            ).get("LocationConstraint")
            region = loc or "us-east-1"  # a null/empty constraint means us-east-1
        except Exception as exc:
            print(f"get_bucket_location failed, using region {region!r}: {exc}", file=sys.stderr)

        # SigV4 (the default can emit a legacy SigV2 URL that 400s outside
        # us-east-1's global endpoint).
        client = boto3.client(
            "s3", region_name=region, config=Config(signature_version="s3v4")
        )
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_EXPIRE_S,
        )
    except Exception as exc:
        print(f"Pre-signed report URL generation failed: {exc}", file=sys.stderr)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args, extra_args = base._parse_args(_inject_default_locustfile(argv))
    results_dir = args.results_root / base._timestamp()
    csv_prefix = results_dir / "run"
    html_path = results_dir / "report.html"
    stats_path = results_dir / "run_stats.csv"

    locust_cmd = base._build_locust_command(
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

    # Render report_detailed.html before uploading so it is part of the
    # artifact copy. Best-effort: never masks Locust's exit code.
    report_ok = False
    try:
        from detailed_report import generate_report

        generate_report(results_dir)
        report_ok = True
    except Exception as exc:
        print(f"Detailed report generation failed: {exc}", file=sys.stderr)

    artifact_uri = _upload_artifacts_detailed(results_dir)
    # Pre-signed link for the summary table's report row (clickable), only when
    # generate_report actually produced and uploaded the report.
    report_url = (
        _presigned_report_url(artifact_uri) if artifact_uri and report_ok else None
    )
    stats = base.parse_stats_csv(stats_path)
    ok = exit_code == 0
    text = base.build_slack_message(
        ok=ok,
        host=args.host,
        duration_s=duration_s,
        exit_code=exit_code,
        stats=stats,
        results_dir=results_dir,
        artifact_uri=artifact_uri,
        error=error,
        report_url=report_url,
    )
    # The literal must match build_slack_message()'s title or this replace
    # silently no-ops (the summary would keep the non-"Detailed" title).
    text = text.replace("*Locust load test", "*Detailed locust load test", 1)

    deployments = base.load_deployment_stats(
        results_dir / "run_deployments.json"
    ) or base.deployment_stats_from_csv(stats_path)
    replies = [
        base.build_criteria_reply(),
        base.build_percentiles_reply(deployments),
        base.build_ratio_reply(deployments),
    ]
    print("\n\n".join([text, *replies]))

    # Threaded posting (summary + replies) needs the Web API; incoming
    # webhooks return no message ts, so they cannot start a thread.
    token = os.environ.get("SLACK_BOT_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    posted = False
    if token and channel:
        posted = base.post_slack_thread(token, channel, text, replies, ok=ok)
    elif webhook:
        print(
            "SLACK_BOT_TOKEN/SLACK_CHANNEL unset; falling back to webhook "
            "(single message, no thread).",
            file=sys.stderr,
        )
    if not posted and webhook:
        base.post_slack(webhook, "\n\n".join([text, *replies]), ok=ok)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
