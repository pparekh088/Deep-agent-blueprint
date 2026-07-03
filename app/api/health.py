"""TEMPLATE_CORE — health endpoints.

GET /health/live  — bare liveness probe, the ONLY unauthenticated endpoint.
GET /health       — authenticated: Redis reachability, queue depth (the KEDA
                    scaling signal), and LLM configuration state. Returns 503
                    when Redis is down so readiness gates traffic.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from app.auth.api_key import require_api_key
from app.llm.azure import llm_configured

live_router = APIRouter(tags=["health"])
router = APIRouter(tags=["health"])


@live_router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
async def health(
    request: Request,
    response: Response,
    consumer_id: str = Depends(require_api_key),
) -> dict[str, Any]:
    deps = request.app.state.deps

    redis_ok = await deps.store.ping()
    queue_depth: int | None = None
    if redis_ok:
        try:
            queue_depth = await deps.queue.depth()
        except Exception:  # noqa: BLE001 — health must degrade, not raise
            queue_depth = None

    checks = {
        "redis": "ok" if redis_ok else "unreachable",
        "queue_depth": queue_depth,
        "llm": "configured" if llm_configured(deps.settings) else "unconfigured",
    }
    healthy = redis_ok
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "domain": deps.settings.domain,
        "checks": checks,
    }
