"""Prometheus metrics.

R11: `prometheus_client`'s default registry raises on duplicate registration, and every test in
this suite builds its own app. Each `Metrics` therefore owns a private `CollectorRegistry`, and
`/metrics` renders from that one.
"""

from __future__ import annotations

from typing import Literal

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

SendOutcome = Literal["sent", "failed", "queued"]
RefreshResult = Literal["ok", "error", "cached"]

#: Buckets chosen around the cost of one Gmail `messages.send` call.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class Metrics:
    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.sends = Counter(
            "fmaiily_sends_total",
            "Send attempts by terminal outcome.",
            ("account", "outcome", "source"),
            registry=self.registry,
        )
        self.failures = Counter(
            "fmaiily_send_failures_total",
            "Send failures by stable error code.",
            ("account", "error_code"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "fmaiily_send_duration_seconds",
            "Wall time of a single Gmail send attempt.",
            ("account",),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "fmaiily_queue_depth",
            "Jobs waiting in the send queue.",
            registry=self.registry,
        )
        self.quota_remaining = Gauge(
            "fmaiily_quota_remaining",
            "Remaining messages or recipients in the rolling 24 hour window.",
            ("account", "resource"),
            registry=self.registry,
        )
        self.tokens_refreshed = Counter(
            "fmaiily_tokens_refreshed_total",
            "OAuth access token refresh attempts by result.",
            ("account", "result"),
            registry=self.registry,
        )
        self.worker_iterations = Counter(
            "fmaiily_worker_iterations_total",
            "Worker loop iterations by resulting action.",
            ("action",),
            registry=self.registry,
        )
        self.http_requests = Counter(
            "fmaiily_http_requests_total",
            "HTTP requests handled, by method, route template, and status.",
            ("method", "path", "status"),
            registry=self.registry,
        )

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST

    def render(self) -> str:
        return generate_latest(self.registry).decode("utf-8")

    # ------------------------------------------------------------------ records

    def record_send(
        self,
        account: str,
        outcome: SendOutcome,
        *,
        source: str,
        error_code: str | None = None,
    ) -> None:
        if outcome not in ("sent", "failed", "queued"):
            raise ValueError(f"unknown send outcome: {outcome!r}")
        self.sends.labels(account=account, outcome=outcome, source=source).inc()
        if outcome == "failed" and error_code:
            self.failures.labels(account=account, error_code=error_code).inc()

    def observe_send_duration(self, account: str, seconds: float) -> None:
        self.duration.labels(account=account).observe(max(seconds, 0.0))

    def set_queue_depth(self, depth: int) -> None:
        self.queue_depth.set(float(max(depth, 0)))

    def set_quota_remaining(self, account: str, resource: str, remaining: int) -> None:
        self.quota_remaining.labels(account=account, resource=resource).set(
            float(max(remaining, 0))
        )

    def record_token_refresh(self, account: str, result: RefreshResult) -> None:
        self.tokens_refreshed.labels(account=account, result=result).inc()

    def record_worker_iteration(self, action: str) -> None:
        self.worker_iterations.labels(action=action).inc()

    def record_http_request(self, method: str, path: str, status: int) -> None:
        self.http_requests.labels(method=method, path=path, status=str(status)).inc()


__all__ = ["LATENCY_BUCKETS", "Metrics", "RefreshResult", "SendOutcome"]
