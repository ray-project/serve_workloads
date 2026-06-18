"""Always-on baseline traffic generator for the serve-validation service.

A separate Anyscale Service that sends a constant low QPS to serve-validation's
ingress, mirroring the locust persona request mix via the shared traffic_model.
The periodic locust load tests run *on top* of this baseline. HTTP-only for v1
(locust still covers gRPC during the spikes).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from typing import Optional

import aiohttp
from aiohttp.client_exceptions import ClientConnectionError, ClientOSError
from aiohttp_retry import RetryClient, ExponentialRetry
from fastapi import FastAPI
from pydantic import BaseModel

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._common.utils import run_background_task

from traffic_model import ENDPOINTS, build_request

logger = logging.getLogger("ray.serve")
api = FastAPI()

# Closed-loop, per-deployment concurrency: hold exactly N requests in flight to
# each deployment and refill the instant one returns (N "slots" per endpoint).
# Anything not in POOL_TARGETS uses DEFAULT_CONCURRENCY; 0 disables an endpoint.
# The send rate is emergent (~N / latency), not a fixed QPS.
DEFAULT_CONCURRENCY = 1
POOL_TARGETS = {}  # all deployments use DEFAULT_CONCURRENCY (uniform 1 in flight)
# heavy returns whatever size the request asks for; the always-on pool requests a
# 1 MB liveness canary and leaves the 5-50 MB stress path to the locust suite.
HEAVY_POOL_MB = 1.0
# Per-endpoint minimum seconds between request starts (0 == closed-loop, no cap).
# heavy returns 1 MB and the internal network is fast (~5 ms round trip), so an
# uncapped closed-loop self-generates ~250 MB/s; the proxy and locust stay clean
# (server is fine), but that churn occasionally truncates a response on the pinger
# side. Pacing heavy to ~2 req/s keeps it a gentle 1 MB canary instead of a hammer.
MIN_INTERVAL_S = {"heavy": 0.5}
# pool_inflight is a LIVE gauge — re-emit the actual per-endpoint in-flight count
# this often so the series stays fresh in Prometheus (a set-once gauge silently
# drops out of Grafana's metric browser) and reflects stalls, not just the target N.
POOL_INFLIGHT_EMIT_S = 2.0


class BaselinePingerArgs(BaseModel):
    target_base_url: str
    bearer_token: str = ""


@serve.deployment(
    num_replicas=1,
    user_config={"default_concurrency": DEFAULT_CONCURRENCY, "pool_targets": POOL_TARGETS},
    # The send loop is a single asyncio task (one core); give it dedicated cores
    # so co-located processes can't starve the loop and inflate latency.
    ray_actor_options={"num_cpus": 2},
)
class BaselinePinger:
    def __init__(self, target_base_url: str, bearer_token: str):
        self.target_base_url = target_base_url
        self.bearer_token = bearer_token
        self.default_concurrency = DEFAULT_CONCURRENCY
        self.pool_targets = dict(POOL_TARGETS)
        self._loop_task: Optional[asyncio.Task] = None
        self._inflight = {}   # live per-endpoint in-flight count; emitted by _emit_inflight
        self._init_metrics()

    async def reconfigure(self, config: dict):
        # Called by Serve after __init__ (in the event loop) and on every config
        # update — both starts the pools and supports live concurrency tuning.
        self.default_concurrency = max(0, int(config.get("default_concurrency", DEFAULT_CONCURRENCY)))
        self.pool_targets = dict(config.get("pool_targets", POOL_TARGETS))
        await self.stop()
        self.start()

    def _init_metrics(self):
        tk = ("endpoint", "source")
        self.req = Counter(
            "baseline_pinger_requests", "Requests sent.", tag_keys=tk
        ).set_default_tags({"source": "baseline"})
        self.ok = Counter(
            "baseline_pinger_requests_succeeded", "Requests with a 2xx response.", tag_keys=tk
        ).set_default_tags({"source": "baseline"})
        self.bad = Counter(
            "baseline_pinger_requests_failed", "Non-2xx or errored requests.", tag_keys=tk
        ).set_default_tags({"source": "baseline"})
        # Server-side failures only (5xx / timeout / connection); client-side
        # 4xx and unexpected client errors are excluded. This is the alert source.
        self.server_err = Counter(
            "baseline_pinger_server_errors",
            "Server-side failures calling serve-validation (reason=http_5xx|timeout|connection).",
            tag_keys=("endpoint", "source", "reason"),
        ).set_default_tags({"source": "baseline"})
        self.lat = Gauge(
            "baseline_pinger_request_latency_s", "Latency of the last request.", tag_keys=tk
        ).set_default_tags({"source": "baseline"})
        self.pool_inflight = Gauge(
            "baseline_pinger_pool_inflight",
            "Target in-flight requests held per deployment (closed-loop pool size).",
            tag_keys=tk,
        ).set_default_tags({"source": "baseline"})

    def _pool_spec(self):
        """(Endpoint, concurrency) per deployment; concurrency 0 disables it.
        heavy is re-pointed at a tiny body so the always-on pool is a liveness
        canary, not a 50 MB bandwidth test (the locust suite covers that)."""
        spec = []
        for ep in ENDPOINTS:
            n = int(self.pool_targets.get(ep.name, self.default_concurrency))
            if n <= 0:
                continue
            if ep.name == "heavy":
                ep = dataclasses.replace(ep, json_factory=lambda: {"mb": HEAVY_POOL_MB})
            spec.append((ep, n))
        return spec

    async def _run_pools(self):
        # Retry only instant connection-level errors (stale keep-alive sockets
        # killed by the LB). Never retry timeouts or 5xx: the slot refills on
        # completion, so the next request IS the retry; holding a slot across
        # timeout attempts would starve that deployment's concurrency.
        retry = ExponentialRetry(
            attempts=3, start_timeout=0.25, factor=2,
            exceptions=[aiohttp.ServerDisconnectedError, ClientOSError],
            retry_all_server_errors=False,
        )
        spec = self._pool_spec()
        async with RetryClient(retry_options=retry) as client:
            # _emit_inflight reports the live gauge; one _worker per slot.
            tasks = [asyncio.ensure_future(self._emit_inflight())]
            for ep, n in spec:
                self._inflight.setdefault(ep.name, 0)
                for _ in range(n):
                    tasks.append(asyncio.ensure_future(self._worker(client, ep)))
            logger.info(
                "BaselinePinger pools started: %s -> %s",
                {ep.name: n for ep, n in spec}, self.target_base_url,
            )
            await asyncio.gather(*tasks)

    async def _emit_inflight(self):
        # pool_inflight is a LIVE gauge: emit the actual per-endpoint in-flight
        # count on a heartbeat so the series stays fresh (a set-once gauge drops
        # out of the metric browser) and reflects reality (~N when healthy, lower
        # if a slot stalls). _worker maintains self._inflight via cheap dict ++/--.
        while True:
            for name, count in list(self._inflight.items()):
                self.pool_inflight.set(count, tags={"endpoint": name})
            await asyncio.sleep(POOL_INFLIGHT_EMIT_S)

    async def _worker(self, client: RetryClient, ep):
        # One persistent slot: keep one request to `ep` in flight and refill when
        # it returns. N workers per endpoint == N concurrent. A per-endpoint min
        # interval (MIN_INTERVAL_S) optionally caps the refill rate so a fast,
        # large-body endpoint can't self-hammer (see heavy).
        name = ep.name
        min_interval = MIN_INTERVAL_S.get(name, 0.0)
        loop = asyncio.get_event_loop()
        while True:
            start = loop.time()
            self._inflight[name] = self._inflight.get(name, 0) + 1
            try:
                await self._send(client, ep)
            except Exception:
                # _send swallows its own request errors; this only guards an
                # unexpected failure (e.g. build_request) from killing the slot.
                logger.exception(f"baseline {ep.name} worker iteration failed")
                await asyncio.sleep(0.1)
            finally:
                self._inflight[name] = self._inflight.get(name, 1) - 1
            if min_interval:
                await asyncio.sleep(max(0.0, min_interval - (loop.time() - start)))

    async def _send(self, client: RetryClient, ep):
        req = build_request(ep, self.target_base_url)
        tags = {"endpoint": ep.name}
        self.req.inc(tags=tags)
        t0 = asyncio.get_event_loop().time()
        try:
            async with client.request(
                req.method, req.url,
                json=req.json, data=req.data,
                headers={**req.headers, "Authorization": f"Bearer {self.bearer_token}"},
                timeout=aiohttp.ClientTimeout(total=ep.timeout_s),
            ) as resp:
                await resp.read()  # drain body (covers streaming + heavy payloads)
                (self.ok if resp.status == 200 else self.bad).inc(tags=tags)
                if resp.status >= 500:
                    # Server-side: serve-validation itself returned a 5xx.
                    self.server_err.inc(tags={**tags, "reason": "http_5xx"})
                    logger.error(f"baseline {ep.name} -> server HTTP {resp.status}")
                elif resp.status != 200:
                    # Client-side (4xx) or other non-2xx: failed, but not alertable.
                    logger.warning(f"baseline {ep.name} -> HTTP {resp.status}")
        # Order matters: ServerTimeoutError subclasses BOTH asyncio.TimeoutError
        # and ClientConnectionError, so the timeout handler must stay first.
        except asyncio.TimeoutError:
            # Server too slow to respond within ep.timeout_s — server-side.
            self.bad.inc(tags=tags)
            self.server_err.inc(tags={**tags, "reason": "timeout"})
            logger.warning(f"baseline {ep.name} server-side timeout")
        except ClientConnectionError as exc:
            # Connection dropped/refused, survived the 3 retries — server/proxy
            # unreachable. Server-side.
            self.bad.inc(tags=tags)
            self.server_err.inc(tags={**tags, "reason": "connection"})
            logger.warning(f"baseline {ep.name} server-side connection error: {exc!r}")
        except Exception:
            # Unexpected / client-side (e.g. a bug building the request) — count
            # as failed, but never as a server error.
            self.bad.inc(tags=tags)
            logger.exception(f"baseline {ep.name} client-side request error")
        finally:
            self.lat.set(asyncio.get_event_loop().time() - t0, tags=tags)

    def start(self):
        if self._loop_task is None:
            self._loop_task = run_background_task(self._run_pools())
        return "started"

    async def stop(self):
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None
        return "stopped"


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0})
@serve.ingress(api)
class Router:
    def __init__(self, pinger):
        self.pinger = pinger

    @api.get("/")
    async def root(self):
        return "baseline pinger ok"

    @api.get("/start")
    async def start(self):
        return await self.pinger.start.remote()

    @api.get("/stop")
    async def stop(self):
        return await self.pinger.stop.remote()


def build_app(args: BaselinePingerArgs):
    # Ray Serve passes the YAML `args` as a plain dict when the builder's type
    # hint is a stringized annotation (this module uses `from __future__ import
    # annotations`), so coerce explicitly instead of relying on auto-parsing.
    if isinstance(args, dict):
        args = BaselinePingerArgs(**args)
    token = args.bearer_token or os.environ.get("ANYSCALE_SERVICE_TOKEN", "")
    # In-flight pool size is tunable from YAML via BASELINE_DEFAULT_CONCURRENCY;
    # per-deployment overrides (if any) live in POOL_TARGETS.
    default_concurrency = int(os.environ.get("BASELINE_DEFAULT_CONCURRENCY", DEFAULT_CONCURRENCY))
    return Router.bind(
        BaselinePinger.options(
            user_config={"default_concurrency": default_concurrency, "pool_targets": dict(POOL_TARGETS)}
        ).bind(args.target_base_url, token)
    )
