from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.adapters.base import (
    AuthMode,
    DownstreamCredentials,
    DownstreamError,
    PayloadValidationError,
    UnsupportedActionError,
)
from app.adapters.jira import JiraAdapter
from app.models.schemas import ProposedAction


@pytest.fixture
def issue_state() -> dict:
    return {"status": "To Do", "updated": "2026-07-01T00:00:00.000+0000"}


@pytest.fixture
def adapter(issue_state) -> JiraAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/rest/api/2/issue/PROJ-1" and method == "GET":
            return httpx.Response(200, json={
                "key": "PROJ-1",
                "fields": {"summary": "s", "status": {"name": issue_state["status"]},
                           "updated": issue_state["updated"]},
            })
        if path == "/rest/api/2/issue/MISSING-1" and method == "GET":
            return httpx.Response(404, json={})
        if path == "/rest/api/2/issue/PROJ-1" and method == "PUT":
            return httpx.Response(204)
        if path == "/rest/api/2/issue/PROJ-1/comment" and method == "POST":
            body = json.loads(request.content)
            assert body["body"]
            return httpx.Response(201, json={"id": "42"})
        if path == "/rest/api/2/search":
            return httpx.Response(200, json={"issues": [{
                "key": "PROJ-1",
                "fields": {"summary": "s", "status": {"name": "To Do"},
                           "assignee": None, "updated": issue_state["updated"]},
            }]})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    return JiraAdapter(
        base_url="https://jira.local",
        http_client_factory=lambda creds: httpx.AsyncClient(
            transport=transport, base_url="https://jira.local"
        ),
    )


def _action(action_type: str, payload: dict, preconditions: dict | None = None) -> ProposedAction:
    return ProposedAction(
        action_id="a-1", action_type=action_type, payload=payload,
        preconditions=preconditions or {}, expires_at=datetime.now(timezone.utc),
        correlation_id="c-1",
    )


CREDS = DownstreamCredentials(user_token="pat")


def test_declares_user_pat():
    assert JiraAdapter.auth_mode is AuthMode.USER_PAT


def test_payload_schemas_validate():
    adapter = JiraAdapter(base_url="https://jira.local")
    adapter.validate_payload("update_issue", {"issue_key": "P-1", "fields": {"summary": "x"}})
    adapter.validate_payload("add_comment", {"issue_key": "P-1", "body": "hi"})
    with pytest.raises(PayloadValidationError):
        adapter.validate_payload("update_issue", {"issue_key": "P-1", "fields": {}})
    with pytest.raises(UnsupportedActionError):
        adapter.validate_payload("delete_project", {})


def test_only_comment_body_is_editable():
    adapter = JiraAdapter(base_url="https://jira.local")
    assert adapter.editable_fields("add_comment") == frozenset({"body"})
    assert adapter.editable_fields("update_issue") == frozenset()


async def test_read_tools_are_read_only_and_work(adapter):
    tools = adapter.read_tools(CREDS)
    assert [t.__name__ for t in tools] == ["search_issues", "get_issue", "get_issue_comments"]
    result = json.loads(await tools[0]("project = PROJ"))
    assert result["issues"][0]["key"] == "PROJ-1"


async def test_preconditions_ok_when_state_matches(adapter):
    action = _action("update_issue", {"issue_key": "PROJ-1", "fields": {"summary": "x"}},
                     {"expected_status": "To Do"})
    assert (await adapter.check_preconditions(action, CREDS)).ok


async def test_preconditions_detect_drift(adapter, issue_state):
    issue_state["status"] = "Done"
    action = _action("update_issue", {"issue_key": "PROJ-1", "fields": {"summary": "x"}},
                     {"expected_status": "To Do"})
    result = await adapter.check_preconditions(action, CREDS)
    assert not result.ok
    assert result.details["actual"] == "Done"


async def test_preconditions_fail_on_missing_issue(adapter):
    action = _action("update_issue", {"issue_key": "MISSING-1", "fields": {"summary": "x"}})
    assert not (await adapter.check_preconditions(action, CREDS)).ok


async def test_execute_update_issue(adapter):
    action = _action("update_issue", {"issue_key": "PROJ-1", "fields": {"summary": "x"}})
    outcome = await adapter.execute(action, action.payload, CREDS)
    assert outcome.result["issue_key"] == "PROJ-1"
    assert outcome.resource_url == "https://jira.local/browse/PROJ-1"


async def test_execute_add_comment(adapter):
    action = _action("add_comment", {"issue_key": "PROJ-1", "body": "hello"})
    outcome = await adapter.execute(action, action.payload, CREDS)
    assert outcome.result["comment_id"] == "42"


async def test_execute_surfaces_downstream_errors(adapter):
    action = _action("add_comment", {"issue_key": "UNKNOWN-9", "body": "hello"})
    with pytest.raises(DownstreamError):
        await adapter.execute(action, action.payload, CREDS)
