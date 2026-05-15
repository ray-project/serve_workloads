"""Simulated workloads and resource helpers (CPU sleep + memory; optional simulated_gpu)."""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any, Dict, List, Optional

# Request simulated_gpu on Ray actors only when the cluster exposes that custom resource
# (e.g. Anyscale cpu-gpu-sim worker group). Default off so CPU-only workers schedule.
# Set RAY_SERVE_SIMULATED_GPU=1 when compute_config tags nodes with simulated_gpu.
_USE_SIM_GPU = os.environ.get("RAY_SERVE_SIMULATED_GPU", "").lower() in ("1", "true", "yes")
# Legacy: strip simulated_gpu for local quick runs
_DEV = os.environ.get("RAY_SERVE_VALIDATION_DEV", "").lower() in ("1", "true", "yes")


def actor_options(
    *,
    num_cpus: float = 0.5,
    simulated_gpu: bool = False,
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {"num_cpus": num_cpus}
    if simulated_gpu and _USE_SIM_GPU and not _DEV:
        opts["resources"] = {"simulated_gpu": 1}
    return opts


async def sleep_ms(ms: float) -> None:
    await asyncio.sleep(ms / 1000.0)


async def simulate_short_cpu_ms(lo: float = 20.0, hi: float = 50.0) -> None:
    await sleep_ms(random.uniform(lo, hi))


async def simulate_encoder_ms() -> None:
    await sleep_ms(random.uniform(40.0, 120.0))


async def simulate_batch_infer_ms(batch: List[Any]) -> None:
    # Simulated GPU batch work scales slightly with batch size.
    base = random.uniform(15.0, 80.0)
    await sleep_ms(base + 2.0 * len(batch))


def alloc_buffer_mb(mb: float) -> bytearray:
    return bytearray(int(mb * 1024 * 1024))


def json_body_size_hint(request_body: bytes) -> Dict[str, Any]:
    try:
        return json.loads(request_body.decode("utf-8"))
    except Exception:
        return {}


def random_image_mb() -> float:
    return random.uniform(2.0, 10.0)


def random_heavy_mb() -> float:
    return random.uniform(5.0, 50.0)


def zipf_model_index(n_models: int = 20, s: float = 1.2) -> int:
    """Skew toward lower IDs (simple Zipf-like draw)."""
    ranks = [1.0 / (i**s) for i in range(1, n_models + 1)]
    t = sum(ranks)
    r = random.random() * t
    acc = 0.0
    for i, w in enumerate(ranks):
        acc += w
        if r <= acc:
            return i
    return n_models - 1
