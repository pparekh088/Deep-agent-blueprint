"""TEMPLATE_CORE — job queue abstraction over arq.

The API tier only ever enqueues and reads depth; the worker tier consumes.
The protocol keeps the API testable without a live Redis/arq worker.
"""

from __future__ import annotations

from typing import Any, Protocol


class QueueUnavailable(Exception):
    pass


class JobQueue(Protocol):
    async def enqueue_research(self, job_id: str) -> None: ...

    async def depth(self) -> int: ...


class ArqJobQueue:
    """Production queue: arq over Redis. ``research_job`` is registered in
    ``app.worker.main.WorkerSettings``."""

    def __init__(self, pool: Any, queue_name: str = "arq:queue") -> None:
        self._pool = pool
        self._queue_name = queue_name

    async def enqueue_research(self, job_id: str) -> None:
        try:
            job = await self._pool.enqueue_job("research_job", job_id, _job_id=f"research:{job_id}")
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable("failed to enqueue research job") from exc
        if job is None:
            # arq dedupes on _job_id; our job_ids are fresh UUIDs so this
            # only happens on a duplicated submit race.
            raise QueueUnavailable(f"job {job_id} already enqueued")

    async def depth(self) -> int:
        return int(await self._pool.zcard(self._queue_name))

    async def aclose(self) -> None:
        await self._pool.aclose()
