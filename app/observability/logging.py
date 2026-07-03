"""TEMPLATE_CORE — single-line JSON logging (Splunk-friendly) with redaction.

Rules this module enforces (see BLUEPRINT.md §Observability):

* Every line is one JSON object on stdout — no multi-line tracebacks.
  Exceptions are serialized into a single ``exception`` field.
* The canonical field set never varies: consistent keys are what make
  ``index=... correlation_id="..."`` Splunk queries work.
* Redaction is absolute: any value registered as a secret (API keys, PATs)
  is replaced with ``[REDACTED]`` in the final serialized line — message,
  extras, and exception text alike.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.observability.correlation import (
    action_id_var,
    consumer_id_var,
    correlation_id_var,
    job_id_var,
    request_id_var,
)

if TYPE_CHECKING:
    from app.config import Settings

REDACTED = "[REDACTED]"
_MIN_SECRET_LEN = 6

# Process-wide registry of secret values to scrub from every log line.
# API keys are registered at startup; user PATs the moment they are seen
# (request header dependency, worker unwrap).
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= _MIN_SECRET_LEN:
        _secrets.add(value)


def clear_secrets() -> None:
    """Test hook — never called in production code paths."""
    _secrets.clear()


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class RedactingJsonFormatter(logging.Formatter):
    """Canonical schema — field names NEVER vary; null fields are allowed."""

    def __init__(self, *, service: str, domain: str, env: str) -> None:
        super().__init__()
        self._service = service
        self._domain = domain
        self._env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self._service,
            "domain": self._domain,
            "env": self._env,
            "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
            "job_id": getattr(record, "job_id", None) or job_id_var.get(),
            "action_id": getattr(record, "action_id", None) or action_id_var.get(),
            "consumer_id": getattr(record, "consumer_id", None) or consumer_id_var.get(),
            "event": getattr(record, "event", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": getattr(record, "status", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            payload["exception"] = " | ".join(line for line in exc_text.splitlines() if line)
        return redact(json.dumps(payload, separators=(",", ":"), default=str))


def build_formatter(settings: "Settings") -> RedactingJsonFormatter:
    return RedactingJsonFormatter(
        service=settings.service_name, domain=settings.domain, env=settings.env
    )


def configure_logging(settings: "Settings") -> None:
    """Install the JSON formatter on the root logger (API and worker alike)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(settings))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    # uvicorn's own access log duplicates our http_request event — silence it.
    logging.getLogger("uvicorn.access").disabled = True

    for consumer_id, key in settings.api_keys.items():
        register_secret(key)


def log_event(
    logger: logging.Logger,
    event: str,
    message: str = "",
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a canonical lifecycle event (event name + optional schema fields)."""
    logger.log(level, message or event, extra={"event": event, **fields})
