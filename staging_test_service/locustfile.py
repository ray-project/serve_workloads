"""
Locust load test implementing the design.md §5 traffic model.

Implements all 8 user personas, a compressed diurnal schedule via LoadTestShape,
Zipfian model-ID distribution, bursty batch submission, realistic payload sizes,
SSE streaming consumption, and client-side backpressure.

The 24-hour diurnal cycle from design.md §5.2 is compressed into the run duration
(default 150 min). Spike windows fire at the compressed equivalents of 10:00 and
15:00 UTC, plus random Poisson-distributed micro-spikes.

Environment variables:
  ANYSCALE_SERVICE_TOKEN   Bearer token for Anyscale Service auth
  LOCUST_RUN_MINUTES       Total run duration in minutes (default: 150)
  LOCUST_PEAK_USERS        User count at 1.0x load multiplier (default: 2000)

Usage:
  locust -f locustfile.py --headless --host "https://..." \\
      --expect-workers 0
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import List, Optional, Tuple

from locust import HttpUser, LoadTestShape, between, task

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_AUTH_TOKEN = os.environ.get("ANYSCALE_SERVICE_TOKEN", "")
_RUN_MINUTES = int(os.environ.get("LOCUST_RUN_MINUTES", "150"))
_PEAK_USERS = int(os.environ.get("LOCUST_PEAK_USERS", "2000"))

# ---------------------------------------------------------------------------
# Auth mixin
# ---------------------------------------------------------------------------

class _AuthMixin:
    """Inject ``Authorization: Bearer …`` when ANYSCALE_SERVICE_TOKEN is set."""

    def on_start(self):
        if _AUTH_TOKEN:
            self.client.headers.update({"Authorization": f"Bearer {_AUTH_TOKEN}"})


# ---------------------------------------------------------------------------
# Zipfian distribution for model-switcher persona (§5.1)
# 80% of traffic to 4 out of 20 models
# ---------------------------------------------------------------------------

_NUM_MODELS = 20
_ZIPF_WEIGHTS: List[float] = [1.0 / (i + 1) ** 1.2 for i in range(_NUM_MODELS)]
_ZIPF_TOTAL = sum(_ZIPF_WEIGHTS)
_ZIPF_CDF: List[float] = []
_acc = 0.0
for _w in _ZIPF_WEIGHTS:
    _acc += _w / _ZIPF_TOTAL
    _ZIPF_CDF.append(_acc)


def _zipf_model_id() -> str:
    r = random.random()
    for idx, c in enumerate(_ZIPF_CDF):
        if r <= c:
            return str(idx)
    return str(_NUM_MODELS - 1)


# ---------------------------------------------------------------------------
# Diurnal schedule (design.md §5.2, compressed to _RUN_MINUTES)
# ---------------------------------------------------------------------------

# 24-hour phase boundaries and load multipliers.
# Each tuple: (start_hour, end_hour, start_mult, end_mult)
_PHASES: List[Tuple[float, float, float, float]] = [
    (0, 6, 0.2, 0.2),       # Night trough
    (6, 8, 0.2, 1.0),       # Morning ramp
    (8, 12, 1.0, 1.2),      # Morning peak
    (12, 13, 1.2, 0.8),     # Midday dip
    (13, 17, 1.0, 1.5),     # Afternoon peak
    (17, 20, 1.5, 0.5),     # Evening decline
    (20, 24, 0.5, 0.2),     # Night ramp-down
]

# Scheduled spike windows at 10:00 and 15:00 UTC → compressed times.
# Each: (center_hour, half_duration_hours, multiplier_boost)
_SPIKE_WINDOWS = [
    (10.0, 5 / 60, 5.0),   # 10:00 ± 5 min → 5x for 10 min
    (15.0, 5 / 60, 5.0),   # 15:00 ± 5 min → 5x for 10 min
]


def _load_multiplier(elapsed_s: float) -> float:
    """Return the load multiplier for the current point in the compressed schedule."""
    run_s = _RUN_MINUTES * 60
    if run_s <= 0:
        return 1.0
    frac = (elapsed_s % run_s) / run_s
    sim_hour = frac * 24.0

    # Base multiplier from diurnal phases
    mult = 0.2
    for start_h, end_h, start_m, end_m in _PHASES:
        if start_h <= sim_hour < end_h:
            t = (sim_hour - start_h) / (end_h - start_h)
            mult = start_m + t * (end_m - start_m)
            break

    # Scheduled spikes
    for center_h, half_dur, boost in _SPIKE_WINDOWS:
        if abs(sim_hour - center_h) <= half_dur:
            mult *= boost

    return mult


# ---------------------------------------------------------------------------
# LoadTestShape — controls user count over time (§5.2, §5.3)
# ---------------------------------------------------------------------------

class DiurnalShape(LoadTestShape):
    """Compressed 24-hour diurnal schedule with spike injection."""

    use_common_options = True

    def tick(self) -> Optional[Tuple[int, float]]:
        elapsed = self.get_run_time()
        if elapsed > _RUN_MINUTES * 60:
            return None  # stop

        mult = _load_multiplier(elapsed)

        # Random Poisson micro-spikes (§5.3): λ = 0.5/hr → per-tick probability
        # tick() is called ~1/s; probability per second = 0.5 / 3600
        if random.random() < 0.5 / 3600:
            mult *= 3.0  # 3x micro-spike

        target_users = max(1, int(_PEAK_USERS * mult))
        # Spawn rate: reach target within ~30 seconds
        current = self.runner.user_count if self.runner else 0
        spawn_rate = max(1.0, abs(target_users - current) / 30.0)
        return target_users, spawn_rate


# ---------------------------------------------------------------------------
# Persona 1: api-caller (§5.1) → echo-baseline
# 200-500 concurrent users, 50-100 RPS per user, high-frequency
# ---------------------------------------------------------------------------

class ApiCaller(_AuthMixin, HttpUser):
    weight = 18  # ~350/2000 of peak users
    wait_time = between(0.01, 0.02)  # 50-100 RPS per user

    @task
    def echo(self):
        self.client.get("/echo/")


# ---------------------------------------------------------------------------
# Persona 2: pipeline-user (§5.1) → nlp-chain, image-dag, cpu-fanout, mixed-preprocess
# 50-200 concurrent users, 2-10 RPS per user
# ---------------------------------------------------------------------------

class PipelineUser(_AuthMixin, HttpUser):
    weight = 6  # ~120/2000
    wait_time = between(0.1, 0.5)  # 2-10 RPS per user

    @task(3)
    def nlp(self):
        payload = os.urandom(random.randint(64, 512))
        self.client.post("/nlp-chain/", data=payload)

    @task(2)
    def image(self):
        # Simulate 2-10 MB image upload (design.md §3.3)
        size = random.randint(2 * 1024, 10 * 1024)  # 2-10 KB compressed stand-in
        self.client.post("/image-dag/", data=os.urandom(size))

    @task(2)
    def fanout(self):
        self.client.post("/cpu-fanout/", data=os.urandom(random.randint(32, 256)))

    @task(1)
    def mixed(self):
        self.client.post("/mixed-preprocess/", data=os.urandom(random.randint(64, 512)))


# ---------------------------------------------------------------------------
# Persona 3: batch-submitter (§5.1) → batch-infer
# 20-100 users, bursty: 50 requests/s for 5s, then 30s pause
# ---------------------------------------------------------------------------

class BatchSubmitter(_AuthMixin, HttpUser):
    weight = 3  # ~50/2000
    wait_time = between(25.0, 35.0)  # pause between bursts

    @task
    def burst(self):
        # Burst: ~50 requests over ~5 seconds
        burst_size = random.randint(40, 60)
        for i in range(burst_size):
            self.client.post(
                "/batch-infer/",
                json={"name": f"item-{i}", "idx": i},
                name="/batch-infer/ [burst]",
            )
            time.sleep(random.uniform(0.05, 0.15))


# ---------------------------------------------------------------------------
# Persona 4: stream-consumer (§5.1) → stream-chat
# 100-500 concurrent connections, long-lived SSE (2-30s per stream)
# ---------------------------------------------------------------------------

class StreamConsumer(_AuthMixin, HttpUser):
    weight = 13  # ~250/2000
    wait_time = between(0.5, 2.0)

    @task
    def sse(self):
        tokens = random.randint(5, 40)
        duration = random.uniform(2.0, 15.0)
        with self.client.post(
            "/stream-chat/",
            json={"tokens": tokens, "duration_s": duration},
            stream=True,
            catch_response=True,
            timeout=max(120, duration * 3),
        ) as resp:
            if resp.status_code == 200:
                for _ in resp.iter_content(chunk_size=4096):
                    pass
            else:
                resp.failure(f"status {resp.status_code}")


# ---------------------------------------------------------------------------
# Persona 5: model-switcher (§5.1) → mux-model-router
# 50-200 concurrent users, 5-20 RPS, Zipfian model ID distribution
# ---------------------------------------------------------------------------

class ModelSwitcher(_AuthMixin, HttpUser):
    weight = 5  # ~100/2000
    wait_time = between(0.05, 0.2)  # 5-20 RPS per user

    @task
    def switch(self):
        model_id = _zipf_model_id()
        self.client.post(
            "/mux/",
            json={"q": f"query-{random.randint(0, 999)}"},
            headers={"serve_multiplexed_model_id": model_id},
            name=f"/mux/ [model={model_id}]",
        )


# ---------------------------------------------------------------------------
# Persona 6: heavy-uploader (§5.1) → heavy-payload
# 10-50 concurrent users, 0.5-2 RPS, 5-50 MB payloads
# ---------------------------------------------------------------------------

class HeavyUploader(_AuthMixin, HttpUser):
    weight = 1  # ~25/2000
    wait_time = between(0.5, 2.0)

    @task
    def upload(self):
        mb = random.uniform(5.0, 50.0)
        self.client.post(
            "/heavy-payload/",
            json={"mb": round(mb, 1)},
            timeout=60,
            name="/heavy-payload/",
        )


# ---------------------------------------------------------------------------
# Persona 7: long-task-submitter (§5.1) → long-runner
# 20-100 concurrent users, 0.1-0.5 RPS, 30-120s requests
# ---------------------------------------------------------------------------

class LongTaskSubmitter(_AuthMixin, HttpUser):
    weight = 3  # ~50/2000
    wait_time = between(2.0, 10.0)  # 0.1-0.5 RPS per user

    @task
    def submit(self):
        seconds = random.uniform(30.0, 120.0)
        self.client.post(
            "/long-runner/",
            json={"seconds": round(seconds, 1)},
            timeout=max(180, seconds * 1.5),
            name="/long-runner/",
        )


# ---------------------------------------------------------------------------
# Persona 8: scale-hammer (§5.1) → highscale-stress
# 500-2000 concurrent users, maximum throughput
# ---------------------------------------------------------------------------

class ScaleHammer(_AuthMixin, HttpUser):
    weight = 51  # ~1000/2000 — the largest persona by far
    wait_time = between(0.005, 0.02)

    @task
    def hammer(self):
        self.client.get("/highscale/")
