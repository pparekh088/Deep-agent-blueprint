"""TEMPLATE_CORE — Redis persistence: job records, proposals, idempotency,
staged-token ciphertext, and cancellation flags.

Key conventions (all prefixed with the domain so several domain services can
share a Redis in lower environments):

    {domain}:job:{job_id}        job record (JSON)
    {domain}:result:{job_id}     completed result document (JSON)
    {domain}:tok:{job_id}        PAT ciphertext (USER_PAT domains only)
    {domain}:proposal:{action_id} stored proposal (JSON)
    {domain}:idem:{key}          /execute idempotency record (JSON)
    {domain}:cancel:{job_id}     cancellation flag

Job state machine (enforced here — nowhere else):

    queued ──► running ──► completed
       │          │  ├───► failed
       │          │  └───► cancelled
       │          └──► running        (retry re-entry after worker death)
       ├────► cancelled               (cancelled before pickup)
       └────► failed                  (e.g. enqueue succeeded, submit aborted)

Terminal transitions purge the staged token ciphertext and the cancel flag
immediately — TTL is only the backstop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.models.schemas import (
    TERMINAL_STATES,
    IdempotencyRecord,
    JobError,
    JobRecord,
    JobResultDoc,
    JobStatus,
    StoredProposal,
    utcnow,
)
from app.observability.logging import log_event

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.config import Settings

logger = logging.getLogger(__name__)

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, job_id: str, current: JobStatus, requested: JobStatus) -> None:
        super().__init__(f"job {job_id}: illegal transition {current.value} -> {requested.value}")
        self.current = current
        self.requested = requested


class JobNotFound(Exception):
    pass


class RedisStore:
    def __init__(self, redis: "Redis", settings: "Settings") -> None:
        self._redis = redis
        self._domain = settings.domain
        self._settings = settings
        # Jobs must outlive their result so poll never 404s while the result
        # is still readable.
        self._job_ttl = settings.result_ttl_s + settings.job_timeout_s * settings.job_max_attempts

    # ── keys ────────────────────────────────────────────────────────────────

    def _key(self, kind: str, ident: str) -> str:
        return f"{self._domain}:{kind}:{ident}"

    def token_key(self, job_id: str) -> str:
        return self._key("tok", job_id)

    # ── jobs ────────────────────────────────────────────────────────────────

    async def create_job(self, record: JobRecord) -> None:
        await self._redis.set(self._key("job", record.job_id), record.model_dump_json(), ex=self._job_ttl)

    async def get_job(self, job_id: str) -> JobRecord | None:
        raw = await self._redis.get(self._key("job", job_id))
        return JobRecord.model_validate_json(raw) if raw else None

    async def save_job(self, record: JobRecord) -> None:
        await self._redis.set(self._key("job", record.job_id), record.model_dump_json(), keepttl=True)

    async def transition(
        self,
        job_id: str,
        new_status: JobStatus,
        *,
        attempt: int | None = None,
        error: JobError | None = None,
    ) -> JobRecord:
        record = await self.get_job(job_id)
        if record is None:
            raise JobNotFound(job_id)
        if new_status not in _ALLOWED_TRANSITIONS[record.status]:
            raise InvalidTransition(job_id, record.status, new_status)

        record.status = new_status
        if attempt is not None:
            record.attempt = attempt
        if error is not None:
            record.error = error
        if new_status is JobStatus.RUNNING and record.started_at is None:
            record.started_at = utcnow()
        if new_status in TERMINAL_STATES:
            record.finished_at = utcnow()

        await self.save_job(record)

        if new_status in TERMINAL_STATES:
            # Purge staged secrets the moment the job is done. TTL is only
            # the backstop for crashes between transition and purge.
            await self.purge_token(job_id)
            await self._redis.delete(self._key("cancel", job_id))

        log_event(
            logger,
            f"job_{new_status.value}",
            job_id=job_id,
            status=new_status.value,
        )
        return record

    async def set_progress(self, job_id: str, *, last_tool: str | None, steps: int) -> None:
        record = await self.get_job(job_id)
        if record is None:
            return
        record.progress.last_tool = last_tool
        record.progress.steps = steps
        await self.save_job(record)

    # ── results ─────────────────────────────────────────────────────────────

    async def save_result(self, job_id: str, result: JobResultDoc) -> None:
        await self._redis.set(
            self._key("result", job_id), result.model_dump_json(), ex=self._settings.result_ttl_s
        )

    async def get_result(self, job_id: str) -> JobResultDoc | None:
        raw = await self._redis.get(self._key("result", job_id))
        return JobResultDoc.model_validate_json(raw) if raw else None

    # ── staged token ciphertext (USER_PAT domains only) ─────────────────────

    async def stage_token(self, job_id: str, ciphertext: str) -> None:
        # TTL = maximum job lifetime; the terminal-state purge is the real
        # deletion path, this is the backstop.
        ttl = self._settings.job_timeout_s * self._settings.job_max_attempts + 120
        await self._redis.set(self.token_key(job_id), ciphertext, ex=ttl)
        log_event(logger, "token_staged", job_id=job_id)

    async def load_token(self, job_id: str) -> str | None:
        return await self._redis.get(self.token_key(job_id))

    async def purge_token(self, job_id: str) -> None:
        removed = await self._redis.delete(self.token_key(job_id))
        if removed:
            log_event(logger, "token_purged", job_id=job_id)

    # ── cancellation ────────────────────────────────────────────────────────

    async def request_cancel(self, job_id: str) -> None:
        await self._redis.set(self._key("cancel", job_id), "1", ex=self._job_ttl)

    async def is_cancel_requested(self, job_id: str) -> bool:
        return bool(await self._redis.exists(self._key("cancel", job_id)))

    # ── proposals ───────────────────────────────────────────────────────────

    async def save_proposal(self, stored: StoredProposal) -> None:
        await self._redis.set(
            self._key("proposal", stored.action.action_id),
            stored.model_dump_json(),
            ex=self._settings.proposal_ttl_s,
        )

    async def get_proposal(self, action_id: str) -> StoredProposal | None:
        raw = await self._redis.get(self._key("proposal", action_id))
        return StoredProposal.model_validate_json(raw) if raw else None

    async def delete_proposal(self, action_id: str) -> None:
        await self._redis.delete(self._key("proposal", action_id))

    # ── idempotency ─────────────────────────────────────────────────────────

    async def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        raw = await self._redis.get(self._key("idem", key))
        return IdempotencyRecord.model_validate_json(raw) if raw else None

    async def put_idempotency(self, key: str, record: IdempotencyRecord) -> None:
        await self._redis.set(
            self._key("idem", key), record.model_dump_json(), ex=self._settings.idempotency_ttl_s
        )

    # ── health ──────────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001 — health check must never raise
            return False

    async def queue_depth(self) -> int:
        # arq's queue is a sorted set; its cardinality is the KEDA scaling signal.
        return int(await self._redis.zcard(self._settings.queue_name))

    async def scan_keys(self, pattern: str = "*") -> list[str]:
        """Diagnostics/tests only."""
        keys: list[Any] = []
        async for key in self._redis.scan_iter(match=pattern):
            keys.append(key)
        return [k.decode() if isinstance(k, bytes) else k for k in keys]
