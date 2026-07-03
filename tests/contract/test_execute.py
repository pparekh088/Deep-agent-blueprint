"""Contract: /execute — proposal binding, payload verification, idempotency,
expiry, stale-target detection, and read-only research enforcement."""

from __future__ import annotations

import uuid

import pytest
from tests.contract.cases import ContractCase


@pytest.fixture
async def completed(env):
    """Completed research with at least one proposal (skips research-only domains)."""
    if not env.case.has_actions:
        pytest.skip("research-only domain: no /execute path")
    return await env.research_completed()


def _first_action(completed_body):
    return completed_body["proposed_actions"][0]


async def test_execute_happy_path_consumes_proposal(env, completed):
    action = _first_action(completed)
    response = await env.execute(action["action_id"], action["payload"], idem_key="idem-1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "executed"
    assert body["action_id"] == action["action_id"]
    assert body["result"]
    assert body["idempotent_replay"] is False

    # Consumed: a NEW idempotency key must not re-execute the same action.
    replayed = await env.execute(action["action_id"], action["payload"], idem_key="idem-2")
    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "ACTION_EXPIRED"


async def test_non_editable_payload_drift_is_rejected(env, completed):
    action = _first_action(completed)
    tampered = dict(action["payload"])
    tampered["issue_key" if "issue_key" in tampered else "target"] = "PROJ-999"
    response = await env.execute(action["action_id"], tampered, idem_key="idem-3")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYLOAD_MISMATCH"


async def test_editable_fields_may_be_edited(env, completed):
    editable_actions = [
        action for action in completed["proposed_actions"] if action.get("editable_fields")
    ]
    if not editable_actions:
        pytest.skip("case proposes no editable actions")
    action = editable_actions[0]
    edited = dict(action["payload"])
    edited[action["editable_fields"][0]] = "Human-reworded text before approval."
    response = await env.execute(action["action_id"], edited, idem_key="idem-4")
    assert response.status_code == 200, response.text


async def test_idempotent_replay_returns_recorded_result(env, completed):
    action = _first_action(completed)
    first = await env.execute(action["action_id"], action["payload"], idem_key="idem-5")
    assert first.status_code == 200

    replay = await env.execute(action["action_id"], action["payload"], idem_key="idem-5")
    assert replay.status_code == 200
    body = replay.json()
    assert body["idempotent_replay"] is True
    assert body["result"] == first.json()["result"]


async def test_idempotency_key_reuse_for_different_request_conflicts(env, completed):
    actions = completed["proposed_actions"]
    first = await env.execute(actions[0]["action_id"], actions[0]["payload"], idem_key="idem-6")
    assert first.status_code == 200

    other = actions[1] if len(actions) > 1 else actions[0]
    payload = dict(other["payload"]) if other is actions[0] else other["payload"]
    if other is actions[0]:
        payload["__mutated__"] = True
    conflict = await env.execute(other["action_id"], payload, idem_key="idem-6")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENT_REPLAY"


async def test_expired_proposal_rejected(env, completed):
    action = _first_action(completed)
    await env.store.delete_proposal(action["action_id"])  # simulate TTL expiry
    response = await env.execute(action["action_id"], action["payload"], idem_key="idem-7")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACTION_EXPIRED"


async def test_unknown_action_rejected_without_oracle(env, completed):
    response = await env.execute(str(uuid.uuid4()), {"x": 1}, idem_key="idem-8")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACTION_EXPIRED"


async def test_stale_target_rejected_when_preconditions_drift(env, completed):
    if env.case.control is None:
        pytest.skip("case exposes no downstream control")
    action = next(
        a for a in completed["proposed_actions"] if a["preconditions"].get("expected_status")
    )
    env.case.control["status"] = "Done"  # someone changed the issue meanwhile
    response = await env.execute(action["action_id"], action["payload"], idem_key="idem-9")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "STALE_TARGET"
    assert body["error"]["details"]


async def test_cross_principal_execute_rejected(env, completed):
    from tests.contract.conftest import API_KEYS

    action = _first_action(completed)
    if env.case.user_token is not None:
        headers = {"X-Api-Key": API_KEYS["consumer-a"], "X-User-Token": "attacker-token-777",
                   "Idempotency-Key": "idem-10"}
        response = await env.client.post(
            "/execute",
            json={
                "action_id": action["action_id"], "session_id": "sess-1",
                "approved_payload": action["payload"],
                "approval": {"approved_by": "u", "approved_at": "2026-07-03T10:00:00Z"},
            },
            headers=headers,
        )
    else:
        response = await env.execute(
            action["action_id"], action["payload"], idem_key="idem-10",
            api_key=API_KEYS["consumer-b"],
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACTION_EXPIRED"  # indistinguishable from expiry


async def test_actions_endpoints_report_and_reject(env, completed):
    action = _first_action(completed)
    fetched = await env.client.get(f"/actions/{action['action_id']}", headers=env.headers())
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "proposed"

    rejected = await env.client.delete(f"/actions/{action['action_id']}", headers=env.headers())
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    gone = await env.client.get(f"/actions/{action['action_id']}", headers=env.headers())
    assert gone.status_code == 409
    assert gone.json()["error"]["code"] == "ACTION_EXPIRED"


async def test_research_toolset_is_structurally_read_only(env):
    """Read-only enforcement is structural: the callables handed to the agent
    factory are exactly the adapter's read tools — no mutation executor is
    reachable from inside a research run."""
    from app.adapters.base import DownstreamCredentials

    await env.research_completed()
    assert env.captured_factory_calls, "agent factory was never invoked"

    adapter = env.worker_deps.adapter
    expected = {tool.__name__ for tool in adapter.read_tools(DownstreamCredentials())}
    mutation_names = set(adapter.action_schemas()) | {"execute", "check_preconditions"}
    for call in env.captured_factory_calls:
        bound = {getattr(tool, "__name__", str(tool)) for tool in call["tools"]}
        assert bound == expected
        assert not bound & mutation_names
