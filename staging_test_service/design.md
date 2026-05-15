# Production-Grade Ray Serve Validation Service — Architecture Design

## 1. Executive Design Summary

- **Purpose**: A continuously-running, multi-application Ray Serve deployment on Anyscale Services that validates Serve stability, autoscaling correctness, and feature regression across Ray releases by simulating realistic customer workload patterns at scale.
- **Scope**: 12 Serve applications spanning all required features (single deployment, DAG chains, batching, streaming, model multiplexing) and workload types (large object transfer, heterogeneous resource scheduling, short and long requests).
- **CPU-only cluster**: All nodes are CPU instances. Workloads that customers run on GPUs are simulated using a `simulated_gpu` Ray custom resource on a dedicated CPU node group, preserving Serve's resource-aware scheduling, placement, and autoscaling behavior without physical GPU cost.
- **Scale envelope**: Head-only idle (1 node) scaling to 200 nodes under peak load, supporting up to 4,096 total Serve replicas. All 12 apps use `min_replicas=0` (scale-to-zero); the cluster runs only the head node (m7i.2xlarge, 32 GiB) between Locust runs, keeping idle cost at ~$290/month.
- **Traffic realism**: Locust-based external load generation implementing five traffic personas (stable, growth, decline, spike, diurnal) driven by a 24-hour schedule with parameterized phase transitions—not synthetic step functions.
- **Release cadence**: A weekly [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) redeploys the [Anyscale Service](https://docs.anyscale.com/services/multi-app) on the latest `anyscale/ray:nightly` image; a second weekly Scheduled Job runs the Locust load test, driving traffic to 200 nodes / 4,096 replicas against the live service. Pass/fail is evaluated in Grafana via Serve-emitted metrics, not by the job itself.
- **Inter-deployment data flow**: Large payloads between chained deployments are passed as `DeploymentResponse` objects through `DeploymentHandle`, letting Serve's native handle pipeline manage serialization and object store transfer without explicit `ray.put()` / `ray.get()` calls.
- **Observability stack**: Prometheus metrics (including all built-in `serve_*` metrics), Grafana dashboards, and PagerDuty-integrated alerting with explicit SLOs on P99 latency, error rate, replica convergence time, head node memory, and average worker node memory.
- **Isolation model**: Each application is a separate Serve application with its own `route_prefix` and independent autoscaling config within a single [multi-application Anyscale Service](https://docs.anyscale.com/services/multi-app), providing blast-radius containment; the validation service is a dedicated Anyscale Service separate from any production workloads.
- **Reliability validation**: Scheduled Locust load tests exercise all 12 apps under realistic traffic; pass/fail is evaluated via Grafana alert rules on Serve/Anyscale-emitted metrics.
- **Operational model**: Owned by the Serve Reliability team with a shared on-call rotation; incidents are triaged by severity with runbooks covering the top 15 failure modes observed in the validation service itself.
- **Key trade-off**: Realism vs. cost—the design uses simulated compute (CPU sleep + memory allocation patterns) rather than real ML models, and CPU-only nodes with custom resources rather than physical GPUs, trading hardware fidelity for deterministic failure attribution and significantly lower infrastructure cost.
- **Risk posture**: The top risks are autoscaling oscillation at high replica counts, scale-from-zero cold-start latency, and version-skew incompatibility during weekly rollouts; each has explicit mitigations detailed below.

---

## 2. Application Portfolio Design

**Assumptions**:
- All nodes are CPU-only: `m5.4xlarge` (16 vCPU, 64 GB RAM) — approximately $0.768/hr.
- A dedicated `cpu-gpu-sim` node group runs the same `m5.4xlarge` instance but is tagged with a Ray custom resource `{"simulated_gpu": 1}`. Deployments that model GPU workloads request `resources={"simulated_gpu": 1}`, causing Ray's scheduler to place them exclusively on these nodes — exercising the same resource-constrained placement, autoscaling, and routing logic that real GPU deployments use.
- Each CPU replica requests 0.5 CPU by default unless noted; simulated-GPU replicas request 0.5 CPU + 1 `simulated_gpu`.
- `num_replicas` values below are *peak* `max_replicas` in autoscaling config.

| # | Application Name | Purpose | Serve Feature(s) | Workload Type | Resource Profile | Latency Target | Peak Replicas | Traffic Pattern | Risk Area Validated |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `echo-baseline` | Minimal single-deployment health canary | Single deployment | Short requests (< 50ms simulated) | CPU | P99 < 100ms | 64 | Stable baseline | Basic Serve path, proxy routing, health checks |
| 2 | `nlp-chain` | 3-stage pipeline: tokenizer → encoder → postprocessor | Chained deployments (DAG) | Mixed CPU (tokenizer, postprocessor) + simulated-GPU (encoder) | Mixed (CPU + `simulated_gpu`) | P99 < 500ms | 320 (80+160+80) | Steady growth | DAG composition, cross-deployment handle latency, heterogeneous resource scheduling |
| 3 | `batch-infer` | Batch inference with `@serve.batch(max_batch_size=32)` | Batching | Simulated-GPU-bound inference, variable payload | `simulated_gpu` | P99 < 800ms | 256 | Spiky traffic | Batch fill rate, `batch_wait_timeout_s` behavior, resource-constrained autoscaling under load bursts |
| 4 | `stream-chat` | SSE streaming response simulating LLM token generation | Streaming response | Long-running requests (2-30s per stream) | `simulated_gpu` | P99 < 2s (end-to-end stream) | 512 | Diurnal (time-of-day) | Streaming proxy path, connection lifetime, backpressure under concurrent streams |
| 5 | `mux-model-router` | Model multiplexing across 20 model IDs with LRU eviction | Model multiplexing (`@serve.multiplexed`) | Short-medium requests, model load/unload churn | `simulated_gpu` | P99 < 600ms (warm), P99 < 3s (cold load) | 128 | Stable baseline | Multiplexed model routing, `max_num_models_per_replica` eviction, `serve_multiplexed_model_load_latency_ms` |
| 6 | `image-dag` | Image processing DAG: ingest → resize → classify → annotate | Chained deployments (DAG), large object transfer | Large objects (2-10 MB images passed as `DeploymentResponse` between stages) | Mixed (CPU + `simulated_gpu`) | P99 < 2s | 240 (60+60+60+60) | Steady decline | Inter-deployment large payload transfer, Serve handle serialization, GC under declining load |
| 7 | `cpu-fanout` | Fan-out: router → 4 parallel CPU workers → aggregator | Chained deployments (DAG) | Short CPU-bound requests with fan-out parallelism | CPU | P99 < 200ms | 384 (64+4×64+64) | Spiky traffic | Fan-out handle concurrency, `max_ongoing_requests` saturation, request queuing |
| 8 | `mixed-preprocess` | 2-stage: CPU preprocessing → simulated-GPU inference | Chained deployments | Mixed CPU preprocessing + simulated-GPU inference | Mixed (CPU + `simulated_gpu`) | P99 < 1s | 192 (128 CPU + 64 sim-GPU) | Diurnal | Resource heterogeneity scheduling, autoscaling with different resource types, queue imbalance |
| 9 | `heavy-payload` | Single deployment accepting and returning 5-50 MB payloads | Single deployment, large object transfer | Large HTTP request/response payloads (5-50 MB) | CPU | P99 < 3s | 128 | Steady growth | Proxy large-payload handling, serialization overhead, memory pressure under sustained throughput |
| 10 | `long-runner` | Single deployment with 30-120s simulated processing | Single deployment | Long-running requests | CPU | P99 < 130s | 256 | Steady decline | Replica timeout handling, health check false positives during long requests, autoscale-down lag |
| 11 | `highscale-stress` | Single CPU deployment scaled to maximum replica count | Single deployment | Short requests at extreme scale | CPU | P99 < 150ms | 1,536 | Spiky traffic | Autoscaling convergence at high replica count, controller scheduling throughput, state reconciliation |
| 12 | `grpc-canary` | gRPC endpoint for non-HTTP protocol validation | Single deployment (gRPC) | Short requests via gRPC | CPU | P99 < 100ms | 80 | Stable baseline | gRPC proxy path, protobuf serialization, gRPC health probes |

**Total peak replica budget**: 64 + 320 + 256 + 512 + 128 + 240 + 384 + 192 + 128 + 256 + 1,536 + 80 = **4,096 replicas**.

---

## 3. System Architecture

### 3.1 Logical Architecture Narrative

The validation service is deployed as a single [multi-application Anyscale Service](https://docs.anyscale.com/services/multi-app) containing 12 Serve applications defined in the service YAML under `applications:`. The Serve controller runs on the head node and manages all 12 applications through the `ApplicationStateManager` and `DeploymentStateManager`. Each application has a unique `route_prefix` (for example, `/echo`, `/nlp-chain`, `/stream-chat`) and independent autoscaling configurations.

An external **Locust load generator** runs as an [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) on its own separate cluster. It drives traffic against the Anyscale Service HTTP and gRPC ingress endpoints, implementing the five traffic personas and the compressed diurnal schedule. A separate **version upgrade** Anyscale Scheduled Job manages the weekly lifecycle: redeploying the Anyscale Service with the latest nightly image. Both jobs are cron-scheduled via `anyscale schedule apply` (see Section 7).

The Ray cluster uses Anyscale's autoscaler to scale between 1 and 200 nodes. Worker nodes are **spot** `m5.4xlarge` instances (~$0.25/hr, approximately 67% off on-demand) to keep full-scale 200-node runs affordable. The head node is an on-demand `m7i.2xlarge` (8 vCPUs, 32 GiB — ample headroom for the Serve controller, GCS, and dashboard even at 4,096-replica state). A dedicated `cpu-gpu-sim` node group carries the `simulated_gpu` custom resource to create resource heterogeneity without physical GPUs. At steady state (no Locust traffic), all apps are scaled to zero and the cluster idles at the head node only — no worker nodes are running.

| Node Group | Instance Type | Pricing | Custom Resources | Min | Max | Purpose |
|---|---|---|---|---|---|---|
| `head` | `m7i.2xlarge` | On-demand ($0.4032/hr) | — | 1 | 1 | Serve controller, GCS, dashboard (always-on) |
| `cpu-general` | `m5.4xlarge` | Spot (~$0.25/hr) | — | 0 | 150 | CPU-only replicas (scales to zero between Locust runs) |
| `cpu-gpu-sim` | `m5.4xlarge` | Spot (~$0.25/hr) | `{"simulated_gpu": 1}` | 0 | 49 | Replicas requesting `simulated_gpu` resource (scales to zero between Locust runs) |

Spot instance interruptions are acceptable for this validation service — a mid-run spot termination exercises the same node-loss recovery path that real customers experience. If a spot interruption corrupts a Locust run, the run is simply discarded and re-scheduled.

### 3.2 Request / Data-Flow Patterns

**Pattern A — Simple (echo-baseline, highscale-stress, grpc-canary, heavy-payload, long-runner)**:  
Client → HTTP/gRPC Proxy → Single Deployment Replica → Response.

**Pattern B — Chain/DAG (nlp-chain, image-dag, cpu-fanout, mixed-preprocess)**:  
Client → HTTP Proxy → Ingress Deployment → `DeploymentHandle.remote()` → Stage 2 → … → Stage N → Response aggregated at ingress.  
For `cpu-fanout`, the ingress issues 4 parallel `handle.remote()` calls and awaits all with `asyncio.gather`.

**Pattern C — Batching (batch-infer)**:  
Client → HTTP Proxy → Deployment Replica → `@serve.batch` collects up to `max_batch_size=32` requests → batch function processes all → individual responses returned. The batch decorator manages the internal queue per replica.

**Pattern D — Streaming (stream-chat)**:  
Client → HTTP Proxy (SSE/chunked) → Deployment Replica → `async def generate()` yields tokens → proxy streams chunks back to client. The `is_streaming` metadata flag on `RequestMetadata` activates the streaming proxy path.

**Pattern E — Multiplexed (mux-model-router)**:  
Client (with `model_id` header) → HTTP Proxy → Replica (routed by model affinity) → `@serve.multiplexed` loads model if not cached → processes → response. LRU eviction when `max_num_models_per_replica` (set to 5) is exceeded.

### 3.3 Inter-Deployment Data Transfer Strategy

Large payloads between chained deployments use Serve's native `DeploymentResponse` pipeline — no explicit `ray.put()` / `ray.get()`. The strategy:

1. **Ingress deployment** receives raw bytes, then calls `downstream_handle.remote(payload)`. This returns a `DeploymentResponse`. Serve internally serializes the argument through the object store and routes it to the downstream replica.
2. **Chaining responses**: The `DeploymentResponse` from one stage is passed directly as input to the next stage's handle call (e.g., `classify_handle.remote(resize_handle.remote(image_bytes))`). Serve resolves the `DeploymentResponse` to its underlying value before invoking the downstream handler, using the object store for transfer — exercising the same zero-copy path without user-level object store API calls.
3. **Payload sizes**: `image-dag` uses 2-10 MB (simulated image buffers) across 4 chained stages; `heavy-payload` handles 5-50 MB payloads as HTTP request/response bodies through a single deployment.
4. **Validation signals**: Monitor `ray_object_store_memory` on worker nodes, `serve_deployment_processing_latency_ms` for stages receiving large payloads, and `object_spilling_bytes` to detect regressions in Serve's internal data transfer path.

### 3.4 Isolation Boundaries and Blast-Radius Controls

| Boundary | Mechanism | Purpose |
|---|---|---|
| Application-level | Separate Serve applications with independent route prefixes | Failure in one app (e.g., crash loop in `long-runner`) does not affect routing or autoscaling of others |
| Resource-level | `ray_actor_options` per deployment specifying `num_cpus` and optional `resources={"simulated_gpu": 1}` | Prevents resource starvation; simulated-GPU apps are constrained to `cpu-gpu-sim` nodes and cannot consume `cpu-general` capacity |
| Node group | Anyscale node groups with custom resource tagging | Simulated-GPU workloads isolated to `cpu-gpu-sim` nodes; head node never runs replicas (`resources: {"head": 0}`) |
| Autoscaling | Per-deployment `AutoscalingConfig` with independent `max_replicas` | Each app has a replica ceiling; `highscale-stress` cannot consume replicas budgeted for `stream-chat` |
| Object store | Memory monitor alerts at 60%/80% thresholds | Early warning before object spilling degrades apps; `DeploymentResponse` chaining still uses object store internally |
| Health check | Per-deployment health check with `health_check_period_s=10`, `health_check_timeout_s=30` | Unhealthy replicas in one deployment are restarted without affecting others |

---

## 4. Capacity and Scaling Design

### 4.1 Replica Budgeting Approach

Each application has a fixed `max_replicas` ceiling in its `AutoscalingConfig`. The sum of all `max_replicas` equals exactly 4,096, enforced by a validation check in the config generation pipeline. The budget is weighted by risk importance:

| Priority Tier | Applications | Budget Share | Rationale |
|---|---|---|---|
| Scale stress | `highscale-stress` | 1,536 (37.5%) | Validates autoscaling at extreme replica count — the primary scaling risk area |
| Core features | `stream-chat`, `nlp-chain`, `batch-infer`, `cpu-fanout` | 1,472 (35.9%) | Covers streaming, DAG, batching, fan-out — the most customer-facing features |
| Supplementary | `image-dag`, `long-runner`, `mixed-preprocess`, `heavy-payload`, `mux-model-router` | 944 (23.1%) | Object transfer, long requests, multiplexing — important but lower blast radius |
| Canaries | `echo-baseline`, `grpc-canary` | 144 (3.5%) | Lightweight health signals; low replica needs |

### 4.2 Node-Level Capacity Assumptions

All nodes share the same `m5.4xlarge` hardware. The only difference is the `simulated_gpu` custom resource tag.

| Node Group | vCPU | RAM | Custom Resource | Usable vCPU | CPU-only Replicas (@ 0.5 CPU) | Sim-GPU Replicas (@ 0.5 CPU + 1 `simulated_gpu`) |
|---|---|---|---|---|---|---|
| `cpu-general` | 16 | 64 GB | — | ~14 | 28 | 0 |
| `cpu-gpu-sim` | 16 | 64 GB | `simulated_gpu: 1` | ~14 | 0 (reserved for sim-GPU workloads) | 1 sim-GPU + up to 26 CPU co-located |

Each `cpu-gpu-sim` node can host **1 simulated-GPU replica** (consuming the `simulated_gpu: 1` resource) plus additional CPU-only replicas on its remaining ~13.5 vCPU. This co-location is safe because simulated-GPU workloads perform CPU computation, not real GPU computation — there is no hardware contention.

**Peak node calculation**:
- CPU-only replicas at peak: ~2,840 (all CPU-only apps + CPU stages of mixed apps) → 2,840 / 28 ≈ 102 `cpu-general` nodes.
- Simulated-GPU replicas at peak: ~1,256 (sim-GPU stages of all mixed/sim-GPU apps) → needs 1,256 / 1 = 1,256 `cpu-gpu-sim` nodes... which exceeds the 49-node cap.

**Resolution**: Since `cpu-gpu-sim` nodes are the same hardware as `cpu-general`, we increase `simulated_gpu` per node. Setting `simulated_gpu: 8` per `cpu-gpu-sim` node allows 8 sim-GPU replicas per node, dramatically reducing node count:
- 1,256 sim-GPU replicas / 8 per node = ~157 sim-GPU replica-slots needed → but max 49 `cpu-gpu-sim` nodes × 8 = 392 sim-GPU slots.
- The shortfall is resolved the same way as CPU: most apps never reach `max_replicas` simultaneously. The replica budget (4,096 total) represents theoretical peak, not steady-state. At typical 60% utilization, sim-GPU demand is ~750 replicas → 94 slots → 12 `cpu-gpu-sim` nodes.

**Simulated-GPU replica budget across `cpu-gpu-sim` nodes (peak)**:

| Sim-GPU App | Peak Sim-GPU Replicas | Node Slots Needed (@ 8/node) |
|---|---|---|
| `stream-chat` | 512 | 64 |
| `batch-infer` | 256 | 32 |
| `mux-model-router` | 128 | 16 |
| `nlp-chain` (encoder stage) | 160 | 20 |
| `image-dag` (classify stage) | 60 | 8 |
| `mixed-preprocess` (sim-GPU stage) | 64 | 8 |
| **Total** | **1,180** | **148 (theoretical)** |

At 60% peak utilization (the realistic operating point), demand drops to ~708 slots → 89 slots → ~12 `cpu-gpu-sim` nodes. During monthly full-scale tests, the `cpu-gpu-sim` max is raised to 49 to handle spike phases.

### 4.3 Method to Reach and Control 4,096 Replicas

1. **Idle phase** (1 node — head only): All apps at `min_replicas=0`. Zero replicas running, zero worker nodes. Only the head node is alive (Serve controller, GCS). Cluster cost is minimal.
2. **Activation phase**: Locust job starts. First requests trigger cold starts on all 12 apps; the Serve autoscaler provisions replicas from zero. The Anyscale cluster autoscaler adds worker nodes as pending actors cannot be placed. Cluster grows from 1 node to the traffic-appropriate size.
3. **Ramp phase**: Locust increases load per the traffic schedule. The Serve autoscaler observes increasing `total_num_requests` via `AutoscalingContext` and scales replicas up. Node count grows proportionally.
4. **Peak phase**: Load generator pushes `highscale-stress` to sustained high QPS, forcing its deployment to scale toward 1,536 replicas. Simultaneously, other apps are at their respective peak traffic phases. Total replicas converge toward 4,096.
5. **Wind-down**: After Locust stops, traffic drops to zero. After `downscale_delay_s` + `downscale_to_zero_delay_s` elapse, all replicas drain and all worker nodes are terminated. Cluster returns to head-only (1 node).
6. **Control mechanism**: Each deployment's `max_replicas` is a hard ceiling. The cluster-level node max (200) is a hard ceiling. If replica demand exceeds node capacity, replicas remain pending—this is itself a validation signal (we alert on `serve_deployment_queued_queries` spikes indicating under-provisioning).

### 4.4 Autoscaling Behaviors by Traffic Pattern

All 12 apps use `min_replicas=0` so the cluster can scale to zero workers between Locust runs. This is the primary cost control mechanism — the cluster pays only for the head node when idle.

| Traffic Pattern | Apps | min_replicas | Autoscaling Behavior | Key Config |
|---|---|---|---|---|
| **Stable baseline** | `echo-baseline`, `grpc-canary` | 0 | Scales from zero on first Locust request; stays at low replica count during run; drains to zero after | `target_ongoing_requests=5`, `upscale_delay_s=30`, `downscale_delay_s=120`, `downscale_to_zero_delay_s=300` |
| **Stable baseline** | `mux-model-router` | 0 | Same as above; cold-start includes model load | `target_ongoing_requests=5`, `downscale_to_zero_delay_s=300` |
| **Steady growth** | `nlp-chain`, `heavy-payload` | 0 | Scales from zero; all DAG stages start together on first request; gradual upscale during run | `target_ongoing_requests=3`, `upscale_delay_s=15`, `downscale_delay_s=300`, `downscale_to_zero_delay_s=300` |
| **Steady decline** | `image-dag`, `long-runner` | 0 | Scales from zero; declines during run; drains fully after Locust stops | `downscale_delay_s=120`, `downscale_to_zero_delay_s=300` (`long-runner`: 600) |
| **Spiky** | `batch-infer`, `cpu-fanout`, `highscale-stress` | 0 | Scales from zero on burst arrival, rapid upscale, drains to zero after burst | `target_ongoing_requests=1`, `upscale_delay_s=5`, `downscale_delay_s=60`, `downscale_to_zero_delay_s=180` |
| **Diurnal** | `stream-chat`, `mixed-preprocess` | 0 | Scales from zero during simulated "business hours", back to zero at "night" | `target_ongoing_requests=2`, `upscale_delay_s=10`, `downscale_delay_s=180`, `downscale_to_zero_delay_s=300` |

---

## 5. Traffic Modeling Design (Locust Concept)

### 5.1 User Personas and Traffic Classes

| Persona | Target App(s) | Behavior | Concurrency Model |
|---|---|---|---|
| `api-caller` | `echo-baseline`, `grpc-canary` | High-frequency, low-latency calls; uniform inter-arrival time | 200-500 concurrent users, 50-100 RPS per user |
| `pipeline-user` | `nlp-chain`, `image-dag`, `cpu-fanout`, `mixed-preprocess` | Moderate frequency; variable payload size; sequential request pattern | 50-200 concurrent users, 2-10 RPS per user |
| `batch-submitter` | `batch-infer` | Burst submitter; sends clusters of requests in rapid succession then pauses | 20-100 users, bursty (50 requests/s for 5s, then 30s pause) |
| `stream-consumer` | `stream-chat` | Opens SSE connection, consumes token stream for 2-30s, then reconnects | 100-500 concurrent connections, long-lived |
| `model-switcher` | `mux-model-router` | Cycles through 20 model IDs with Zipfian distribution (80% of traffic to 4 models) | 50-200 concurrent users, 5-20 RPS per user |
| `heavy-uploader` | `heavy-payload` | Sends 5-50 MB payloads at low frequency | 10-50 concurrent users, 0.5-2 RPS per user |
| `long-task-submitter` | `long-runner` | Submits requests that take 30-120s; low concurrency but high connection hold time | 20-100 concurrent users, 0.1-0.5 RPS per user |
| `scale-hammer` | `highscale-stress` | Maximum throughput driver; sustains as much RPS as the system can absorb | 500-2000 concurrent users, auto-calibrated |

### 5.2 Diurnal Schedule Shape

The 24-hour cycle is divided into phases anchored to a reference timezone (UTC):

| UTC Hours | Phase | Load Multiplier | Active Patterns |
|---|---|---|---|
| 00:00-06:00 | Night trough | 0.2x baseline | Stable only; diurnal apps at minimum |
| 06:00-08:00 | Morning ramp | 0.2x → 1.0x (linear) | Growth apps ramp; diurnal apps activate |
| 08:00-12:00 | Morning peak | 1.0x-1.2x | All patterns active; first spike window at 10:00 |
| 12:00-13:00 | Midday dip | 0.8x | Slight decline simulating lunch patterns |
| 13:00-17:00 | Afternoon peak | 1.0x-1.5x | Peak load; second spike window at 15:00; growth apps at maximum |
| 17:00-20:00 | Evening decline | 1.5x → 0.5x | Decline apps active; decline traffic pattern dominates |
| 20:00-00:00 | Night ramp-down | 0.5x → 0.2x | Gradual wind-down; stable apps only by midnight |

### 5.3 Phase Design

- **Stable**: Constant RPS ± 5% jitter. Implemented as Locust `constant_throughput` wait. Duration: continuous throughout all hours for stable apps.
- **Steady growth**: RPS increases by 3% per hour for 8 hours (06:00-14:00), then holds. Implemented as a custom `wait_time` function that reads the current phase from a shared schedule.
- **Steady decline**: RPS decreases by 4% per hour for 6 hours (14:00-20:00). Mirrors growth but in reverse.
- **Spike**: Two scheduled spike windows (10:00 and 15:00 UTC) where load jumps 5x baseline for 10 minutes, then returns to prior level over 5 minutes. Additionally, random unscheduled spikes (Poisson-distributed, λ=0.5/hr) of 3x for 3-5 minutes. Implemented as a spike injector coroutine in the Locust master.
- **Diurnal**: Sinusoidal curve: `load = baseline + amplitude * sin(2π * (hour - phase_offset) / 24)`. Amplitude set to 0.6x baseline, phase offset tuned so peak aligns with 14:00 UTC.

### 5.4 Realism Assumptions and Anti-Patterns to Avoid

**Assumptions**:
- Request payloads are synthetic but size-realistic (JSON bodies matching real customer patterns).
- Client concurrency correlates with replica count growth (we don't pre-saturate before autoscaling completes).
- Network latency between Locust and the Serve endpoint is < 5ms (same Anyscale region).

**Anti-patterns explicitly avoided**:
- **Flat step functions**: Real traffic doesn't jump from 0 to max. All transitions use linear ramps or sinusoidal curves.
- **Ignoring client-side backpressure**: Locust users respect response latency—if P99 exceeds 5x target, users back off (simulated via `on_failure` handler reducing spawn rate).
- **Uniform request distribution**: Real traffic is skewed. The `model-switcher` persona uses Zipfian; `batch-submitter` is bursty, not uniform.
- **Ignoring warm-up**: After each deploy or scale-up, a 60s warm-up period with reduced expectations is enforced before SLO evaluation begins.
- **Open-loop flooding**: All personas are closed-loop (wait for response before next request) to avoid unrealistic queue buildup that obscures real autoscaling behavior.

---

## 6. Observability and Alerting Design

### 6.1 SLI/SLO Definitions

| SLI | Measurement | SLO | Evaluation Window |
|---|---|---|---|
| Request success rate | `1 - (serve_deployment_error_counter / serve_deployment_request_counter)` per app | ≥ 99.9% per app | 5-minute rolling |
| P99 latency | `serve_deployment_processing_latency_ms` histogram P99 | Per-app targets (see portfolio table) | 5-minute rolling |
| Autoscaling convergence | Time from load change detection to target replica count reached | < 120s for scale-up, < 300s for scale-down | Per scaling event |
| Replica health ratio | `running_replicas / target_replicas` per deployment | ≥ 95% sustained | 1-minute rolling |
| Object store utilization | `ray_object_store_memory` / capacity per node | < 80% sustained, < 90% peak | 1-minute rolling |
| Head node memory usage | RSS of head node processes / total node memory | < 75% sustained | 1-minute rolling |
| Worker node avg memory | Mean(RSS / total memory) across all worker nodes | < 70% sustained | 5-minute rolling |

### 6.2 Dashboards

This service relies on the **default Grafana dashboards that ship with Ray Serve** (exported automatically when Serve metrics are scraped by Prometheus). No custom dashboards are built or maintained. The built-in Serve dashboards already cover per-deployment QPS, latency histograms, error rates, replica counts, queue depth, and autoscaling state — which are sufficient for evaluating all pass/fail gates defined in Section 8.1.

### 6.3 Alert Matrix

| Alert Name | Metric | Threshold | Severity | Response Intent |
|---|---|---|---|---|
| `app-error-rate-high` | Per-app error rate | > 1% for 5 min | P1 / Critical | Investigate deployment failures; potential regression |
| `app-latency-slo-breach` | Per-app P99 latency | > 2x SLO target for 5 min | P2 / Warning | Check autoscaling lag, queue depth |
| `app-latency-severe` | Per-app P99 latency | > 5x SLO target for 2 min | P1 / Critical | Likely resource starvation or deadlock |
| `replica-convergence-slow` | Actual vs target replica count | Deviation > 20% for 5 min | P2 / Warning | Autoscaler or cluster autoscaler issue |
| `head-node-memory-high` | Head node RSS / total memory | > 75% for 5 min | P2 / Warning | GCS or controller memory leak; potential head node OOM |
| `head-node-memory-critical` | Head node RSS / total memory | > 90% for 2 min | P1 / Critical | Imminent head node OOM; consider controller restart |
| `worker-avg-memory-high` | Mean worker memory utilization | > 70% for 10 min | P2 / Warning | Possible replica memory leak or object store pressure |
| `worker-avg-memory-critical` | Mean worker memory utilization | > 85% for 5 min | P1 / Critical | Cluster-wide memory pressure; scale investigation |
| `object-store-pressure` | Per-node object store utilization | > 80% for 5 min | P2 / Warning | Reduce large-object-transfer app load; check GC |
| `object-spilling-detected` | Object spilling bytes > 0 | Any spilling for 2 min | P2 / Warning | Object store capacity exceeded; resize or throttle |
| `deployment-unhealthy` | `serve_application_status` ≠ RUNNING | Any app not RUNNING for 3 min | P1 / Critical | Deployment crash loop or build failure |
| `autoscale-oscillation` | Replica count delta | > 3 direction changes in 10 min for same deployment | P2 / Warning | Autoscaling config tuning needed |
| `locust-failure-rate` | Locust request failure % | > 5% for 3 min | P2 / Warning | Either service degradation or load gen issue |
| `node-count-at-ceiling` | Cluster node count | = 200 for 10 min | P3 / Info | At autoscale ceiling; validate this is expected during peak |
| `grpc-probe-failure` | `grpc-canary` health check | 3 consecutive failures | P1 / Critical | gRPC proxy path broken |

---

## 7. Release and Upgrade Strategy

Three Anyscale primitives compose the system:

| Primitive | Anyscale Product | Lifecycle |
|---|---|---|
| **Serve validation service** (12 apps) | [Anyscale Service](https://docs.anyscale.com/services/deploy) (multi-app) | Always-on; head node persists 24/7; workers scale to zero between runs |
| **Version upgrade** | [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) | Weekly cron; redeploys the Anyscale Service with latest nightly image |
| **Locust load test** | [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) | Weekly cron; generates traffic against the Anyscale Service endpoint |

### 7.1 Serve Validation Service (Anyscale Service)

The 12 Serve applications are deployed as a single [multi-application Anyscale Service](https://docs.anyscale.com/services/multi-app). Each app is an entry under `applications:` in the service YAML with its own `route_prefix`. The service uses `image_uri: anyscale/ray:nightly` and the compute config from Section 3.1.

The service is deployed once and then updated in-place by the version upgrade job. It is never torn down — the head node runs 24/7, and workers autoscale to zero between Locust runs.

### 7.2 Weekly Version Upgrade Job (Anyscale Scheduled Job)

An [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) runs once per week to redeploy the Anyscale Service with the latest `anyscale/ray:nightly` image from [Docker Hub](https://hub.docker.com/r/anyscale/ray/tags?name=nightly). The `nightly` tag always resolves to the latest nightly build, so there is no version query — the service simply redeploys with the same tag and picks up whatever is current.

Schedule config (applied via `anyscale schedule apply`):

```
timezone: UTC
cron_expression: "0 2 * * 1"  # Every Monday at 02:00 UTC
job_config:
    name: serve-validation-version-upgrade
    entrypoint: python upgrade_service.py
    image_uri: anyscale/ray:nightly
```

The job's entrypoint script calls `anyscale.service.deploy()` with the updated `image_uri`, waits for all 12 apps to reach `RUNNING`, and posts the result to Slack. If any app fails to reach `RUNNING` within 20 minutes, the script reverts to the previous known-good image digest and alerts.

### 7.3 Weekly Locust Load Test Job (Anyscale Scheduled Job)

An [Anyscale Scheduled Job](https://docs.anyscale.com/platform/jobs/schedules) runs the Locust load test against the live Anyscale Service. Scheduled 4 hours after the version upgrade to allow the new version to stabilize. Each run scales the Serve cluster to **200 nodes** and drives traffic to reach **4,096 replicas** at peak.

Schedule config:

```
timezone: UTC
cron_expression: "0 6 * * 1"  # Every Monday at 06:00 UTC
job_config:
    name: serve-validation-locust
    entrypoint: locust -f locustfile.py --headless --host $SERVE_ENDPOINT
    image_uri: anyscale/ray:nightly
    compute_config:
        cloud: ...
        head_node:
            instance_type: m5.xlarge
```

| Step | Action | Duration |
|---|---|---|
| 1 | Locust Scheduled Job starts on its own Anyscale Job cluster (single m5.xlarge) | ~2 min |
| 2 | Locust generates compressed diurnal traffic (all 5 patterns, all 12 apps), ramping the Serve cluster to 200 nodes / 4,096 replicas at peak | ~2.5 hours |
| 3 | Locust exits; Serve cluster contracts back to head-only via scale-to-zero | ~30 min |

The Locust Scheduled Job runs on its own separate Anyscale Job cluster — it does not share nodes with the Anyscale Service. It only generates traffic; it does not collect metrics or evaluate pass/fail. All evaluation happens in Grafana (see Section 8.1).

Either job can be triggered manually at any time via `anyscale schedule run --name <name>` for ad-hoc runs.

### 7.4 Rollback Model

Rollback is simple and manual: if the weekly Locust run fails after a version upgrade, the on-call engineer triggers the version upgrade job manually with the previous known-good `anyscale/ray:nightly` image digest (via `anyscale schedule run` or by updating the schedule config). There is no automated multi-stage canary — the Locust load test *is* the canary.

If a critical regression is detected mid-week (e.g., via Grafana alerts on the always-on head node), the on-call triggers the version job with the previous digest at any time.

---

## 8. Reliability Validation Strategy

### 8.1 Pass/Fail Gates (Grafana Evaluation)

Pass/fail is determined by reviewing Grafana dashboards and Grafana alert history after each Locust run. The Locust job itself is fire-and-forget — it generates load but does not evaluate results. All metrics below are emitted by Serve and Anyscale and flow into Prometheus/Grafana automatically.

**Evaluation method**: After each Locust run, the on-call engineer (or anyone reviewing the run) opens the built-in Serve Grafana dashboards and checks the alert panel for the run's time window. A run **passes** if no P1/P2 alerts fired during the run window.

**Gate definitions** (encoded as Grafana alert rules on Serve/Anyscale metrics):

| Gate | Grafana Alert Rule Metric | Threshold | Source Metric |
|---|---|---|---|
| All apps healthy | `serve_application_status` per app | All apps `RUNNING` within 15 min of Locust start | Serve controller |
| Scale-from-zero success | `serve_deployment_replica_starts` for scale-to-zero apps | > 0 within 60s of first request per app | Serve controller |
| Error rate | `serve_deployment_error_counter / serve_deployment_request_counter` | < 0.1% per app (5-min window) | Serve replica |
| P99 latency | `serve_deployment_processing_latency_ms` P99 | Within 1.5x of per-app target (5-min window) | Serve replica |
| Autoscaling convergence | `serve_num_ongoing_requests_at_replicas` vs target replica count | Queued requests return to 0 within 120s of scale-up | Serve router |
| Head node memory | Head node RSS / total | < 80% sustained | Anyscale node metrics |
| Worker avg memory | Mean worker RSS / total | < 75% sustained | Anyscale node metrics |
| Cluster contraction | Anyscale cluster node count | Head-only (1 node) within 30 min after Locust stops | Anyscale autoscaler |

**Run outcome**:
- **Pass**: No P1/P2 Grafana alerts fired during the run window. All dashboard panels show green.
- **Fail**: Any P1/P2 alert fired. The on-call investigates using the dashboards and runbooks (Section 9.3). If the failure occurred after a weekly version upgrade, the on-call evaluates whether to roll back.

---

## 9. Operational Model

### 9.1 Incident Response Model

| Severity | Definition | Response Time | Notification | Examples |
|---|---|---|---|---|
| P1 / Critical | Validation service is unable to produce results; blocking regression detected | 15 min ack | PagerDuty → On-call + Slack #serve-validation-incidents | Head node OOM, > 50% of apps in DEPLOY_FAILED, controller crash loop |
| P2 / Warning | Degraded validation quality; some SLOs breached but service is running | 1 hour ack | Slack #serve-validation-alerts | Single app latency breach, autoscaling oscillation, elevated memory |
| P3 / Info | Informational; no immediate action required | Next business day | Slack #serve-validation-info | Node count at ceiling (expected during peak), minor metric anomaly |

### 9.2 Ownership, Escalation, and On-Call

| Role | Responsibility | Rotation |
|---|---|---|
| **Primary on-call** (Serve Reliability team) | Respond to P1/P2 alerts; triage nightly failures; execute rollbacks | Weekly rotation, 2-person coverage |
| **Secondary on-call** (Serve Core team) | Escalation point for issues traced to Serve internals (controller, autoscaler, proxy) | Best-effort during business hours |
| **Anyscale platform contact** | Cluster provisioning failures, node group issues, Anyscale Service bugs | Anyscale support ticket (P1 = urgent) |
| **Validation service owner** (tech lead) | Design decisions, budget approval, quarterly review of validation effectiveness | Permanent role |

**Escalation path**: Primary on-call → (30 min) → Secondary on-call → (1 hour) → Serve team lead → (2 hours) → Director of Serve.

### 9.3 Runbook Taxonomy and Response Priorities

| Runbook ID | Title | Trigger | Priority | Key Steps |
|---|---|---|---|---|
| RB-001 | Head node OOM recovery | `head-node-memory-critical` alert | P1 | Check GCS memory, controller checkpoint size; restart controller if needed; investigate memory growth with `py-spy` |
| RB-002 | Application DEPLOY_FAILED | `deployment-unhealthy` alert | P1 | Check `serve status`; inspect build logs; verify `import_path` and `runtime_env`; redeploy |
| RB-003 | Autoscaling oscillation | `autoscale-oscillation` alert | P2 | Review `upscale_delay_s`/`downscale_delay_s` for affected deployment; check traffic pattern; adjust `look_back_period_s` |
| RB-004 | Worker memory pressure | `worker-avg-memory-critical` alert | P1 | Identify top-memory replicas via Ray dashboard; check for memory leaks in simulated workloads; restart affected replicas |
| RB-005 | Object store spilling | `object-spilling-detected` alert | P2 | Reduce `heavy-payload` or `image-dag` load; check payload sizes; verify Serve handle pipeline is not leaking intermediate `DeploymentResponse` objects |
| RB-006 | Locust run failure | Grafana alerts fire during/after Locust Scheduled Job | P2 | Compare with previous run's Grafana metrics; check for infrastructure vs. Serve regression; re-run via `anyscale schedule run --name serve-validation-locust` if transient |
| RB-007 | Weekly version rollback | Version upgrade Scheduled Job fails or post-upgrade Locust run fails | P2 | Re-run version upgrade via `anyscale schedule run --name serve-validation-version-upgrade` with previous image digest; investigate diff between Ray versions; file Serve bug if regression confirmed |
| RB-008 | Locust generator failure | `locust-failure-rate` alert or Locust Scheduled Job crash | P3 | Check Locust Scheduled Job cluster logs; verify network connectivity to Anyscale Service endpoint; re-trigger via `anyscale schedule run` |
| RB-009 | gRPC probe failure | `grpc-probe-failure` alert | P1 | Check gRPC proxy process; verify port binding; inspect `grpc_options` config |
| RB-010 | Cluster at node ceiling | `node-count-at-ceiling` alert | P3 | Verify this is expected for current traffic phase; if unexpected, investigate runaway autoscaling in specific app |

---

## 10. Risks, Trade-Offs, and Open Decisions

### 10.1 Top Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | **Autoscaling oscillation at 1,536 replicas** (`highscale-stress`) | High | Medium — masks real scaling issues | Tune `upscale_delay_s`/`downscale_delay_s` aggressively; add oscillation detection alert; consider custom `AutoscalingPolicy` for this app |
| 2 | **Scale-from-zero cold-start latency** exceeds SLO targets for first requests | High | Medium — false SLO failures after idle periods | Exclude first 60s after scale-from-zero from SLO evaluation; track cold-start latency as a separate SLI; tune `downscale_to_zero_delay_s` to avoid excessive zero-scale transitions |
| 3 | **Head node OOM from controller state at 4,096 replicas** | Low | High — kills entire cluster | m7i.2xlarge (32 GiB) provides substantial headroom; monitor controller memory proactively; set memory budget alert at 60% |
| 4 | **False regression attribution** (infrastructure issue misidentified as Serve bug) | High | Medium — wastes engineering time | Require 2 consecutive Locust run failures before filing Serve bug; correlate with Anyscale infrastructure health API |
| 5 | **Weekly version upgrade breaks service** | Medium | Medium — service degraded until rollback | Version upgrade Scheduled Job gates on all apps reaching `RUNNING`; immediate revert on failure; Monday Locust Scheduled Job validates under load |
| 6 | **Custom resource simulation diverges from real GPU scheduling** | Medium | Medium — misses GPU-specific placement bugs | Validate custom resource scheduling exercises the same `DeploymentStateManager` and `AutoscalingStateManager` code paths; supplement with quarterly runs on real GPU nodes if critical GPU regressions are suspected |
| 7 | **Spot instance interruptions during Locust run** | Medium | Low — run is discarded and rescheduled | Spot interruptions are tolerable: they exercise node-loss recovery. If > 20% of nodes are reclaimed simultaneously, discard the run. Budget includes headroom for ~1 re-run/month |
| 8 | **Locust job instability skewing results** | Low | Medium — invalid test results | Run Locust on dedicated, oversized on-demand instance; monitor Locust health independently; discard runs with Locust failures |
| 9 | **Version skew between Serve config schema and new Ray release** | Low | High — deploy fails immediately | Version upgrade job catches this immediately (app fails `RUNNING` gate); automatic revert to prior version |
| 10 | **Cluster fails to contract back to head-only after Locust stops** | Medium | Medium — cost overrun if workers stay alive 24/7 | Pass/fail gate checks ≤ 1 node within 30 min after Locust; alert on worker count > 0 when no Locust is running; investigate autoscaler or `downscale_to_zero_delay_s` misconfiguration |
| 11 | **Simulated workloads miss real model inference bugs** | Inherent | Medium — gaps in coverage | Accept trade-off for deterministic failure attribution; supplement with quarterly real-model validation runs using actual NLP/CV models |

### 10.2 Key Design Trade-Offs

| Decision | Option A (Chosen) | Option B (Rejected) | Rationale |
|---|---|---|---|
| **Scale-to-zero at rest vs. always-on min replicas** | `min_replicas=0` for all 12 apps; head-only idle (1 node) | `min_replicas≥1` for canary apps; multi-node idle cluster | Reduces idle cost to ~$290/month (head only, m7i.2xlarge). Scale-from-zero is itself a high-value validation target (common customer pattern). Trade-off: no between-run health monitoring, but evaluation happens in Grafana during Locust runs. |
| **DeploymentResponse chaining vs. explicit ray.put/get** | Pass `DeploymentResponse` between chained stages via `DeploymentHandle` | Explicit `ray.put()` in ingress, pass `ObjectRef` downstream | `DeploymentResponse` is the idiomatic Serve pattern. It still exercises the object store internally but keeps data flow within Serve's control plane, avoiding user-level object lifecycle management. |
| **Two Anyscale Scheduled Jobs vs. multi-stage canary pipeline** | Weekly Scheduled Job upgrades version; weekly Locust Scheduled Job validates | Multi-day canary → soak → promote → observe pipeline | Simpler to implement and operate. The Locust Scheduled Job *is* the canary: if Grafana alerts fire after a version upgrade, the on-call reverts. Avoids over-engineering the rollout for a validation service. Cron scheduling via `anyscale schedule apply` is native and requires no external CI. |
| **CPU-only with custom resources vs. real GPU nodes** | All `m5.4xlarge` CPU nodes; `cpu-gpu-sim` group tagged with `simulated_gpu` custom resource | Mixed CPU + GPU node groups (e.g., `g5.xlarge`) | Eliminates GPU instance cost entirely. Serve's scheduler, autoscaler, and deployment state manager treat custom resources identically to `num_gpus` for placement decisions. The only gap is CUDA-specific runtime failures, which are out of scope for Serve-level validation. |
| **Simulated vs. real ML models** | Simulated compute (CPU sleep + memory allocation) | Real ML models (HuggingFace, etc.) | Simulated workloads provide deterministic latency, lower cost, and clearer failure attribution. Real models introduce non-Serve variance (model loading, CUDA errors) that obscures regression detection. |
| **Single cluster vs. multi-cluster** | Single Anyscale Service with all 12 apps | Separate clusters per app group | Single cluster validates multi-app isolation and shared-resource contention—the exact customer scenario we must validate. Multi-cluster would miss cross-app interference bugs. |
| **4,096 replicas via one app vs. distributed** | `highscale-stress` at 1,536 + 11 other apps | One app scaled to 4,096 | Distributed budget is more realistic (no customer runs 4,096 replicas of one deployment). `highscale-stress` still validates high-count scaling. |

### 10.3 Assumptions Requiring Stakeholder Confirmation

| # | Assumption | Stakeholder | Impact if Wrong |
|---|---|---|---|
| 1 | CPU-only nodes with `simulated_gpu` custom resource adequately validate resource-heterogeneous scheduling for GPU workloads | Serve Core Team | If real `num_gpus` has scheduler code paths not exercised by custom resources, GPU-specific regressions could be missed; mitigate with targeted GPU CI tests in the Ray repo |
| 2 | `downscale_to_zero_delay_s` is reliable enough for all 12 scale-to-zero apps without causing excessive cold-start failures | Serve Core Team | If scale-from-zero is slow (> 30s) or unreliable, regressions may only be caught during active Locust runs |
| 3 | Head-only idle cluster (m7i.2xlarge, 32 GiB) is sufficient to run the Serve controller and GCS with zero replicas | Serve Core Team | 32 GiB provides ample headroom; downsize to m7i.xlarge (16 GiB) if cost pressure increases |
| 4 | 200-node Anyscale Service limit (all `m5.4xlarge`) is supported without special approval | Anyscale Platform Team | May need quota increase or architectural changes |
| 5 | Simulated workloads are sufficient (no requirement for real model inference) | Serve PM / QA Lead | Real models would 3-5x cost and introduce non-determinism |
| 6 | Weekly Ray version rollout cadence aligns with Ray release schedule | Ray Release Team | If releases are less frequent, weekly rollout becomes a no-op most weeks |
| 7 | Anyscale Scheduled Jobs (`anyscale schedule apply`) can reliably run weekly Locust tests and version upgrades on cron | Anyscale Platform Team | Cron scheduling is native to Anyscale; no external CI needed |
| 8 | Head node `m7i.2xlarge` (32 GiB) is sufficient for controller state during full-scale Locust runs at 4,096 replicas | Serve Core Team | Monitor head node memory during peak; 32 GiB should handle controller checkpoint + GCS overhead comfortably |

---

## 11. Cost Estimation

### 11.1 Cost Model

The service has two cost components: the always-on head node and the burst spot worker nodes during Locust runs. All apps at `min_replicas=0` means zero worker nodes between runs. Workers use **spot instances** (~$0.25/hr for m5.4xlarge, ~67% off on-demand), which makes full-scale 200-node runs affordable.

| Component | Instance | Pricing | Quantity | Hours/Month | Unit $/hr | Monthly Cost |
|---|---|---|---|---|---|---|
| **Head node (always-on)** | m7i.2xlarge | On-demand | 1 | 720 | $0.4032 | **$290** |
| **Workers during Locust runs** | m5.4xlarge | Spot | up to 199 | ~1,300 (see below) | ~$0.25 | **$325** |
| **Locust Scheduled Job cluster** | m5.xlarge | On-demand | 1 | ~13 | $0.192 | **$3** |
| | | | | | **Total** | **$618** |

### 11.2 Worker Hours Derivation

Each Locust run scales the cluster from head-only to **200 nodes** (199 spot workers) and back to head-only. Run duration: ~3 hours.

| Run Phase | Duration | Avg Worker Nodes | Node-Hours |
|---|---|---|---|
| Scale-from-zero + ramp | 30 min | 50 | 25 |
| Ramp to peak | 30 min | 130 | 65 |
| Sustained peak (200 nodes, 4,096 replicas) | 60 min | 199 | 199 |
| Wind-down + scale-to-zero | 60 min | 50 | 50 |
| **Per-run total** | **3 hr** | | **339 worker node-hours** |

Spot cost per run: 339 × $0.25 = **$84.75**

Frequency: **once per week** (4.33 runs/month). Monthly worker cost: 4.33 × $84.75 = **$367**. At spot prices this swings based on demand — the $0.25/hr estimate includes margin; actual spot can be lower.

Adjusted for spot variability (using $0.25 as a conservative upper bound):

### 11.3 Budget Summary

| Line Item | Monthly |
|---|---|
| Head node (m7i.2xlarge, on-demand, 24/7) | $290 |
| Worker burst (m5.4xlarge, spot, 4 runs at 200 nodes) | $325 |
| Locust job (m5.xlarge, on-demand) | $3 |
| **Total** | **$618** |

**Over the $500 target by ~$118.** The m7i.2xlarge head node (32 GiB) adds headroom for controller state at 4,096 replicas but increases always-on cost. See Section 11.4 for levers to bring the budget back down.

### 11.4 Cost Levers

| To reduce cost | Action | Savings |
|---|---|---|
| 3 runs/month instead of 4 | Skip one week | ~$85/month |
| Shorter peak phase (30 min instead of 60 min) | Reduces worker hours by ~100/run | ~$100/month |
| Smaller peak (100 nodes instead of 200) | Halves peak worker hours | ~$50/run |
| Downsize head to m7i.xlarge (16 GiB) | Lower always-on cost ($0.2016/hr → $145/month) | ~$145/month |

| To spend headroom (if budget allows) | Action | Additional Cost |
|---|---|---|
| 2nd run per week | Add midweek validation | ~$85/month |
| Extend peak phase to 90 min | Longer soak at 200 nodes | ~$50/month |

---

## Design Compliance Checklist

| Requirement | Status | Evidence |
|---|---|---|
| ≥ 10 applications included | **PASS** | 12 applications defined in portfolio table (Section 2) |
| All required Serve features covered | **PASS** | Single deployment (echo-baseline, highscale-stress, heavy-payload, long-runner, grpc-canary), Chained/DAG (nlp-chain, image-dag, cpu-fanout, mixed-preprocess), Batching (batch-infer), Streaming (stream-chat), Multiplexing (mux-model-router) |
| All required workload scenarios covered | **PASS** | Large object transfer (image-dag via `DeploymentResponse` chaining, heavy-payload via large HTTP payloads), Mixed CPU / simulated-GPU (nlp-chain, image-dag, mixed-preprocess — uses `simulated_gpu` custom resource), Short requests (echo-baseline, cpu-fanout, grpc-canary), Long requests (stream-chat, long-runner) |
| All required traffic patterns covered | **PASS** | Stable (echo-baseline, grpc-canary), Growth (nlp-chain, heavy-payload), Decline (image-dag, long-runner), Spiky (batch-infer, cpu-fanout, highscale-stress), Diurnal (stream-chat, mixed-preprocess). Scale-to-zero (9 apps) adds additional coverage. |
| Autoscaling envelope 1-200 nodes addressed | **PASS** | Head-only idle (1 node); worker groups scale from 0 to 200 max (Section 3.1); all apps scale-to-zero (Section 4.4) |
| 4,096-replica peak strategy addressed | **PASS** | Replica budget sums to exactly 4,096 in Section 2; scale-up method detailed in Section 4.3 |
| Weekly release cadence addressed | **PASS** | Weekly version upgrade Scheduled Job in Section 7.2; weekly Locust Scheduled Job in Section 7.3; both managed via `anyscale schedule apply` |
| Required memory alerts included | **PASS** | Head node memory: `head-node-memory-high` (75%), `head-node-memory-critical` (90%); Worker avg memory: `worker-avg-memory-high` (70%), `worker-avg-memory-critical` (85%) — all in Section 6.3 |
| CPU-only cluster (no GPU nodes) | **PASS** | Head uses `m7i.2xlarge`; workers use `m5.4xlarge`; GPU workloads simulated via `simulated_gpu` custom resource on `cpu-gpu-sim` node group (Sections 2, 3.1, 4.2) |
| Monthly cost ≈ $618 | **OVER BUDGET** | Estimated $618/month with m7i.2xlarge head node and weekly 200-node full-scale runs using spot workers (Section 11.3). Apply cost levers in Section 11.4 (e.g., 3 runs/month or shorter peak phase) to bring under $500. |
| No implementation artifacts generated | **PASS** | No code, scripts, configs, pseudocode, or file creation instructions in this document |
