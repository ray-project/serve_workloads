"""Autoscaling presets and replica budget validation (must sum to 4,096)."""

from __future__ import annotations

from ray.serve.config import AutoscalingConfig

# Stable baseline (echo, grpc-canary, mux)
AUTOSCALE_STABLE = AutoscalingConfig(
    min_replicas=1,
    max_replicas=1,  # overridden per deployment
    target_ongoing_requests=5,
    upscale_delay_s=30,
    downscale_delay_s=120,
    downscale_to_zero_delay_s=300,
)

# Steady growth (nlp-chain, heavy-payload)
AUTOSCALE_GROWTH = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=3,
    upscale_delay_s=15,
    downscale_delay_s=300,
    downscale_to_zero_delay_s=300,
)

# Steady decline (image-dag, long-runner)
AUTOSCALE_DECLINE = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=3,
    upscale_delay_s=20,
    downscale_delay_s=120,
    downscale_to_zero_delay_s=300,
)

# Spiky (batch-infer, cpu-fanout, highscale-stress)
AUTOSCALE_SPIKY = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=1,
    upscale_delay_s=5,
    downscale_delay_s=60,
    downscale_to_zero_delay_s=180,
)

# Diurnal (stream-chat, mixed-preprocess)
AUTOSCALE_DIURNAL = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=2,
    upscale_delay_s=10,
    downscale_delay_s=180,
    downscale_to_zero_delay_s=300,
)

# long-runner: longer downscale_to_zero
AUTOSCALE_LONG_RUNNER = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=2,
    upscale_delay_s=20,
    downscale_delay_s=120,
    downscale_to_zero_delay_s=600,
)


def _with_max(base: AutoscalingConfig, max_replicas: int) -> AutoscalingConfig:
    return base.copy(update={"max_replicas": max_replicas})


# Peak max_replicas per deployment (design Section 2).
REPLICA_BUDGET: dict[str, int] = {
    "echo": 64,
    "nlp-tokenizer": 80,
    "nlp-encoder": 160,
    "nlp-postprocessor": 80,
    "batch-infer": 256,
    "stream-chat": 512,
    "mux-ingress": 64,
    "mux-model-worker": 64,
    "image-dag-ingest": 60,
    "image-dag-resize": 60,
    "image-dag-classify": 60,
    "image-dag-annotate": 60,
    "cpu-fanout-router": 64,
    "cpu-fanout-worker-0": 64,
    "cpu-fanout-worker-1": 64,
    "cpu-fanout-worker-2": 64,
    "cpu-fanout-worker-3": 64,
    "cpu-fanout-agg": 64,
    "mixed-preprocess-cpu": 128,
    "mixed-preprocess-gpu": 64,
    "heavy-payload": 128,
    "long-runner": 256,
    "highscale-stress": 1536,
    "grpc-canary": 80,
}


def validate_replica_budget() -> None:
    total = sum(REPLICA_BUDGET.values())
    if total != 4096:
        raise RuntimeError(
            f"Replica budget must be 4096 (design Section 2); got {total}."
        )


validate_replica_budget()
