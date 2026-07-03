"""Jira DomainAdapter — the reference USER_PAT implementation.

CUSTOMIZATION POINT: this file (like everything in app/adapters/) is
domain-owned. It is the working example new USER_PAT domains (Confluence,
Email) copy from.

The service acts AS THE END USER: every downstream call carries the caller's
PAT as a bearer token. Research binds three read-only tools; mutations are
two deterministic executors (update_issue, add_comment) with live
precondition checks.
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar, Mapping

import httpx
from pydantic import BaseModel, Field

from app.adapters.base import (
    AuthMode,
    DomainAdapter,
    DownstreamCredentials,
    DownstreamError,
    ExecutionResult,
    PreconditionResult,
    read_request_with_backoff,
)
from app.models.schemas import ProposedAction
from app.observability.correlation import downstream_headers

_API = "/rest/api/2"


# ── Action payload schemas (the contract for /execute) ──────────────────────


class UpdateIssuePayload(BaseModel):
    issue_key: str = Field(min_length=1)
    fields: dict[str, Any] = Field(min_length=1, description="Jira field id -> new value")


class AddCommentPayload(BaseModel):
    issue_key: str = Field(min_length=1)
    body: str = Field(min_length=1)


class JiraAdapter(DomainAdapter):
    name: ClassVar[str] = "jira"
    auth_mode: ClassVar[AuthMode] = AuthMode.USER_PAT
    # Reviewed allowlist: issue keys/summaries are identifiers, not user content.
    log_content_allowlist: ClassVar[frozenset[str]] = frozenset({"issue_key"})

    def __init__(
        self,
        base_url: str,
        http_client_factory: Callable[[DownstreamCredentials], httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Injectable for tests (httpx.MockTransport); production uses the default.
        self._http_client_factory = http_client_factory or self._default_client

    def _default_client(self, credentials: DownstreamCredentials) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {credentials.user_token}",
                "Accept": "application/json",
                **downstream_headers(),  # X-Correlation-Id onto every Jira call
            },
            timeout=httpx.Timeout(15.0),
        )

    # ── research tools (read-only — no mutation callables exist here) ───────

    def read_tools(self, credentials: DownstreamCredentials) -> list[Callable[..., Any]]:
        factory = self._http_client_factory

        async def search_issues(jql: str, max_results: int = 20) -> str:
            """Search Jira issues with a JQL query. Returns matching issues as
            JSON (key, summary, status, assignee, updated)."""
            # DOMAIN-SPECIFIC: Jira search API
            async with factory(credentials) as client:
                response = await read_request_with_backoff(
                    lambda: client.get(
                        f"{_API}/search",
                        params={
                            "jql": jql,
                            "maxResults": min(max_results, 50),
                            "fields": "summary,status,assignee,updated,issuetype",
                        },
                    )
                )
                response.raise_for_status()
                issues = [
                    {
                        "key": issue["key"],
                        "summary": issue["fields"].get("summary"),
                        "status": (issue["fields"].get("status") or {}).get("name"),
                        "assignee": ((issue["fields"].get("assignee") or {}).get("displayName")),
                        "updated": issue["fields"].get("updated"),
                    }
                    for issue in response.json().get("issues", [])
                ]
                return json.dumps({"total": len(issues), "issues": issues})

        async def get_issue(issue_key: str) -> str:
            """Fetch one Jira issue by key (e.g. PROJ-123): full fields
            including description, status, and version metadata."""
            # DOMAIN-SPECIFIC: Jira issue API
            async with factory(credentials) as client:
                response = await read_request_with_backoff(
                    lambda: client.get(f"{_API}/issue/{issue_key}")
                )
                response.raise_for_status()
                return json.dumps(response.json())

        async def get_issue_comments(issue_key: str, max_results: int = 20) -> str:
            """List recent comments on a Jira issue as JSON."""
            # DOMAIN-SPECIFIC: Jira comments API
            async with factory(credentials) as client:
                response = await read_request_with_backoff(
                    lambda: client.get(
                        f"{_API}/issue/{issue_key}/comment",
                        params={"maxResults": min(max_results, 50)},
                    )
                )
                response.raise_for_status()
                return json.dumps(response.json())

        return [search_issues, get_issue, get_issue_comments]

    def research_instructions(self) -> str:
        return (
            "You are researching Jira. Use JQL via search_issues to locate "
            "issues, then get_issue/get_issue_comments for detail. When you "
            "propose update_issue or add_comment actions, include the issue's "
            "current status as preconditions.expected_status so execution can "
            "detect drift."
        )

    # ── mutation contract ────────────────────────────────────────────────────

    def action_schemas(self) -> Mapping[str, type[BaseModel]]:
        return {"update_issue": UpdateIssuePayload, "add_comment": AddCommentPayload}

    def editable_fields(self, action_type: str) -> frozenset[str]:
        # Humans may reword a proposed comment in the approval UI; issue field
        # updates must execute exactly as proposed.
        return frozenset({"body"}) if action_type == "add_comment" else frozenset()

    async def check_preconditions(
        self, action: ProposedAction, credentials: DownstreamCredentials
    ) -> PreconditionResult:
        issue_key = action.payload.get("issue_key")
        if not issue_key:
            return PreconditionResult(ok=False, details={"reason": "payload missing issue_key"})

        # DOMAIN-SPECIFIC: fetch live issue state
        async with self._http_client_factory(credentials) as client:
            response = await client.get(f"{_API}/issue/{issue_key}")
        if response.status_code == 404:
            return PreconditionResult(ok=False, details={"reason": f"issue {issue_key} not found"})
        if response.status_code >= 400:
            raise DownstreamError(
                f"Jira precondition check failed ({response.status_code})",
                status_code=response.status_code,
            )

        fields = response.json().get("fields", {})
        actual = {
            "expected_status": (fields.get("status") or {}).get("name"),
            "expected_updated": fields.get("updated"),
        }
        for name, expected in action.preconditions.items():
            if name in actual and actual[name] != expected:
                return PreconditionResult(
                    ok=False,
                    details={"precondition": name, "expected": expected, "actual": actual[name]},
                )
        return PreconditionResult(ok=True)

    async def execute(
        self,
        action: ProposedAction,
        approved_payload: dict[str, Any],
        credentials: DownstreamCredentials,
    ) -> ExecutionResult:
        validated = self.validate_payload(action.action_type, approved_payload)

        async with self._http_client_factory(credentials) as client:
            if action.action_type == "update_issue":
                payload = validated  # UpdateIssuePayload
                # DOMAIN-SPECIFIC: Jira edit-issue API
                response = await client.put(
                    f"{_API}/issue/{payload.issue_key}", json={"fields": payload.fields}
                )
                if response.status_code >= 400:
                    raise DownstreamError(
                        f"Jira update_issue failed ({response.status_code})",
                        status_code=response.status_code,
                    )
                return ExecutionResult(
                    result={
                        "issue_key": payload.issue_key,
                        "updated_fields": sorted(payload.fields),
                    },
                    resource_url=f"{self._base_url}/browse/{payload.issue_key}",
                )

            # add_comment (validate_payload already rejected unknown types)
            payload = validated  # AddCommentPayload
            # DOMAIN-SPECIFIC: Jira add-comment API
            response = await client.post(
                f"{_API}/issue/{payload.issue_key}/comment", json={"body": payload.body}
            )
            if response.status_code >= 400:
                raise DownstreamError(
                    f"Jira add_comment failed ({response.status_code})",
                    status_code=response.status_code,
                )
            comment_id = response.json().get("id")
            return ExecutionResult(
                result={"issue_key": payload.issue_key, "comment_id": comment_id},
                resource_url=f"{self._base_url}/browse/{payload.issue_key}"
                f"?focusedCommentId={comment_id}",
            )
