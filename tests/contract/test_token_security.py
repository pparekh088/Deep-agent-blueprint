"""Contract: token handling — the hard security constraints.

The plaintext PAT must never appear in Redis, results, proposals, or any log
line; the staged ciphertext is purged the moment the job reaches a terminal
state. All tests auto-skip for non-USER_PAT domains (which must stage nothing
at all — asserted here too).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pat(env):
    if env.case.user_token is None:
        pytest.skip("not a USER_PAT domain")
    return env.case.user_token


async def _all_redis_values(env) -> str:
    chunks = []
    for key in await env.store.scan_keys("*"):
        value = await env.store._redis.get(key)
        chunks.append(f"{key}={value}")
    return "\n".join(chunks)


async def test_plaintext_pat_never_stored_in_redis(env, pat):
    job_id = (await env.submit()).json()["job_id"]

    token_keys = await env.store.scan_keys(f"{env.settings.domain}:tok:*")
    assert token_keys == [f"{env.settings.domain}:tok:{job_id}"], "PAT must be staged (encrypted)"
    assert pat not in await _all_redis_values(env)

    await env.run_worker()
    assert pat not in await _all_redis_values(env), "PAT leaked into results/proposals"


async def test_ciphertext_purged_on_completion_not_ttl(env, pat):
    job_id = (await env.submit()).json()["job_id"]
    await env.run_worker()
    assert await env.store.load_token(job_id) is None
    assert (await env.poll(job_id)).json()["status"] == "completed"


async def test_ciphertext_purged_immediately_on_cancellation(env, pat):
    job_id = (await env.submit()).json()["job_id"]
    await env.cancel(job_id)
    assert await env.store.load_token(job_id) is None


async def test_ciphertext_purged_on_failure(env, pat):
    job_id = (await env.submit()).json()["job_id"]
    await env.run_worker(env.worker_deps_with(agent_factory=env.agent_factory_with(fail=True)))
    assert (await env.poll(job_id)).json()["status"] == "failed"
    assert await env.store.load_token(job_id) is None


async def test_pat_and_api_keys_redacted_from_all_log_lines(env, pat):
    from tests.contract.conftest import API_KEYS

    await env.research_completed()
    joined = "\n".join(env.log_lines)
    assert env.log_lines, "expected captured log lines"
    assert pat not in joined, "plaintext PAT appeared in a log line"
    for secret in API_KEYS.values():
        assert secret not in joined, "service API key appeared in a log line"


async def test_pat_redacted_even_in_exception_paths(env, pat):
    class LeakyAgentFactory:
        def __call__(self, *, model, tools, instructions):
            raise RuntimeError(f"downstream rejected token {pat}")

    job_id = (await env.submit()).json()["job_id"]
    await env.run_worker(env.worker_deps_with(agent_factory=LeakyAgentFactory()))

    joined = "\n".join(env.log_lines)
    assert pat not in joined
    assert "[REDACTED]" in joined

    body = (await env.poll(job_id)).json()
    assert body["status"] == "failed"
    assert pat not in str(body), "PAT leaked into the error surface"


async def test_non_pat_domains_never_stage_tokens(env):
    if env.case.user_token is not None:
        pytest.skip("USER_PAT domain")
    await env.research_completed()
    assert await env.store.scan_keys(f"{env.settings.domain}:tok:*") == []
