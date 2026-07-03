"""Contract-suite domain cases.

The contract suite in tests/contract/ is domain-agnostic and MUST pass
unchanged for every domain. What each domain contributes is ONE ContractCase
here: how to build its adapter offline (mock transports), a valid user token
(if USER_PAT), and the canned agent output the fake harness will emit.

scripts/new_domain.py appends a stub case for new domains — a domain agent is
"done" only when the whole suite is green with its case registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.adapters.base import DomainAdapter

JIRA_USER_PAT = "jira-user-pat-supersecret-000111"


@dataclass
class ContractCase:
    name: str
    user_token: str | None
    agent_output: dict[str, Any]
    build: Callable[[], DomainAdapter]
    has_actions: bool
    # Mutable downstream state, so tests can simulate target drift
    # (stale-target). None for research-only domains.
    control: dict[str, Any] | None = None
    settings_overrides: dict[str, Any] = field(default_factory=dict)


# ── Jira (USER_PAT reference) ────────────────────────────────────────────────


def _jira_case() -> ContractCase:
    issue_state: dict[str, Any] = {
        "status": "To Do",
        "updated": "2026-07-01T00:00:00.000+0000",
        "summary": "Login page throws 500",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/2/issue/PROJ-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "key": "PROJ-1",
                    "fields": {
                        "summary": issue_state["summary"],
                        "status": {"name": issue_state["status"]},
                        "updated": issue_state["updated"],
                    },
                },
            )
        if path == "/rest/api/2/issue/PROJ-1" and request.method == "PUT":
            return httpx.Response(204)
        if path == "/rest/api/2/issue/PROJ-1/comment" and request.method == "POST":
            return httpx.Response(201, json={"id": "10001"})
        if path == "/rest/api/2/search":
            return httpx.Response(200, json={"issues": []})
        return httpx.Response(404, json={})

    def build() -> DomainAdapter:
        from app.adapters.jira import JiraAdapter

        transport = httpx.MockTransport(handler)

        def client_factory(credentials: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, base_url="https://jira.local")

        return JiraAdapter(base_url="https://jira.local", http_client_factory=client_factory)

    agent_output = {
        "summary": "PROJ-1 tracks the login 500; root cause is a stale config flag.",
        "sources": [{"title": "PROJ-1", "url_or_id": "https://jira.local/browse/PROJ-1"}],
        "details": {"issues_reviewed": 1},
        "proposed_actions": [
            {
                "action_type": "update_issue",
                "target": {"system": "jira", "id_or_parent": "PROJ-1"},
                "payload": {"issue_key": "PROJ-1", "fields": {"summary": "Login 500 — config"}},
                "preview": "Rename PROJ-1 to 'Login 500 — config'.",
                "preconditions": {"expected_status": "To Do"},
            },
            {
                "action_type": "add_comment",
                "target": {"system": "jira", "id_or_parent": "PROJ-1"},
                "payload": {"issue_key": "PROJ-1", "body": "Root cause: stale config flag."},
                "preview": "Comment the root cause on PROJ-1.",
                "preconditions": {"expected_status": "To Do"},
            },
        ],
    }
    return ContractCase(
        name="jira",
        user_token=JIRA_USER_PAT,
        agent_output=agent_output,
        build=build,
        has_actions=True,
        control=issue_state,
        settings_overrides={"jira_base_url": "https://jira.local"},
    )


# ── Web search (SERVICE_CREDENTIAL reference, research-only) ────────────────


def _websearch_case() -> ContractCase:
    def build() -> DomainAdapter:
        from app.adapters.websearch import WebSearchAdapter

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        transport = httpx.MockTransport(handler)
        return WebSearchAdapter(
            api_key="svc-search-key-supersecret-222333",
            base_url="https://search.local",
            http_client_factory=lambda: httpx.AsyncClient(
                transport=transport, base_url="https://search.local"
            ),
        )

    agent_output = {
        "summary": "Three vendor advisories cover the CVE; patch available since May.",
        "sources": [{"title": "Advisory", "url_or_id": "https://example.com/advisory"}],
        "details": {},
        # A rogue mutation attempt: the contract suite asserts it is dropped
        # structurally (no schema for it -> never surfaced to the consumer).
        "proposed_actions": [
            {
                "action_type": "send_email",
                "target": {"system": "email"},
                "payload": {"to": "someone@example.com"},
                "preview": "should never appear",
                "preconditions": {},
            }
        ],
    }
    return ContractCase(
        name="websearch",
        user_token=None,
        agent_output=agent_output,
        build=build,
        has_actions=False,
        settings_overrides={"websearch_api_key": "svc-search-key-supersecret-222333"},
    )


CASES: list[Callable[[], ContractCase]] = [
    _jira_case,
    _websearch_case,
    # scaffold:contract-case (scripts/new_domain.py appends new cases above)
]
