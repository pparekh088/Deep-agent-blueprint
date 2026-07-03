"""TEMPLATE_CORE — arq worker entrypoint.

Run with:  arq app.worker.main.WorkerSettings

This module is the ONLY place arq appears. It adapts arq's callback protocol
to the framework-free run loop in runner.py:

* ``job_try`` -> runner ``attempt``
* runner ``RetryableJobError`` -> arq ``Retry`` (exponential defer)
* arq ``max_tries`` mirrors JOB_MAX_ATTEMPTS so a worker pod dying mid-run
  redelivers the job (research is read-only, so a from-scratch re-run is safe)

KEDA scales worker replicas on the queue's sorted-set cardinality
(see BLUEPRINT.md §Failure modes).
"""

from __future__ import annotations

import logging
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.adapters import build_adapter
from app.agent.factory import get_agent_factory
from app.config import get_settings
from app.llm.azure import build_llm
from app.observability.logging import configure_logging, log_event
from app.state.redis_store import RedisStore
from app.state.token_vault import build_token_vault
from app.worker.runner import RetryableJobError, WorkerDeps, run_research_job

logger = logging.getLogger(__name__)

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings)
    redis = Redis.from_url(_settings.redis_url, decode_responses=True)
    ctx["redis"] = redis
    ctx["deps"] = WorkerDeps(
        settings=_settings,
        store=RedisStore(redis, _settings),
        vault=build_token_vault(_settings),
        adapter=build_adapter(_settings),
        agent_factory=get_agent_factory(_settings.agent_factory),
        build_llm=build_llm,
    )
    log_event(logger, "worker_started", message=f"domain={_settings.domain}")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["deps"].vault.aclose()
    await ctx["redis"].aclose()
    log_event(logger, "worker_stopped")


async def research_job(ctx: dict[str, Any], job_id: str) -> None:
    attempt = int(ctx.get("job_try", 1))
    try:
        await run_research_job(ctx["deps"], job_id, attempt)
    except RetryableJobError as exc:
        raise Retry(defer=min(2**attempt, 30)) from exc


class WorkerSettings:
    functions = [research_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    queue_name = _settings.queue_name
    max_tries = _settings.job_max_attempts
    # Outer guard only — runner enforces the real per-attempt budget with
    # asyncio.timeout and marks the job failed:JOB_TIMEOUT itself.
    job_timeout = _settings.job_timeout_s + 60
    max_jobs = 4  # deep agent runs are LLM-bound; keep per-pod concurrency low
    health_check_interval = 60
