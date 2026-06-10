"""Always-on baseline traffic generator for the serve-validation service.

A separate Anyscale Service that sends a constant low QPS to serve-validation's
ingress, mirroring the locust persona request mix via the shared traffic_model.
The periodic locust load tests run *on top* of this baseline. HTTP-only for v1
(locust still covers gRPC during the spikes).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Dict, Optional, Set

import aiohttp
from aiohttp.client_exceptions import ClientOSError
from aiohttp_retry import RetryClient, ExponentialRetry
from fastapi import FastAPI
from pydantic import BaseModel

from ray import serve
from ray.util.metrics import Counter, Gauge
from ray._common.utils import run_background_task

from traffic_model import build_request, choose_endpoint

logger = logging.getLogger("ray.serve")
api = FastAPI()

# Endpoints whose timeout exceeds this are gated separately from the fast
# majority, so a stuck slow request can never block baseline traffic.
SLOW_TIER_TIMEOUT_THRESHOLD_S = 15.0
SLOW_TIER_MAX_IN_FLIGHT = 10


class BaselinePingerArgs(BaseModel):
    target_base_url: str
    bearer_token: str = ""
    total_qps: float = 18.0


@serve.deployment(
    num_replicas=1,
    user_config={"total_qps": 18.0},      # reconfigure() starts/retunes the loop
    ray_actor_options={"num_cpus": 0},
)
class BaselinePinger:
    def __init__(self, target_base_url: str, bearer_token: str):
        self.target_base_url = target_base_url
        self.bearer_token = bearer_token
        self.total_qps = 18.0
        self._rng = random.Random()
        self._loop_task: Optional[asyncio.Task] = None
        self._pending: Dict[str, Set[asyncio.Future]] = {"fast": set(), "slow": set()}
        self._last_skip_log: Dict[str, float] = {}
        self._init_metrics()

    async def reconfigure(self, config: dict):
        # Called by Serve after __init__ (in the event loop) and on every config
        # update — both starts the loop and supports live QPS tuning.
        self.total_qps = max(0.1, float(config.get("total_qps", 18.0)))
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
        self.lat = Gauge(
            "baseline_pinger_request_latency_s", "Latency of the last request.", tag_keys=tk
        ).set_default_tags({"source": "baseline"})
        self.pend = Gauge(
            "baseline_pinger_pending_requests", "In-flight requests per tier.",
            tag_keys=("source", "tier"),
        ).set_default_tags({"source": "baseline"})
        self.skipped = Counter(
            "baseline_pinger_sends_skipped",
            "Sends skipped because the tier's in-flight cap was reached.",
            tag_keys=("source", "tier"),
        ).set_default_tags({"source": "baseline"})

    async def _run_loop(self):
        send_interval_s = 1.0 / self.total_qps
        caps = {"fast": 2.5 * self.total_qps, "slow": SLOW_TIER_MAX_IN_FLIGHT}
        # Retry only instant connection-level errors (stale keep-alive sockets
        # killed by the LB). Never retry timeouts or 5xx: for a constant-rate
        # generator the next scheduled send IS the retry, and a slot held
        # across 130s timeout attempts starves the send budget for minutes.
        retry = ExponentialRetry(
            attempts=3, start_timeout=0.25, factor=2,
            exceptions=[aiohttp.ServerDisconnectedError, ClientOSError],
            retry_all_server_errors=False,
        )
        loop = asyncio.get_event_loop()
        async with RetryClient(retry_options=retry) as client:
            next_send = loop.time()
            while True:
                ep = choose_endpoint(self._rng)
                tier = "slow" if ep.timeout_s > SLOW_TIER_TIMEOUT_THRESHOLD_S else "fast"
                pending = self._pending[tier]
                if len(pending) < caps[tier]:
                    pending.add(asyncio.ensure_future(self._send(client, ep)))
                else:
                    self.skipped.inc(tags={"tier": tier})
                    now = loop.time()
                    if now - self._last_skip_log.get(tier, 0.0) > 5.0:
                        self._last_skip_log[tier] = now
                        logger.warning(
                            f"baseline pinger {tier} tier at capacity "
                            f"({len(pending)} in flight); skipping sends."
                        )
                for t, futs in self._pending.items():
                    self._pending[t] = {f for f in futs if not f.done()}
                    self.pend.set(len(self._pending[t]), tags={"tier": t})
                # Absolute-deadline pacing: relative sleeps accumulate timer
                # overshoot (~2ms/tick == only ~17.4 of 18 QPS). On a stall
                # of the loop itself, resync instead of bursting to catch up.
                next_send += send_interval_s
                now = loop.time()
                if next_send < now - 1.0:
                    next_send = now
                if next_send > now:
                    await asyncio.sleep(next_send - now)

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
                if resp.status != 200:
                    logger.warning(f"baseline {ep.name} -> HTTP {resp.status}")
        except Exception:
            self.bad.inc(tags=tags)
            logger.exception(f"baseline {ep.name} request error")
        finally:
            self.lat.set(asyncio.get_event_loop().time() - t0, tags=tags)

    def start(self):
        if self._loop_task is None:
            self._loop_task = run_background_task(self._run_loop())
            logger.info(
                f"BaselinePinger started: {self.total_qps} QPS -> {self.target_base_url}"
            )
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
    qps = float(os.environ.get("BASELINE_QPS", args.total_qps))
    return Router.bind(
        BaselinePinger.options(user_config={"total_qps": qps}).bind(args.target_base_url, token)
    )
