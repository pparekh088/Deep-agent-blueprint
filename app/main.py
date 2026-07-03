"""TEMPLATE_CORE — FastAPI app factory: middleware, routers, error envelope,
and dependency wiring.

Two construction modes:
* ``create_app()`` — production: resources (Redis, queue pool, vault,
  adapter) are built from env config inside the lifespan.
* ``create_app(deps=...)`` — tests/embedding: pre-built AppDeps are injected
  and owned by the caller.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import TEMPLATE_VERSION
from app.adapters.base import DomainAdapter
from app.config import Settings, get_settings
from app.errors import ApiError
from app.models.schemas import ErrorCode, ErrorDetail, ErrorResponse
from app.observability.correlation import CorrelationMiddleware, current_correlation_id
from app.observability.logging import configure_logging
from app.state.queue import JobQueue
from app.state.redis_store import RedisStore
from app.state.token_vault import BaseTokenVault

logger = logging.getLogger(__name__)


@dataclass
class AppDeps:
    settings: Settings
    store: RedisStore
    vault: BaseTokenVault
    adapter: DomainAdapter
    queue: JobQueue


def _error_response(status_code: int, code: ErrorCode, message: str, details=None) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details),
        correlation_id=current_correlation_id(),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    owns_resources = getattr(app.state, "deps", None) is None
    if owns_resources:
        from arq import create_pool
        from arq.connections import RedisSettings
        from redis.asyncio import Redis

        from app.adapters import build_adapter
        from app.state.queue import ArqJobQueue
        from app.state.token_vault import build_token_vault

        settings = get_settings()
        configure_logging(settings)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        app.state.deps = AppDeps(
            settings=settings,
            store=RedisStore(redis, settings),
            vault=build_token_vault(settings),
            adapter=build_adapter(settings),
            queue=ArqJobQueue(pool, queue_name=settings.queue_name),
        )
        app.state._redis = redis
        logger.info(
            "api started",
            extra={"event": "api_started", "message": f"domain={settings.domain}"},
        )
    try:
        yield
    finally:
        if owns_resources:
            await app.state.deps.vault.aclose()
            await app.state.deps.queue.aclose()  # type: ignore[attr-defined]
            await app.state._redis.aclose()


def create_app(deps: AppDeps | None = None) -> FastAPI:
    app = FastAPI(
        title="Domain Deep Agent Service",
        version=TEMPLATE_VERSION,
        lifespan=_lifespan,
        # The interactive docs stay off outside dev: the API is consumer-to-
        # service only and sits behind APIM.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if deps is not None:
        app.state.deps = deps
        configure_logging(deps.settings)
    else:
        app.state.deps = None

    app.add_middleware(CorrelationMiddleware)

    from app.api import execute, health, research

    app.include_router(health.live_router)  # unauthenticated liveness only
    app.include_router(health.router)
    app.include_router(research.router)
    app.include_router(execute.router)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            422,
            ErrorCode.VALIDATION_ERROR,
            "Request failed validation.",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals (or secrets riding in exception text) to the
        # caller; the log line carries the redacted traceback.
        logger.error("unhandled error", exc_info=exc, extra={"event": "unhandled_error"})
        return _error_response(500, ErrorCode.INTERNAL_ERROR, "Internal error.")

    return app


# uvicorn entrypoint: `uvicorn app.main:app`
app = create_app()
