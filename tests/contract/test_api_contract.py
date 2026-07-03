"""Contract: response shapes and the canonical error envelope."""

from __future__ import annotations

import uuid


async def test_submit_returns_202_with_contract_shape(env):
    response = await env.submit()
    assert response.status_code == 202
    body = response.json()
    assert set(body) >= {"job_id", "status", "poll_url", "estimated_wait_s", "correlation_id"}
    assert body["status"] == "queued"
    assert body["poll_url"] == f"/research/{body['job_id']}"
    uuid.UUID(body["job_id"])  # job ids are UUIDs


async def test_poll_unknown_job_is_404_with_error_envelope(env):
    response = await env.poll(str(uuid.uuid4()))
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "JOB_NOT_FOUND"
    assert body["error"]["message"]
    assert body["correlation_id"]


async def test_completed_result_matches_contract_shape(env):
    body = await env.research_completed()
    assert body["status"] == "completed"
    assert body["session_id"] == "sess-1"
    assert body["findings"]["summary"]
    assert isinstance(body["findings"]["sources"], list)

    if env.case.has_actions:
        actions = body["proposed_actions"]
        assert actions, "USER_PAT reference case must propose actions"
        for action in actions:
            assert set(action) >= {
                "action_id", "action_type", "target", "payload",
                "preview", "preconditions", "expires_at", "correlation_id",
            }
    else:
        # Research-only domain: the rogue proposal in the canned output must
        # have been dropped structurally (no schema -> never surfaced).
        assert body.get("proposed_actions") in (None, [])


async def test_validation_error_uses_canonical_envelope(env):
    response = await env.client.post("/research", json={"task": ""}, headers=env.headers())
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["correlation_id"]


async def test_missing_idempotency_key_rejected(env):
    response = await env.client.post(
        "/execute",
        json={
            "action_id": "a", "session_id": "s", "approved_payload": {},
            "approval": {"approved_by": "u", "approved_at": "2026-07-03T10:00:00Z"},
        },
        headers=env.headers(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_agent_output_never_leaks_harness_types(env):
    """The contract must not leak Deep Agents concepts."""
    body = await env.research_completed()
    dumped = str(body)
    for leaked in ("langchain", "langgraph", "deepagents", "AIMessage"):
        assert leaked not in dumped
