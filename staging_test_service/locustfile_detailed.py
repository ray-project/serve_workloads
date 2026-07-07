"""
Detailed variant of the locust load test: reuses the 8 personas and the
DiurnalShape from locustfile.py unchanged, and adds a master-only collector
greenlet. Every LOCUST_DETAILED_INTERVAL_S seconds it snapshots locust's
merged cumulative response-time histograms, diffs them against the previous
snapshot, and merges request names to deployment level, yielding exact
per-bucket percentiles with no per-request logging on the workers.

Files written next to the --csv artifacts (master or local runner only,
skipped when --csv is not set):
  <csv_prefix>_timeseries.csv  one row per deployment key per bucket
  <csv_prefix>_users.csv       user_count / target_user_count every 3s
  <csv_prefix>_meta.json       run parameters, consumed by detailed_report.py

Environment variables (in addition to locustfile.py's):
  LOCUST_DETAILED_INTERVAL_S   Bucket length in seconds (default: 15, min: 5)

Usage:
  locust -f locustfile_detailed.py --headless --host "https://..." --csv run
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import gevent
import locust
from locust import events
from locust.stats import calculate_response_time_percentile

# Star import re-exposes the 8 personas and DiurnalShape so locust discovers
# them from this module; it also re-registers locustfile's own listeners
# (run_deployments.json dump and 5xx capture), which is intended.
from locustfile import *
import locustfile as _base

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

try:
    _INTERVAL_S = max(5.0, float(os.environ.get("LOCUST_DETAILED_INTERVAL_S", "15")))
except ValueError:
    _INTERVAL_S = 15.0
_USERS_SAMPLE_S = 3.0
_PERCENTILES = (0.5, 0.90, 0.95, 0.99, 0.999)

_TIMESERIES_HEADER = [
    "ts_iso", "elapsed_s", "interval_s", "key", "requests", "failures",
    "rps", "avg_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms", "p999_ms",
    "max_ms",
]
_USERS_HEADER = ["ts_iso", "elapsed_s", "user_count", "target_user_count"]

# Measurement caveats surfaced through run_meta.json for the report.
_DEPLOYMENT_NOTES = {
    "/stream-chat/": "latency is time to response headers (TTFB); stream duration (2-15s by design) is excluded",
    "/batch-infer/": "includes up to 50ms server-side batch wait by design",
    "/long-runner/": "requests sleep 30-120s by design; high latency here is intrinsic",
    "/heavy-payload/": "response bodies are 5-50MB; latency is transfer dominated",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Bucket math. Snapshots are cumulative per request name; a bucket is the
# diff of consecutive snapshots, merged to deployment level at histogram
# level so per-bucket percentiles are exact (never averaged).
#
# Two invariants: (1) master stats are monotonic, which holds because the run
# never passes --reset-stats; (2) _cumulative_snapshot must stay yield-free (no
# gevent.sleep / socket I/O) so it cannot interleave with the master's stats
# aggregation greenlet mid-iteration.
# ---------------------------------------------------------------------------

def _cumulative_snapshot(stats):
    snap = {}
    for entry in stats.entries.values():
        if not entry.num_requests and not entry.num_failures:
            continue
        snap[entry.name] = (
            entry.num_requests,
            entry.num_failures,
            dict(entry.response_times),
            entry.total_response_time,
        )
    return snap


def _bucket_rows(prev, cur):
    """Group per-name cumulative snapshots to deployment level, diff vs
    prev, return {key: (requests, failures, hist, total_rt)}. Mux model
    names additionally keep their own key for drill-down."""
    grouped = {}
    for name, (n_req, n_fail, hist, total_rt) in cur.items():
        keys = [_base._deployment_of(name)]
        if name.startswith("/mux/ ["):
            keys.append(name)
        for key in keys:
            g = grouped.setdefault(key, [0, 0, {}, 0.0])
            g[0] += n_req
            g[1] += n_fail
            for rt, count in hist.items():
                g[2][rt] = g[2].get(rt, 0) + count
            g[3] += total_rt
    out = {}
    for key, (n_req, n_fail, hist, total_rt) in grouped.items():
        p_req, p_fail, p_hist, p_total = prev.get(key, (0, 0, {}, 0.0))
        d_hist = {}
        for rt, count in hist.items():
            d = count - p_hist.get(rt, 0)
            if d > 0:
                d_hist[rt] = d
        out[key] = (n_req - p_req, n_fail - p_fail, d_hist, total_rt - p_total)
    return out, grouped


# ---------------------------------------------------------------------------
# Collector: a background greenlet on the master (or local runner) samples
# user counts every 3s and takes a timeseries bucket every _INTERVAL_S.
# Best-effort throughout; a failure here must never fail the load test.
# ---------------------------------------------------------------------------

class _Collector:
    """State for the timeseries/users CSVs, created at test_start."""

    def __init__(self, environment, csv_prefix):
        self.environment = environment
        self.csv_prefix = csv_prefix
        self.t0 = time.monotonic()
        self.started_utc = _utc_now_iso()
        self.last_edge = 0.0  # elapsed seconds of the last bucket boundary
        self.prev = {}  # grouped cumulative snapshot at the last boundary
        self.seen_keys = set()  # deployment keys observed so far
        self.greenlet = None
        self.ts_file = open(f"{csv_prefix}_timeseries.csv", "w", newline="")
        self.ts_writer = csv.writer(self.ts_file)
        self.ts_writer.writerow(_TIMESERIES_HEADER)
        self.ts_file.flush()
        self.users_file = open(f"{csv_prefix}_users.csv", "w", newline="")
        self.users_writer = csv.writer(self.users_file)
        self.users_writer.writerow(_USERS_HEADER)
        self.users_file.flush()

    def run(self):
        while True:
            gevent.sleep(_USERS_SAMPLE_S)
            try:
                self._sample_users()
                if (time.monotonic() - self.t0) - self.last_edge >= _INTERVAL_S:
                    self._take_bucket()
            except Exception as e:
                print(f"detailed collector: {e!r}", file=sys.stderr, flush=True)

    def _sample_users(self):
        elapsed = time.monotonic() - self.t0
        runner = self.environment.runner
        self.users_writer.writerow([
            _utc_now_iso(),
            f"{elapsed:.1f}",
            getattr(runner, "user_count", ""),
            getattr(runner, "target_user_count", ""),
        ])
        self.users_file.flush()

    def _take_bucket(self):
        elapsed = time.monotonic() - self.t0
        interval = elapsed - self.last_edge
        cur = _cumulative_snapshot(self.environment.stats)
        diffs, grouped = _bucket_rows(self.prev, cur)
        self.prev = grouped
        self.last_edge = elapsed
        ts_iso = _utc_now_iso()
        for key in diffs:
            if not key.startswith("/mux/ ["):
                self.seen_keys.add(key)
        # Every deployment seen so far gets a row, zero-request buckets included.
        for key in sorted(self.seen_keys):
            self._write_row(
                ts_iso, elapsed, interval, key, diffs.get(key, (0, 0, {}, 0.0))
            )
        # Mux model drill-down keys: rows only when active in this bucket.
        for key in sorted(diffs):
            if key.startswith("/mux/ [") and (diffs[key][0] or diffs[key][1]):
                self._write_row(ts_iso, elapsed, interval, key, diffs[key])
        self.ts_file.flush()

    def _write_row(self, ts_iso, elapsed, interval, key, row):
        n_req, n_fail, d_hist, d_total_rt = row
        # Histogram total: the correct percentile denominator, and equal to the
        # num_requests diff while every request is timed (HttpUser always is).
        n_timed = sum(d_hist.values())
        if n_timed:
            avg = f"{d_total_rt / n_timed:.1f}"
            pcts = [
                calculate_response_time_percentile(d_hist, n_timed, p)
                for p in _PERCENTILES
            ]
            max_ms = max(d_hist)
        else:
            avg = ""
            pcts = [""] * len(_PERCENTILES)
            max_ms = ""
        self.ts_writer.writerow([
            ts_iso,
            f"{elapsed:.1f}",
            f"{interval:.1f}",
            key,
            n_req,
            n_fail,
            f"{n_req / interval:.3f}",
            avg,
            *pcts,
            max_ms,
        ])

    def finalize(self):
        try:
            if (time.monotonic() - self.t0) - self.last_edge >= 1.0:
                self._take_bucket()
        except Exception as e:
            print(f"detailed collector final bucket: {e!r}", file=sys.stderr, flush=True)
        try:
            self._write_meta()
        except Exception as e:
            print(f"detailed collector meta: {e!r}", file=sys.stderr, flush=True)
        for fh in (self.ts_file, self.users_file):
            try:
                fh.close()
            except Exception:
                pass

    def _write_meta(self):
        run_s = _base._RUN_MINUTES * 60
        spikes = [
            {
                "start": round((center_h - half_h) / 24.0 * run_s, 1),
                "end": round((center_h + half_h) / 24.0 * run_s, 1),
                "boost": boost,
            }
            for center_h, half_h, boost in _base._SPIKE_WINDOWS
        ]
        meta = {
            "host": self.environment.host,
            "started_utc": self.started_utc,
            "finished_utc": _utc_now_iso(),
            "run_minutes": _base._RUN_MINUTES,
            "peak_users": _base._PEAK_USERS,
            "max_spawn_rate": _base._MAX_SPAWN_RATE,
            "interval_s": _INTERVAL_S,
            "users_sample_s": _USERS_SAMPLE_S,
            "locust_version": locust.__version__,
            "phases": _base._PHASES,
            "spike_windows_elapsed_s": spikes,
            "deployment_notes": _DEPLOYMENT_NOTES,
        }
        path = f"{self.csv_prefix}_meta.json"
        with open(path, "w") as fh:
            json.dump(meta, fh, indent=2)
        print(f"Wrote detailed run metadata to {path}", flush=True)


_collector: Optional[_Collector] = None


@events.test_start.add_listener
def _start_detailed_collector(environment, **kwargs):
    from locust.runners import WorkerRunner

    global _collector
    if isinstance(environment.runner, WorkerRunner):
        return  # only the master has the merged stats
    csv_prefix = getattr(environment.parsed_options, "csv_prefix", None)
    if not csv_prefix:
        print("detailed collector: --csv not set, timeseries disabled", flush=True)
        return
    try:
        _collector = _Collector(environment, csv_prefix)
        _collector.greenlet = gevent.spawn(_collector.run)
    except Exception as e:
        _collector = None
        print(f"detailed collector start failed: {e!r}", file=sys.stderr, flush=True)


@events.test_stop.add_listener
def _finalize_detailed_collector(environment, **kwargs):
    from locust.runners import WorkerRunner

    global _collector
    if isinstance(environment.runner, WorkerRunner):
        return
    col = _collector
    _collector = None
    if col is None:
        return
    try:
        if col.greenlet is not None:
            col.greenlet.kill(block=False)
    except Exception as e:
        print(f"detailed collector kill failed: {e!r}", file=sys.stderr, flush=True)
    try:
        col.finalize()
    except Exception as e:
        print(f"detailed collector finalize failed: {e!r}", file=sys.stderr, flush=True)
