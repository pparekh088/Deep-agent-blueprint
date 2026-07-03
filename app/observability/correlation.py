"""TEMPLATE_CORE — X-Correlation-Id middleware and contextvars propagation.

One correlation ID spans the whole workflow: submit -> queue -> worker ->
poll -> approve -> execute. The caller's ID is used verbatim when supplied;
otherwise we mint a UUID. The ID rides on contextvars so every log line in
both the API and worker processes carries it automatically — it is never
passed through function signatures.

A separate per-request ``request_id`` identifies the individual HTTP call;
``correlation_id`` groups the workflow.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterator

CORRELATION_HEADER = "X-Correlation-Id"

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
# True when the caller supplied the header (vs. one we generated). /execute
# uses this to fall back to the proposal's stored correlation ID.
correlation_from_header_var: ContextVar[bool] = ContextVar("correlation_from_header", default=False)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
action_id_var: ContextVar[str | None] = ContextVar("action_id", default=None)
consumer_id_var: ContextVar[str | None] = ContextVar("consumer_id", default=None)

_access_logger = logging.getLogger("app.http")


def new_id() -> str:
    return str(uuid.uuid4())


def current_correlation_id() -> str:
    """Return the active correlation ID, minting one if none is bound yet."""
    cid = correlation_id_var.get()
    if cid is None:
        cid = new_id()
        correlation_id_var.set(cid)
    return cid


def downstream_headers() -> dict[str, str]:
    """Headers to attach to every downstream HTTP/MCP call."""
    return {CORRELATION_HEADER: current_correlation_id()}


@contextmanager
def bound_context(
    *,
    correlation_id: str | None = None,
    request_id: str | None = None,
    job_id: str | None = None,
    action_id: str | None = None,
    consumer_id: str | None = None,
) -> Iterator[None]:
    """Bind observability context for a block of work (worker job runs)."""
    pairs = [
        (correlation_id_var, correlation_id),
        (request_id_var, request_id),
        (job_id_var, job_id),
        (action_id_var, action_id),
        (consumer_id_var, consumer_id),
    ]
    tokens = [(var, var.set(value)) for var, value in pairs if value is not None]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class CorrelationMiddleware:
    """Pure ASGI middleware: bind correlation/request IDs, echo the header,
    and emit one canonical ``http_request`` access-log line per request."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        supplied = headers.get(CORRELATION_HEADER.lower())
        cid = supplied or new_id()

        tok_cid = correlation_id_var.set(cid)
        tok_sup = correlation_from_header_var.set(supplied is not None)
        tok_rid = request_id_var.set(new_id())

        started = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # Handlers may rebind the var (e.g. poll: the job's stored ID
                # is authoritative) — read it at send time, not entry time.
                active = correlation_id_var.get() or cid
                raw = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != CORRELATION_HEADER.lower().encode("latin-1")
                ]
                raw.append((CORRELATION_HEADER.encode("latin-1"), active.encode("latin-1")))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in ("/health/live",):  # keep probe noise out of Splunk
                _access_logger.info(
                    "%s %s" % (scope.get("method", "-"), path),
                    extra={
                        "event": "http_request",
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "status": status_holder.get("status"),
                    },
                )
            request_id_var.reset(tok_rid)
            correlation_from_header_var.reset(tok_sup)
            correlation_id_var.reset(tok_cid)
