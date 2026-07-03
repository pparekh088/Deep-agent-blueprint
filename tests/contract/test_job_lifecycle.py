"""Contract: job state machine, authorization binding, retries, timeout,
cancellation."""

from __future__ import annotations

import pytest

from tests.contract.conftest import API_KEYS, FakeAgent


async def test_full_lifecycle_queued_to_completed(env):
    submitted = await env.submit()
    job_id = submitted.json()["job_id"]

    polled = await env.poll(job_id)
    assert polled.json()["status"] == "queued"

    await env.run_worker()

    body = (await env.poll(job_id)).json()
    assert body["status"] == "completed"
    assert body["attempt"] == 1
    assert body["finished_at"]


async def test_cross_principal_poll_rejected(env):
    job_id = (await env.submit()).json()["job_id"]

    if env.case.user_token is not None:
        # USER_PAT: a different user's token must not see the job (404, not
        # 403 — job ids are no existence oracle).
        response = await env.client.get(
            f"/research/{job_id}",
            headers={
                "X-Api-Key": API_KEYS["consumer-a"],
                "X-User-Token": "some-other-users-token-999",
            },
        )
    else:
        # Non-PAT: the job is bound to the submitting consumer's API key.
        response = await env.poll(job_id, api_key=API_KEYS["consumer-b"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_owner_can_poll_after_completion_repeatedly(env):
    body = await env.research_completed()
    again = await env.poll(body["job_id"])
    assert again.status_code == 200
    assert again.json()["status"] == "completed"


async def test_worker_failure_retries_then_fails_with_diagnostic(env):
    job_id = (await env.submit()).json()["job_id"]
    failing = env.worker_deps_with(agent_factory=env.agent_factory_with(fail=True))
    await env.run_worker(failing)

    body = (await env.poll(job_id)).json()
    assert body["status"] == "failed"
    assert body["attempt"] == env.settings.job_max_attempts  # capped
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # Diagnostics never carry raw exception text (may embed response bodies).
    assert "synthetic downstream failure" not in body["error"]["message"]


async def test_timeout_marks_job_failed_with_job_timeout(env):
    job_id = (await env.submit()).json()["job_id"]
    slow = env.worker_deps_with(
        settings=env.settings.model_copy(update={"job_timeout_s": 0}),
        agent_factory=env.agent_factory_with(delay_s=0.05),
    )
    await env.run_worker(slow)

    body = (await env.poll(job_id)).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "JOB_TIMEOUT"


async def test_cancel_queued_job_finalizes_immediately(env):
    job_id = (await env.submit()).json()["job_id"]
    cancelled = await env.cancel(job_id)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # The worker must skip the cancelled record when the queue delivers it.
    await env.run_worker()
    assert (await env.poll(job_id)).json()["status"] == "cancelled"


async def test_cancel_mid_run_aborts_between_agent_steps(env):
    job_id = (await env.submit()).json()["job_id"]

    async def cancel_now() -> None:
        await env.store.request_cancel(job_id)

    factory_calls = []

    def factory(*, model, tools, instructions):
        factory_calls.append(1)
        return FakeAgent('{"summary": "should never persist"}', on_step=cancel_now)

    await env.run_worker(env.worker_deps_with(agent_factory=factory))

    body = (await env.poll(job_id)).json()
    assert body["status"] == "cancelled"
    assert body.get("findings") is None
    assert factory_calls, "agent must have started before cancellation"


async def test_cancel_is_idempotent_on_terminal_jobs(env):
    body = await env.research_completed()
    response = await env.cancel(body["job_id"])
    assert response.status_code == 200
    assert response.json()["status"] == "completed"  # no-op, state preserved
