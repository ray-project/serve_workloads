"""Autoscaling presets and replica budget validation (must sum to 4,096)."""

from __future__ import annotations

from ray.serve.config import AutoscalingConfig

# Stable baseline (echo, grpc-canary, mux)
AUTOSCALE_STABLE = AutoscalingConfig(
    min_replicas=1,
    max_replicas=1,  # overridden per deployment
    target_ongoing_requests=5,
    upscale_delay_s=0,
    look_back_period_s=2,
    downscale_delay_s=300,
    upscaling_factor=3.0,
    downscaling_factor=0.5,
)

AUTOSCALE_STABLE_MUX_WORKER = AutoscalingConfig(
    min_replicas=1,
    max_replicas=1,  # overridden per deployment
    target_ongoing_requests=10,
    upscale_delay_s=0,
    look_back_period_s=2,
    downscale_delay_s=300,
    upscaling_factor=4.0,
    downscaling_factor=0.5,
)

# Steady growth (nlp-chain, heavy-payload)
AUTOSCALE_GROWTH = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=3,
    upscale_delay_s=0,
    look_back_period_s=2,
    downscale_delay_s=300,
    downscale_to_zero_delay_s=300,
)

# Steady decline (image-dag, long-runner)
AUTOSCALE_DECLINE = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=3,
    upscale_delay_s=2,
    look_back_period_s=4,
    downscale_delay_s=300,
    downscale_to_zero_delay_s=300,
)

# Spiky (batch-infer, cpu-fanout, highscale-stress)
AUTOSCALE_SPIKY = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=1,
    upscale_delay_s=0,
    look_back_period_s=1,
    upscaling_factor=3.0,
    downscale_delay_s=300,
    downscale_to_zero_delay_s=300,
)

# Diurnal (stream-chat, mixed-preprocess)
AUTOSCALE_DIURNAL = AutoscalingConfig(
    min_replicas=1,
    max_replicas=1,
    target_ongoing_requests=3,
    # Fast scale-from-zero (see AUTOSCALE_GROWTH).
    upscale_delay_s=0,
    look_back_period_s=2,
    upscaling_factor=3.0,
    downscaling_factor=0.5,
    downscale_delay_s=300,
)

# long-runner: longer downscale_to_zero
AUTOSCALE_LONG_RUNNER = AutoscalingConfig(
    min_replicas=0,
    max_replicas=1,
    target_ongoing_requests=2,
    upscale_delay_s=2,
    look_back_period_s=4,
    downscale_delay_s=300,
    downscale_to_zero_delay_s=600,
)


# highscale-stress: keep target=1 (1 replica per in-flight request, so it still
# scales to its 1536 max under load); a small min floor keeps the always-on N=1
# baseline pinned at 2 replicas instead of oscillating at the target=1 boundary.
AUTOSCALE_HIGHSCALE = AUTOSCALE_SPIKY.copy(
    update={
        "min_replicas": 2,
        # Autoscaling experiment: faster upscale, gentler downscale.
        "upscaling_factor": 3.0,
        "downscaling_factor": 0.5,
        "target_ongoing_requests": 4,
    }
)

# batch-infer + cpu-fanout: raise target to 2 so the N=1 baseline settles at a
# single replica with margin (no boundary churn). Halves their under-load scale,
# which is fine -- they are not the scale-to-1536 deployment.
AUTOSCALE_SPIKY_T2 = AUTOSCALE_SPIKY.copy(
    update={
        "target_ongoing_requests": 2,
        # Fast scale-from-zero (see AUTOSCALE_GROWTH).
        "upscale_delay_s": 0,
        "look_back_period_s": 2,
        "upscaling_factor": 2.0,
        "downscaling_factor": 0.5,
    }
)

# heavy-payload: the pinger paces heavy to ~2 req/s (a 1 MB canary), so its ongoing
# requests sit near zero and the autoscaler would scale it to zero and oscillate
# 0<->1. Pin a floor of 1 to keep it warm. (locust still drives 5-50 MB at load.)
AUTOSCALE_HEAVY = AUTOSCALE_GROWTH.copy(
    update={
        "min_replicas": 1,
        # Autoscaling experiment: faster upscale, gentler downscale.
        "upscaling_factor": 3.0,
        "downscaling_factor": 0.5,
        "target_ongoing_requests": 2,
    }
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
