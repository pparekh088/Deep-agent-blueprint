"""TEMPLATE_CORE — POST /research (enqueue), GET /research/{job_id} (poll),
DELETE /research/{job_id} (cancel).

The API tier never runs an agent. Submit = authenticate, validate, stage the
PAT (USER_PAT only, encrypted), enqueue, return 202. Poll/cancel authorize by
salted principal hash (USER_PAT) or consumer binding (everything else); an
authorization mismatch returns 404 JOB_NOT_FOUND deliberately, so job IDs are
not an existence oracle across principals.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request

from app.adapters.base import AuthMode
from app.auth.api_key import require_api_key
from app.auth.user_token import get_user_token
from app.errors import ApiError
from app.models.schemas import (
    TERMINAL_STATES,
    ErrorCode,
    JobError,
    JobRecord,
    JobStatus,
    JobStatusResponse,
    ResearchAccepted,
    ResearchRequest,
)
from app.observability.correlation import (
    correlation_id_var,
    current_correlation_id,
    job_id_var,
)
from app.observability.logging import log_event
from app.state.queue import QueueUnavailable
from app.state.token_vault import TokenVaultError, principal_hash

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research"])


@router.post("", status_code=202, response_model=ResearchAccepted)
async def submit_research(
    body: ResearchRequest,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
) -> ResearchAccepted:
    deps = request.app.state.deps
    job_id = str(uuid.uuid4())
    job_id_var.set(job_id)
    correlation_id = current_correlation_id()

    record = JobRecord(
        job_id=job_id,
        domain=deps.settings.domain,
        task=body.task,
        session_id=body.session_id,
        context=body.context,
        constraints=body.constraints,
        consumer_id=consumer_id,
        principal_hash=(
            principal_hash(user_token, deps.settings.principal_hash_salt) if user_token else None
        ),
        correlation_id=correlation_id,
        max_attempts=deps.settings.job_max_attempts,
    )

    if deps.adapter.auth_mode is AuthMode.USER_PAT:
        # Documented exception to the no-persistence rule (ADR-0005): stage
        # the PAT as Key Vault-wrapped ciphertext for the deferred worker.
        # Fail CLOSED if encryption is unavailable — never stage weaker.
        try:
            ciphertext = await deps.vault.encrypt(user_token)  # type: ignore[arg-type]
        except TokenVaultError as exc:
            raise ApiError(
                503,
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Token protection is unavailable; research cannot be accepted.",
            ) from exc
        await deps.store.stage_token(job_id, ciphertext)

    await deps.store.create_job(record)
    try:
        await deps.queue.enqueue_research(job_id)
    except QueueUnavailable as exc:
        # Roll back so nothing secret outlives the failed submit.
        await deps.store.purge_token(job_id)
        await deps.store.transition(
            job_id,
            JobStatus.FAILED,
            error=JobError(
                code=ErrorCode.DEPENDENCY_UNAVAILABLE, message="enqueue failed at submit"
            ),
        )
        raise ApiError(
            503, ErrorCode.DEPENDENCY_UNAVAILABLE, "Job queue is unavailable; retry later."
        ) from exc

    log_event(logger, "job_submitted", message=f"session_id={body.session_id}")
    return ResearchAccepted(
        job_id=job_id,
        poll_url=f"/research/{job_id}",
        estimated_wait_s=deps.settings.estimated_wait_s,
        correlation_id=correlation_id,
    )


async def _authorized_job(
    request: Request, job_id: str, consumer_id: str, user_token: str | None
) -> JobRecord:
    """Shared poll/cancel authorization. Mismatches are 404 by design."""
    deps = request.app.state.deps
    record = await deps.store.get_job(job_id)
    if record is None:
        raise ApiError(404, ErrorCode.JOB_NOT_FOUND, f"Unknown or expired job: {job_id}")

    if deps.adapter.auth_mode is AuthMode.USER_PAT:
        presented = principal_hash(user_token or "", deps.settings.principal_hash_salt)
        if presented != record.principal_hash:
            log_event(logger, "poll_principal_mismatch", level=logging.WARNING, job_id=job_id)
            raise ApiError(404, ErrorCode.JOB_NOT_FOUND, f"Unknown or expired job: {job_id}")
    elif record.consumer_id != consumer_id:
        log_event(logger, "poll_consumer_mismatch", level=logging.WARNING, job_id=job_id)
        raise ApiError(404, ErrorCode.JOB_NOT_FOUND, f"Unknown or expired job: {job_id}")

    # The job's stored correlation ID is authoritative — echo it regardless
    # of what (if anything) the caller sent on this poll.
    correlation_id_var.set(record.correlation_id)
    job_id_var.set(job_id)
    return record


def _status_response(record: JobRecord, *, cancel_requested: bool = False) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=record.job_id,
        status=record.status,
        session_id=record.session_id,
        correlation_id=record.correlation_id,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        attempt=record.attempt,
        cancel_requested=cancel_requested,
        progress=record.progress if record.status is JobStatus.RUNNING else None,
        error=record.error,
    )


@router.get("/{job_id}", response_model=JobStatusResponse, response_model_exclude_none=True)
async def poll_research(
    job_id: str,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
) -> JobStatusResponse:
    deps = request.app.state.deps
    record = await _authorized_job(request, job_id, consumer_id, user_token)
    response = _status_response(
        record, cancel_requested=await deps.store.is_cancel_requested(job_id)
    )

    if record.status is JobStatus.COMPLETED:
        result = await deps.store.get_result(job_id)
        if result is not None:  # may have outlived RESULT_TTL_S — job status still serves
            response.findings = result.findings
            response.proposed_actions = result.proposed_actions
    return response


@router.delete("/{job_id}", response_model=JobStatusResponse, response_model_exclude_none=True)
async def cancel_research(
    job_id: str,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
) -> JobStatusResponse:
    deps = request.app.state.deps
    record = await _authorized_job(request, job_id, consumer_id, user_token)

    if record.status in TERMINAL_STATES:
        return _status_response(record)  # idempotent no-op

    # Purge the staged token IMMEDIATELY — before the worker even notices.
    await deps.store.purge_token(job_id)

    if record.status is JobStatus.QUEUED:
        # Not picked up yet: finalize now; the worker skips cancelled records.
        await deps.store.request_cancel(job_id)
        record = await deps.store.transition(job_id, JobStatus.CANCELLED)
        return _status_response(record)

    # RUNNING: best-effort — the worker checks the flag between agent steps.
    await deps.store.request_cancel(job_id)
    log_event(logger, "job_cancel_requested", job_id=job_id)
    return _status_response(record, cancel_requested=True)
