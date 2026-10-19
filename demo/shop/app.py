"""A small shop to practise incidents on.

One image runs as three services, chosen by SERVICE_ROLE:
  checkout  takes orders, calls payments, has a coupon endpoint
  payments  the payment gateway checkout depends on
  search    product search whose latency depends on load per replica

Faults are switched on through /admin endpoints (see demo/inject.py), and the
same endpoints are the "real" recovery actions: deploy a version, flip a
feature flag, scale replicas. Every service exports Prometheus metrics, JSON
logs on stdout (collected into Loki) and traces (sent to Jaeger).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

ROLE = os.environ.get("SERVICE_ROLE", "checkout")
SERVICE = os.environ.get("SERVICE_NAME", ROLE)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "demo-admin-token")
PAYMENTS_URL = os.environ.get("PAYMENTS_URL", "http://payments:8000")
INITIAL_VERSION = os.environ.get("APP_VERSION", "v2.3.9")

# v2.4.0 shipped a bug in the pay handler. v2.3.0 predates the coupon API, so
# rolling back that far "fixes" checkout but breaks coupons.
BUGGY_VERSIONS = {"v2.4.0"}
NO_COUPON_VERSIONS = {"v2.3.0", "v2.2.0"}
BUG_ERROR_RATE = 0.3

# --- telemetry ------------------------------------------------------------------

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2, 3, 5),
)
ERRORS = Counter("app_errors_total", "Errors by signature", ["service", "signature"])
ORDERS = Counter("orders_total", "Completed orders", ["service"])
CPU = Gauge("app_cpu_utilization", "Simulated CPU utilization (0-1)", ["service"])
INFO = Gauge("app_version_info", "Running version", ["service", "version"])
REPLICAS = Gauge("app_replicas", "Simulated replica count", ["service"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)), "level": record.levelname,
                 "service": SERVICE, "version": state.version, "msg": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("shop")
log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False


def _tracer():
    from opentelemetry import trace

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    return trace.get_tracer("shop")


tracer = _tracer()

# --- state -------------------------------------------------------------------------


@dataclass
class State:
    version: str = INITIAL_VERSION
    payments_outage: bool = False
    traffic_multiplier: float = 1.0
    replicas: int = 4
    flags: dict[str, bool] = field(default_factory=dict)
    # Share of requests (0-100) a flag applies to; missing means all of them.
    flag_rollout: dict[str, float] = field(default_factory=dict)

    def flag_on(self, name: str) -> bool:
        return self.flags.get(name, False) and random.random() * 100 < self.flag_rollout.get(name, 100.0)

    def utilization(self) -> float:
        # 4 replicas at normal traffic run at 50%.
        return 0.5 * self.traffic_multiplier * 4 / max(self.replicas, 1)


state = State()


def _publish_gauges() -> None:
    INFO.clear()
    INFO.labels(SERVICE, state.version).set(1)
    REPLICAS.labels(SERVICE).set(state.replicas)
    CPU.labels(SERVICE).set(min(state.utilization(), 1.0) if ROLE == "search" else 0.4)


_publish_gauges()


class Failure(Exception):
    def __init__(self, code: int, signature: str, message: str):
        self.code, self.signature, self.message = code, signature, message


# --- app ---------------------------------------------------------------------------

app = FastAPI(title=f"shop-{SERVICE}")


@app.exception_handler(Failure)
async def on_failure(request: Request, f: Failure) -> JSONResponse:
    # The middleware reads these headers to count and log the error.
    return JSONResponse({"error": f.message}, status_code=f.code,
                        headers={"x-error-signature": f.signature, "x-error-message": f.message})


@app.middleware("http")
async def observe(request: Request, call_next):
    from opentelemetry import propagate, trace

    route = request.url.path
    if route in ("/metrics", "/healthz") or route.startswith("/admin"):
        return await call_next(request)
    start = time.perf_counter()
    with tracer.start_as_current_span(
        f"{request.method} {route}", context=propagate.extract(request.headers), kind=trace.SpanKind.SERVER
    ) as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        response = await call_next(request)
        code = response.status_code
        signature = response.headers.get("x-error-signature")
        message = response.headers.get("x-error-message")
        span.set_attribute("http.status_code", code)
        if code >= 500:
            span.set_status(trace.Status(trace.StatusCode.ERROR, message or ""))
    elapsed = time.perf_counter() - start
    REQUESTS.labels(SERVICE, route, str(code)).inc()
    LATENCY.labels(SERVICE, route).observe(elapsed)
    fields = {"route": route, "status": code, "duration_ms": round(elapsed * 1000, 1), "trace_id": trace_id}
    if signature:
        ERRORS.labels(SERVICE, signature).inc()
        log.error(message, extra={"fields": {**fields, "error_signature": signature}})
    elif random.random() < 0.05:  # sample healthy requests so logs stay small
        log.info("request ok", extra={"fields": fields})
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# checkout ------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=1.0)
    return _client


@app.get("/checkout")
async def checkout() -> dict:
    from opentelemetry import propagate

    await asyncio.sleep(random.uniform(0.02, 0.05))
    if state.version in BUGGY_VERSIONS and random.random() < BUG_ERROR_RATE:
        raise Failure(500, "TypeError: 'NoneType' object is not subscriptable in pay_handler",
                      "pay_handler crashed: cart.payment_method is None")
    if state.flag_on("use_backup_payment_provider"):
        await asyncio.sleep(random.uniform(0.03, 0.06))
        provider = "backup"
    else:
        headers: dict[str, str] = {}
        propagate.inject(headers)
        with tracer.start_as_current_span("POST payments /charge"):
            try:
                r = await client().get(f"{PAYMENTS_URL}/charge", headers=headers)
            except httpx.HTTPError as e:
                raise Failure(502, "PaymentGatewayError: unreachable", f"payment gateway unreachable: {e!r}") from None
        if r.status_code != 200:
            raise Failure(502, f"PaymentGatewayError: {r.status_code}",
                          f"payment gateway returned {r.status_code}")
        provider = "primary"
    ORDERS.labels(SERVICE).inc()
    return {"order": "ok", "provider": provider}


@app.get("/coupon")
async def coupon() -> dict:
    await asyncio.sleep(random.uniform(0.01, 0.03))
    if state.version in NO_COUPON_VERSIONS:
        raise Failure(500, "AttributeError: 'Cart' object has no attribute 'apply_coupon_v2'",
                      "coupon API not available in this version")
    return {"coupon": "applied"}


# payments ------------------------------------------------------------------------


@app.get("/charge")
async def charge() -> dict:
    if state.payments_outage:
        await asyncio.sleep(random.uniform(0.2, 0.4))
        raise Failure(503, "UpstreamUnavailable: card network", "card network unavailable")
    await asyncio.sleep(random.uniform(0.01, 0.03))
    return {"charged": True}


# search --------------------------------------------------------------------------


@app.get("/search")
async def search() -> dict:
    u = state.utilization()
    # Queueing: latency explodes as utilization approaches 100%.
    await asyncio.sleep(0.1 / (1 - min(u, 0.95)) * random.uniform(0.8, 1.2))
    if u >= 0.95 and random.random() < 0.05:
        raise Failure(503, "SearchTimeout: request queue full", "search request timed out in queue")
    return {"results": 10}


# admin: fault injection and recovery actions ----------------------------------------


def _auth(token: Optional[str]) -> None:
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "bad admin token")


class DeployIn(BaseModel):
    version: str


class FlagIn(BaseModel):
    flag: str
    value: bool
    rollout_pct: float = Field(100.0, ge=0, le=100)


class FaultsIn(BaseModel):
    payments_outage: Optional[bool] = None
    traffic_multiplier: Optional[float] = None


class ScaleIn(BaseModel):
    replicas: int


def _changed(action: Optional[str] = None, **fields) -> dict:
    """Apply gauges, and log real operational changes (deploys, flags, scaling)
    the way a deploy pipeline would. Injected faults are deliberately not
    logged: in a real incident nobody announces the cause."""
    _publish_gauges()
    if action:
        log.warning(f"change: {action}", extra={"fields": {"change_type": action, **fields}})
    return asdict(state)


@app.get("/admin/state")
def get_state(x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    return asdict(state)


@app.post("/admin/deploy")
def deploy(body: DeployIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    old, state.version = state.version, body.version
    return _changed("deploy", from_version=old, to_version=body.version)


@app.post("/admin/flags")
def set_flag(body: FlagIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    state.flags[body.flag] = body.value
    state.flag_rollout[body.flag] = body.rollout_pct
    return _changed("flag", flag=body.flag, value=body.value, rollout_pct=body.rollout_pct)


@app.post("/admin/faults")
def set_faults(body: FaultsIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    if body.payments_outage is not None:
        state.payments_outage = body.payments_outage
    if body.traffic_multiplier is not None:
        state.traffic_multiplier = body.traffic_multiplier
    return _changed()


@app.post("/admin/scale")
def scale(body: ScaleIn, x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    if not 1 <= body.replicas <= 20:
        raise HTTPException(422, "replicas must be between 1 and 20")
    old, state.replicas = state.replicas, body.replicas
    return _changed("scale", from_replicas=old, to_replicas=body.replicas)


@app.post("/admin/reset")
def reset(x_admin_token: Optional[str] = Header(None)) -> dict:
    _auth(x_admin_token)
    global state
    state = State()
    return _changed()
