"""Contract: one correlation ID spans submit -> queue -> worker -> poll ->
execute, echoed in headers and bodies, present on every log line."""

from __future__ import annotations

import json
import uuid


async def test_supplied_correlation_id_used_verbatim_and_echoed(env):
    cid = f"orch-{uuid.uuid4()}"
    response = await env.submit(correlation_id=cid)
    assert response.headers["x-correlation-id"] == cid
    assert response.json()["correlation_id"] == cid


async def test_correlation_id_generated_when_absent(env):
    response = await env.submit()
    cid = response.json()["correlation_id"]
    uuid.UUID(cid)
    assert response.headers["x-correlation-id"] == cid


async def test_stored_correlation_id_is_authoritative_on_poll(env):
    cid = f"orch-{uuid.uuid4()}"
    job_id = (await env.submit(correlation_id=cid)).json()["job_id"]
    await env.run_worker()

    # Poll WITHOUT the header: the job's stored ID is echoed regardless.
    polled = await env.poll(job_id)
    assert polled.json()["correlation_id"] == cid
    assert polled.headers["x-correlation-id"] == cid


async def test_correlation_id_stamped_onto_proposals_and_execute(env):
    if not env.case.has_actions:
        return  # nothing more to assert for research-only domains
    cid = f"orch-{uuid.uuid4()}"
    job_id = (await env.submit(correlation_id=cid)).json()["job_id"]
    await env.run_worker()
    body = (await env.poll(job_id)).json()

    action = body["proposed_actions"][0]
    assert action["correlation_id"] == cid

    # Execute WITHOUT the header: joined to the research run via the proposal.
    executed = await env.execute(action["action_id"], action["payload"], idem_key="idem-corr")
    assert executed.status_code == 200
    assert executed.json()["correlation_id"] == cid
    assert executed.headers["x-correlation-id"] == cid


async def test_worker_log_lines_carry_the_correlation_id(env):
    cid = f"orch-{uuid.uuid4()}"
    (await env.submit(correlation_id=cid)).json()
    await env.run_worker()

    worker_events = ("job_started", "job_completed", "agent_step", "proposal_created")
    seen = set()
    for line in env.log_lines:
        record = json.loads(line)
        if record.get("event") in worker_events:
            assert record["correlation_id"] == cid, f"{record['event']} missing correlation"
            seen.add(record["event"])
    assert {"job_started", "job_completed"} <= seen


async def test_log_lines_are_single_line_json_with_canonical_keys(env):
    await env.research_completed()
    canonical = {
        "timestamp", "level", "service", "domain", "env", "correlation_id",
        "request_id", "job_id", "action_id", "consumer_id", "event",
        "duration_ms", "status", "message",
    }
    assert env.log_lines
    for line in env.log_lines:
        assert "\n" not in line
        record = json.loads(line)  # every line parses as one JSON object
        assert canonical <= set(record), f"missing canonical keys: {canonical - set(record)}"
        assert record["domain"] == env.settings.domain
