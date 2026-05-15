#!/usr/bin/env python3
"""Weekly version-upgrade job (design.md §7.2).

Deploys / updates the Anyscale Service using the same Ray Serve graph as local ``serve run``.
Requires the Anyscale CLI (``anyscale``) on PATH and an authenticated session.

Environment:
  ANYSCALE_SERVICE_CONFIG   Path to service YAML (default: anyscale_service.yaml)
  UPGRADE_WAIT_TIMEOUT_S   Seconds for ``anyscale service wait`` (default: 1200)
  SLACK_WEBHOOK_URL        Optional; posts success / failure JSON
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _post_slack(webhook: str, text: str, ok: bool) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy or update the Serve validation Anyscale Service."
    )
    default_cfg = os.environ.get("ANYSCALE_SERVICE_CONFIG", "anyscale_service.yaml")
    parser.add_argument(
        "--config-file",
        "-f",
        default=default_cfg,
        type=Path,
        help="Anyscale service YAML (image_uri, working_dir, compute_config, applications).",
    )
    parser.add_argument(
        "--name",
        "-n",
        default="serve-validation",
        help="Service name (default: serve-validation).",
    )
    parser.add_argument(
        "--wait-timeout-s",
        type=int,
        default=int(os.environ.get("UPGRADE_WAIT_TIMEOUT_S", "1200")),
        help="Timeout for anyscale service wait (default 20 min).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands only; do not execute.",
    )
    parser.add_argument(
        "--rollback-on-failure",
        action="store_true",
        help="After a failed wait, run anyscale service rollback (use with care).",
    )
    parser.add_argument(
        "--fast-rollout",
        action="store_true",
        help="Pass --canary-percent 100 to anyscale service deploy (skip gradual canary; for testing).",
    )
    args = parser.parse_args()

    cfg = args.config_file.resolve()
    if not cfg.is_file():
        print(f"Config not found: {cfg}", file=sys.stderr)
        return 1

    service_name = args.name

    deploy_cmd = [
        "anyscale",
        "service",
        "deploy",
        "-f",
        str(cfg),
    ]
    if args.fast_rollout:
        deploy_cmd.extend(["--canary-percent", "100"])

    wait_cmd = [
        "anyscale",
        "service",
        "wait",
        "-n",
        service_name,
        "-s",
        "RUNNING",
        "-t",
        str(args.wait_timeout_s),
    ]

    if args.dry_run:
        print("Would run:", subprocess.list2cmdline(deploy_cmd))
        print("Then:", subprocess.list2cmdline(wait_cmd))
        return 0

    try:
        subprocess.run(deploy_cmd, check=True)
    except FileNotFoundError:
        print(
            "Anyscale CLI not found. Install: https://docs.anyscale.com/reference/cli/",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as e:
        msg = f"anyscale service deploy failed (exit {e.returncode})."
        print(msg, file=sys.stderr)
        if os.environ.get("SLACK_WEBHOOK_URL"):
            _post_slack(os.environ["SLACK_WEBHOOK_URL"], msg, ok=False)
        return 1

    try:
        subprocess.run(wait_cmd, check=True)
    except subprocess.CalledProcessError:
        msg = (
            f"Service {service_name!r} did not reach RUNNING within {args.wait_timeout_s}s."
        )
        print(msg, file=sys.stderr)
        if args.rollback_on_failure:
            rb = ["anyscale", "service", "rollback", "-n", service_name]
            print("Running:", subprocess.list2cmdline(rb), file=sys.stderr)
            subprocess.run(rb, check=False)
        if os.environ.get("SLACK_WEBHOOK_URL"):
            _post_slack(os.environ["SLACK_WEBHOOK_URL"], msg, ok=False)
        return 1

    ok_msg = f"Serve validation service {service_name!r} updated and RUNNING."
    print(ok_msg)
    if os.environ.get("SLACK_WEBHOOK_URL"):
        _post_slack(os.environ["SLACK_WEBHOOK_URL"], ok_msg, ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
