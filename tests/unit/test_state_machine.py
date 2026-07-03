from __future__ import annotations

import pytest

from app.models.schemas import JobError, JobRecord, JobStatus, ErrorCode
from app.state.redis_store import InvalidTransition, JobNotFound


def _record(job_id: str = "job-1") -> JobRecord:
    return JobRecord(
        job_id=job_id, domain="jira", task="t", session_id="s",
        consumer_id="consumer-a", correlation_id="corr-1",
    )


async def test_happy_path_transitions(store):
    await store.create_job(_record())
    running = await store.transition("job-1", JobStatus.RUNNING, attempt=1)
    assert running.started_at is not None
    done = await store.transition("job-1", JobStatus.COMPLETED)
    assert done.finished_at is not None


@pytest.mark.parametrize(
    "path,illegal",
    [
        ([JobStatus.RUNNING, JobStatus.COMPLETED], JobStatus.RUNNING),
        ([JobStatus.RUNNING, JobStatus.FAILED], JobStatus.COMPLETED),
        ([JobStatus.CANCELLED], JobStatus.RUNNING),
        ([], JobStatus.COMPLETED),  # queued cannot complete without running
    ],
)
async def test_illegal_transitions_rejected(store, path, illegal):
    await store.create_job(_record())
    for status in path:
        await store.transition("job-1", status)
    with pytest.raises(InvalidTransition):
        await store.transition("job-1", illegal)


async def test_retry_reentry_running_to_running_is_legal(store):
    await store.create_job(_record())
    await store.transition("job-1", JobStatus.RUNNING, attempt=1)
    record = await store.transition("job-1", JobStatus.RUNNING, attempt=2)
    assert record.attempt == 2


async def test_terminal_transition_purges_token_and_cancel_flag(store):
    await store.create_job(_record())
    await store.stage_token("job-1", "ciphertext-blob")
    await store.request_cancel("job-1")
    await store.transition("job-1", JobStatus.CANCELLED)

    assert await store.load_token("job-1") is None
    assert await store.is_cancel_requested("job-1") is False


async def test_error_recorded_on_failure(store):
    await store.create_job(_record())
    await store.transition("job-1", JobStatus.RUNNING)
    failed = await store.transition(
        "job-1", JobStatus.FAILED,
        error=JobError(code=ErrorCode.JOB_TIMEOUT, message="over budget"),
    )
    assert failed.error.code is ErrorCode.JOB_TIMEOUT


async def test_transition_on_missing_job_raises(store):
    with pytest.raises(JobNotFound):
        await store.transition("nope", JobStatus.RUNNING)
