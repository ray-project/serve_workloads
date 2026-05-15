"""Smoke-test every HTTP route plus one unary gRPC call to the ``grpc-canary`` app.

Usage (from repo root with ``serve_validation`` importable)::

    python -m serve_validation.smoke_all <http_base> <grpc_host:port>

Exits 0 on success, 1 on any failed check, 2 on bad arguments.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import grpc
from ray.serve.generated import serve_pb2, serve_pb2_grpc


def _grpc_ok(host: str) -> tuple[bool, str]:
    try:
        ch = grpc.insecure_channel(host)
        grpc.channel_ready_future(ch).result(timeout=10.0)
        stub = serve_pb2_grpc.UserDefinedServiceStub(ch)
        req = serve_pb2.UserDefinedMessage(name="smoke", num=1, foo="test")
        metadata = (("application", "grpc-canary"),)
        stub.__call__(req, metadata=metadata, timeout=30.0)
        ch.close()
        return True, ""
    except Exception as e:
        return False, str(e)


def run_smoke(base: str, grpc_host: str) -> list[str]:
    base = base.rstrip("/")
    checks: list[tuple[str, str, bytes, dict]] = [
        ("GET", f"{base}/echo/", b"", {}),
        ("POST", f"{base}/nlp-chain/", b"smoke", {}),
        ("POST", f"{base}/batch-infer/", json.dumps({"name": "x"}).encode(), {"Content-Type": "application/json"}),
        (
            "POST",
            f"{base}/stream-chat/",
            json.dumps({"tokens": 5, "duration_s": 1.0}).encode(),
            {"Content-Type": "application/json"},
        ),
        (
            "POST",
            f"{base}/mux/",
            json.dumps({"q": "hi"}).encode(),
            {
                "Content-Type": "application/json",
                "serve_multiplexed_model_id": "0",
            },
        ),
        ("POST", f"{base}/image-dag/", b"", {}),
        ("POST", f"{base}/cpu-fanout/", b"smoke", {}),
        ("POST", f"{base}/mixed-preprocess/", b"smoke", {}),
        ("POST", f"{base}/heavy-payload/", json.dumps({"mb": 1.0}).encode(), {"Content-Type": "application/json"}),
        ("POST", f"{base}/long-runner/", json.dumps({"seconds": 3.0}).encode(), {"Content-Type": "application/json"}),
        ("GET", f"{base}/highscale/", b"", {}),
    ]
    failures: list[str] = []
    for method, url, body, headers in checks:
        try:
            req = urllib.request.Request(url, data=body if method != "GET" else None, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                if resp.status >= 400:
                    failures.append(f"{method} {url} -> HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            failures.append(f"{method} {url} -> HTTP {e.code} {e.reason}")
        except Exception as e:
            failures.append(f"{method} {url} -> {e!r}")

    ok, gerr = _grpc_ok(grpc_host)
    if not ok:
        failures.append(f"gRPC grpc-canary @ {grpc_host}: {gerr}")
    return failures


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python -m serve_validation.smoke_all <http_base> <grpc_host:port>", file=sys.stderr)
        return 2
    failures = run_smoke(sys.argv[1], sys.argv[2])
    if failures:
        print("SMOKE FAILURES:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("SMOKE OK: all HTTP routes + gRPC grpc-canary passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
