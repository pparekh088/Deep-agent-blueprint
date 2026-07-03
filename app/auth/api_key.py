"""TEMPLATE_CORE — X-Api-Key dependency (Layer 1: consumer -> this service).

* Multiple simultaneously active keys → zero-downtime rotation.
* Constant-time comparison (hmac.compare_digest) against every configured key
  — no early exit on the first match, so timing does not leak which key id
  matched.
* The consumer key's ID (never the key) is bound to the logging context for
  auditability of every request.
"""

from __future__ import annotations

import hmac
import logging
from typing import Mapping

from fastapi import Request

from app.errors import ApiError
from app.models.schemas import ErrorCode
from app.observability.correlation import consumer_id_var

API_KEY_HEADER = "X-Api-Key"

logger = logging.getLogger(__name__)


def verify_api_key(presented: str, api_keys: Mapping[str, str]) -> str | None:
    """Return the consumer ID for a valid key, else None. Compares against
    every configured key so timing is independent of which key matches."""
    presented_bytes = presented.encode("utf-8")
    matched: str | None = None
    for consumer_id, secret in api_keys.items():
        if hmac.compare_digest(secret.encode("utf-8"), presented_bytes):
            matched = consumer_id
    return matched


async def require_api_key(request: Request) -> str:
    """FastAPI dependency applied to every endpoint except bare liveness."""
    deps = request.app.state.deps
    presented = request.headers.get(API_KEY_HEADER)
    if not presented:
        raise ApiError(401, ErrorCode.INVALID_API_KEY, "Missing X-Api-Key header.")

    consumer_id = verify_api_key(presented, deps.settings.api_keys)
    if consumer_id is None:
        logger.warning("rejected API key", extra={"event": "auth_rejected"})
        raise ApiError(401, ErrorCode.INVALID_API_KEY, "Invalid API key.")

    consumer_id_var.set(consumer_id)
    return consumer_id
