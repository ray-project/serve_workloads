#!/usr/bin/env python3
"""Weekly version-upgrade job (design.md §7.2).

Deploys / updates the Anyscale Service using the same Ray Serve graph as local ``serve run``.
Requires the Anyscale CLI (``anyscale``) on PATH and an authenticated session.

The post-deploy check is version-aware: the service must reach RUNNING on a *new*
primary version with no canary in flight. A rollout that fails and is rolled back
lands back on the old primary version and is reported to Slack as a failure —
service state alone (``anyscale service wait -s RUNNING``) cannot tell the two apart.

Environment:
  ANYSCALE_SERVICE_CONFIG   Path to service YAML (default: anyscale_service.yaml)
  UPGRADE_WAIT_TIMEOUT_S   Seconds for the rollout to land on the new version (default: 1200)
  SLACK_WEBHOOK_URL        Optional; posts success / failure JSON
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

POLL_INTERVAL_S = 15

# States a rollout cannot recover from (see anyscale.service.models.ServiceState).
_TERMINAL_STATES = {"TERMINATING", "TERMINATED", "SYSTEM_FAILURE"}


def _post_slack(
    webhook: str,
    title: str,
    *,
    ok: bool,
    fields: list[tuple[str, str]] | None = None,
    detail: str | None = None,
) -> None:
    """Post a Block Kit message: colored bar, bold title, field grid, context line."""
    icon = ":white_check_mark:" if ok else ":x:"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{icon} *{title}*"}}
    ]
    if fields:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{name}*\n{value}"}
                    for name, value in fields
                ],
            }
        )
    if detail:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": detail}]}
        )
    def _post(payload: dict) -> None:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)

    try:
        _post(
            {
                "text": title,
                "attachments": [
                    {"color": "#2eb67d" if ok else "#e01e5a", "blocks": blocks}
                ],
            }
        )
    except Exception as exc:
        # A rejected blocks payload must never suppress the alert itself.
        print(f"Slack blocks post failed ({exc}); retrying as plain text", file=sys.stderr)
        lines = [title]
        lines += [f"{name}: {value}" for name, value in (fields or [])]
        if detail:
            lines.append(detail)
        try:
            _post(
                {
                    "text": "\n".join(lines),
                    "attachments": [{"color": "good" if ok else "danger"}],
                }
            )
        except Exception as exc2:
            print(f"Slack notification failed: {exc2}", file=sys.stderr)


def _console_link(status) -> str | None:
    host = os.environ.get("ANYSCALE_HOST", "").rstrip("/")
    service_id = getattr(status, "id", None) if status is not None else None
    if host and service_id:
        return f"{host}/services/{service_id}"
    return None


def _service_status(name: str):
    # Lazy import so --dry-run / --help work without the SDK installed.
    import anyscale

    return anyscale.service.status(name=name)


def _state_name(status) -> str:
    return str(getattr(status, "state", "UNKNOWN")).split(".")[-1].upper()


def _version_info(version) -> tuple[str | None, str | None]:
    """(id, display name) of a ServiceVersionStatus; tolerates dict or None."""
    if version is None:
        return None, None
    if isinstance(version, dict):
        return version.get("id"), version.get("name")
    return getattr(version, "id", None), getattr(version, "name", None)


def _image_of(version) -> str | None:
    if version is None:
        return None
    config = (
        version.get("config") if isinstance(version, dict)
        else getattr(version, "config", None)
    )
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get("image_uri")
    return getattr(config, "image_uri", None)


def _primary_version_before_deploy(name: str) -> tuple[str | None, str | None]:
    try:
        status = _service_status(name)
    except Exception as exc:  # first deploy, or transient API error
        print(f"No pre-deploy status for {name!r} ({exc}); treating as first deploy.")
        return None, None
    return _version_info(getattr(status, "primary_version", None))


def _wait_for_upgrade(name: str, old_primary_id: str | None, timeout_s: int):
    """Poll service status until the rollout lands.

    Returns (outcome, last_status, last_canary) with outcome one of:
      "upgraded"    — RUNNING on a new primary version, no canary in flight
      "rolled_back" — a rollout was observed but the service is back on the old primary
      "terminal"    — service entered a state it cannot roll out from
      "timeout"     — rollout did not settle within timeout_s
    last_canary is the display name of the most recently observed canary version.
    """
    deadline = time.monotonic() + timeout_s
    saw_rollout = False
    last_state = None
    last_canary = None
    status = None
    while time.monotonic() < deadline:
        try:
            status = _service_status(name)
        except Exception as exc:
            print(f"Status poll failed (will retry): {exc}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_S)
            continue
        state = _state_name(status)
        canary_id, canary_name = _version_info(getattr(status, "canary_version", None))
        primary_id, primary_name = _version_info(getattr(status, "primary_version", None))
        if canary_id is not None:
            last_canary = canary_name or canary_id
        if state != last_state:
            print(
                f"Service state: {state} "
                f"(primary={primary_name or primary_id}, canary={canary_name or canary_id})"
            )
            last_state = state
        if canary_id is not None or state in ("ROLLING_OUT", "ROLLING_BACK"):
            saw_rollout = True
        if state == "RUNNING" and canary_id is None:
            if primary_id is not None and primary_id != old_primary_id:
                return "upgraded", status, last_canary
            if saw_rollout:
                return "rolled_back", status, last_canary
            # Still the pre-rollout RUNNING snapshot (deploy submitted, rollout not
            # visible yet) — keep polling rather than declaring stale success.
        elif state in _TERMINAL_STATES:
            return "terminal", status, last_canary
        time.sleep(POLL_INTERVAL_S)
    return "timeout", status, last_canary


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
        help="Seconds for the service to reach RUNNING on the new version (default 20 min).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands only; do not execute.",
    )
    parser.add_argument(
        "--rollback-on-failure",
        action="store_true",
        help="If the rollout is still stuck at timeout, run anyscale service rollback (use with care).",
    )
    parser.add_argument(
        "--fast-rollout",
        action="store_true",
        help="Pass --canary-percent 100 to anyscale service deploy (skip gradual canary; for testing).",
    )
    parser.add_argument(
        "--max-surge-percent",
        type=int,
        default=None,
        metavar="PCT",
        help="Pass --max-surge-percent to anyscale service deploy (0-100): cap on the extra "
        "replica capacity allocated while the rollout is in flight.",
    )
    args = parser.parse_args()

    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    cfg = args.config_file.resolve()
    if not cfg.is_file():
        msg = f"Upgrade of {args.name!r} FAILED before deploy: config not found: {cfg}"
        print(msg, file=sys.stderr)
        if webhook:
            _post_slack(
                webhook,
                "Serve validation upgrade failed — config not found",
                ok=False,
                fields=[("Service", args.name)],
                detail=f"`{cfg}` does not exist in the job working directory.",
            )
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
    if args.max_surge_percent is not None:
        deploy_cmd.extend(["--max-surge-percent", str(args.max_surge_percent)])

    if args.dry_run:
        print("Would run:", subprocess.list2cmdline(deploy_cmd))
        print(
            f"Then poll anyscale.service.status({service_name!r}) until RUNNING on a "
            f"new primary version with no canary (timeout {args.wait_timeout_s}s)."
        )
        return 0

    old_primary_id, old_primary_name = _primary_version_before_deploy(service_name)
    print(f"Primary version before deploy: {old_primary_name or old_primary_id or '<none>'}")

    try:
        subprocess.run(deploy_cmd, check=True)
    except FileNotFoundError:
        msg = (
            f"Upgrade of {service_name!r} FAILED: Anyscale CLI not found on PATH. "
            "Install: https://docs.anyscale.com/reference/cli/"
        )
        print(msg, file=sys.stderr)
        if webhook:
            _post_slack(
                webhook,
                "Serve validation upgrade failed — Anyscale CLI missing",
                ok=False,
                fields=[("Service", service_name)],
                detail="`anyscale` was not found on PATH in the job environment.",
            )
        return 1
    except subprocess.CalledProcessError as e:
        msg = f"anyscale service deploy for {service_name!r} failed (exit {e.returncode})."
        print(msg, file=sys.stderr)
        if webhook:
            _post_slack(
                webhook,
                "Serve validation upgrade failed — deploy command error",
                ok=False,
                fields=[("Service", service_name), ("Exit code", str(e.returncode))],
                detail="`anyscale service deploy` exited non-zero before the rollout started; see the job logs.",
            )
        return 1

    outcome, status, failed_canary = _wait_for_upgrade(
        service_name, old_primary_id, args.wait_timeout_s
    )

    if outcome == "upgraded":
        new_primary = getattr(status, "primary_version", None)
        _, new_name = _version_info(new_primary)
        image = _image_of(new_primary)
        ok_msg = (
            f"Serve validation service {service_name!r} upgraded and RUNNING: "
            f"primary version {old_primary_name or '<none>'} -> {new_name}"
            + (f" (image {image})." if image else ".")
        )
        print(ok_msg)
        if webhook:
            link = _console_link(status)
            _post_slack(
                webhook,
                "Serve validation upgrade succeeded",
                ok=True,
                fields=[
                    ("Service", service_name),
                    ("State", "RUNNING"),
                    ("Version", f"{old_primary_name or '<none>'} → {new_name}"),
                    ("Image", image or "unknown"),
                ],
                detail=(
                    f"<{link}|View the service in the Anyscale console>" if link else None
                ),
            )
        return 0

    if outcome == "rolled_back":
        msg = (
            f"Upgrade of {service_name!r} FAILED: the rollout did not complete and was "
            f"rolled back — the service is RUNNING on the previous version "
            f"{old_primary_name or old_primary_id}. Check the service events in the "
            "Anyscale console for the failure reason."
        )
        slack_title = "Serve validation upgrade failed — rolled back"
        slack_fields = [
            ("Service", service_name),
            ("Still serving", f"{old_primary_name or old_primary_id} (previous version)"),
            ("Failed canary", failed_canary or "unknown"),
        ]
    elif outcome == "terminal":
        msg = (
            f"Upgrade of {service_name!r} FAILED: service entered state "
            f"{_state_name(status)} during the rollout."
        )
        slack_title = "Serve validation upgrade failed"
        slack_fields = [
            ("Service", service_name),
            ("Service state", _state_name(status)),
        ]
    else:  # timeout
        last = _state_name(status) if status is not None else "UNKNOWN"
        msg = (
            f"Upgrade of {service_name!r} did not land on a new version within "
            f"{args.wait_timeout_s}s (last state: {last})."
        )
        slack_title = "Serve validation upgrade timed out"
        slack_fields = [
            ("Service", service_name),
            ("Last state", last),
            ("Waited", f"{args.wait_timeout_s}s"),
        ]
        if args.rollback_on_failure:
            rb = ["anyscale", "service", "rollback", "-n", service_name]
            print("Running:", subprocess.list2cmdline(rb), file=sys.stderr)
            subprocess.run(rb, check=False)
            msg += " Rollback was triggered (--rollback-on-failure)."
            slack_fields.append(("Rollback", "triggered by --rollback-on-failure"))

    print(msg, file=sys.stderr)
    if webhook:
        link = _console_link(status)
        events = (
            f"<{link}|service events in the Anyscale console>"
            if link
            else "service events in the Anyscale console"
        )
        _post_slack(
            webhook,
            slack_title,
            ok=False,
            fields=slack_fields,
            detail=f"Check the {events} for the failure reason.",
        )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        crash_msg = f"Version-upgrade job crashed: {exc!r}"
        print(crash_msg, file=sys.stderr)
        crash_webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if crash_webhook:
            _post_slack(
                crash_webhook,
                "Serve validation upgrade job crashed",
                ok=False,
                detail=f"`{exc!r}` — see the job logs for the traceback.",
            )
        raise
