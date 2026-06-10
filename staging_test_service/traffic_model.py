"""Shared traffic model for the serve-validation service.

Single source of truth for the request mix, payloads, and headers. Imported by
BOTH locustfile.py (periodic spike driver) and baseline_pinger.py (always-on
baseline driver) so the two never drift.

Pure stdlib only — must import cleanly in both the Locust and Ray Serve
runtimes (no ``locust``, no ``ray`` imports here).
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Zipfian model-id distribution for the mux endpoint (design.md §5.1)
# ---------------------------------------------------------------------------
_NUM_MODELS = 20
_ZIPF_CDF: List[float] = []
_acc = 0.0
_total = sum(1.0 / (i + 1) ** 1.2 for i in range(_NUM_MODELS))
for _i in range(_NUM_MODELS):
    _acc += (1.0 / (_i + 1) ** 1.2) / _total
    _ZIPF_CDF.append(_acc)


def zipf_model_id() -> str:
    r = random.random()
    for idx, c in enumerate(_ZIPF_CDF):
        if r <= c:
            return str(idx)
    return str(_NUM_MODELS - 1)


# ---------------------------------------------------------------------------
# Payload / header factories (fresh value per call, mirrors locustfile.py)
# ---------------------------------------------------------------------------
def nlp_payload() -> bytes:
    return os.urandom(random.randint(64, 512))


def image_payload() -> bytes:
    return os.urandom(random.randint(2 * 1024, 10 * 1024))


def fanout_payload() -> bytes:
    return os.urandom(random.randint(32, 256))


def mixed_payload() -> bytes:
    return os.urandom(random.randint(64, 512))


def mux_payload() -> dict:
    return {"q": f"query-{random.randint(0, 999)}"}


def mux_headers() -> Dict[str, str]:
    return {"serve_multiplexed_model_id": zipf_model_id()}


def stream_payload() -> dict:
    return {"tokens": random.randint(5, 40), "duration_s": round(random.uniform(2.0, 15.0), 1)}


def batch_payload() -> dict:
    i = random.randint(0, 59)
    return {"name": f"item-{i}", "idx": i}


def heavy_payload() -> dict:
    return {"mb": round(random.uniform(5.0, 50.0), 1)}


def long_payload() -> dict:
    return {"seconds": round(random.uniform(30.0, 120.0), 1)}


# ---------------------------------------------------------------------------
# Endpoint catalog
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str                       # "GET" | "POST"
    path: str                         # route prefix on the service
    weight: float                     # share of request volume (need not sum to 1)
    json_factory: Optional[Callable[[], dict]] = None
    data_factory: Optional[Callable[[], bytes]] = None
    headers_factory: Optional[Callable[[], Dict[str, str]]] = None
    # Client-side total timeout for ONE request attempt. ~2x worst healthy
    # latency: a stuck request must release its in-flight slot quickly, or
    # rate generators stall on it. long covers the 125s max sleep + cold
    # start while staying under the proxy's request_timeout_s=180.
    timeout_s: float = 10.0


# Weights = the request-level mix derived from locustfile.py personas:
#   share ~= persona_user_weight / mean(wait_time) x task_sub_weight
# Worked out, highscale + echo dominate (~98%); the slow personas are a thin
# tail by design (this is a *realistic baseline*, not endpoint coverage).
# gRPC-canary is omitted — HTTP-only for v1; Locust still covers gRPC.
ENDPOINTS: List[Endpoint] = [
    Endpoint("highscale", "GET",  "/highscale/",       76.0),
    Endpoint("echo",      "GET",  "/echo/",            22.0),
    Endpoint("mux",       "POST", "/mux/",              0.75, json_factory=mux_payload, headers_factory=mux_headers),
    Endpoint("stream",    "POST", "/stream-chat/",      0.20, json_factory=stream_payload, timeout_s=30.0),
    Endpoint("nlp",       "POST", "/nlp-chain/",        0.14, data_factory=nlp_payload),
    Endpoint("image",     "POST", "/image-dag/",        0.09, data_factory=image_payload),
    Endpoint("fanout",    "POST", "/cpu-fanout/",       0.09, data_factory=fanout_payload),
    Endpoint("batch",     "POST", "/batch-infer/",      0.08, json_factory=batch_payload),
    Endpoint("mixed",     "POST", "/mixed-preprocess/", 0.05, data_factory=mixed_payload),
    Endpoint("heavy",     "POST", "/heavy-payload/",    0.015, json_factory=heavy_payload, timeout_s=60.0),
    Endpoint("long",      "POST", "/long-runner/",      0.009, json_factory=long_payload, timeout_s=150.0),
]


def normalized_weights(endpoints: List[Endpoint] = ENDPOINTS) -> List[float]:
    total = sum(e.weight for e in endpoints)
    return [e.weight / total for e in endpoints]


def choose_endpoint(rng: random.Random, endpoints: List[Endpoint] = ENDPOINTS) -> Endpoint:
    return rng.choices(endpoints, weights=[e.weight for e in endpoints], k=1)[0]


@dataclass
class BuiltRequest:
    method: str
    url: str
    json: Optional[dict] = None
    data: Optional[bytes] = None
    headers: Dict[str, str] = field(default_factory=dict)


def build_request(endpoint: Endpoint, base_url: str) -> BuiltRequest:
    return BuiltRequest(
        method=endpoint.method,
        url=base_url.rstrip("/") + endpoint.path,
        json=endpoint.json_factory() if endpoint.json_factory else None,
        data=endpoint.data_factory() if endpoint.data_factory else None,
        headers=endpoint.headers_factory() if endpoint.headers_factory else {},
    )
