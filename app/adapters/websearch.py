"""Web search DomainAdapter — the reference SERVICE_CREDENTIAL implementation.

CUSTOMIZATION POINT: domain-owned file.

Demonstrates the no-PAT path end to end:
* the search API key is SERVICE-OWNED — loaded from config at construction,
  never accepted from the caller, never staged in Redis;
* X-User-Token is ignored if sent;
* research-only: no action schemas, so no /execute path exists for this
  domain (proposals are never produced, and execute finds nothing to run).
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar, Mapping

import httpx
from pydantic import BaseModel

from app.adapters.base import (
    AuthMode,
    DomainAdapter,
    DownstreamCredentials,
    UnsupportedActionError,
    read_request_with_backoff,
)
from app.models.schemas import ProposedAction
from app.observability.correlation import downstream_headers


class WebSearchAdapter(DomainAdapter):
    name: ClassVar[str] = "websearch"
    auth_mode: ClassVar[AuthMode] = AuthMode.SERVICE_CREDENTIAL

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_client_factory = http_client_factory or self._default_client

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=downstream_headers(),
            timeout=httpx.Timeout(20.0),
        )

    def service_credentials(self) -> DownstreamCredentials:
        return DownstreamCredentials(service_credential=self._api_key)

    # ── research tools ───────────────────────────────────────────────────────

    def read_tools(self, credentials: DownstreamCredentials) -> list[Callable[..., Any]]:
        factory = self._http_client_factory
        api_key = credentials.service_credential

        async def web_search(query: str, max_results: int = 5) -> str:
            """Search the web. Returns JSON results with title, url, and a
            content snippet for each hit."""
            # DOMAIN-SPECIFIC: Tavily search API
            async with factory() as client:
                response = await read_request_with_backoff(
                    lambda: client.post(
                        "/search",
                        json={
                            "api_key": api_key,
                            "query": query,
                            "max_results": min(max_results, 10),
                        },
                    )
                )
                response.raise_for_status()
                data = response.json()
                return json.dumps(
                    {
                        "results": [
                            {
                                "title": r.get("title"),
                                "url": r.get("url"),
                                "content": r.get("content"),
                            }
                            for r in data.get("results", [])
                        ]
                    }
                )

        async def extract_page(url: str) -> str:
            """Fetch and extract the readable content of a single web page."""
            # DOMAIN-SPECIFIC: Tavily extract API
            async with factory() as client:
                response = await read_request_with_backoff(
                    lambda: client.post("/extract", json={"api_key": api_key, "urls": [url]})
                )
                response.raise_for_status()
                return json.dumps(response.json())

        return [web_search, extract_page]

    def research_instructions(self) -> str:
        return (
            "You are a web research agent. Use web_search to find sources and "
            "extract_page for detail. Always cite URLs in findings.sources. "
            "This domain is read-only: never propose actions."
        )

    # ── mutation contract: research-only domain ──────────────────────────────

    def action_schemas(self) -> Mapping[str, type[BaseModel]]:
        return {}

    async def check_preconditions(
        self, action: ProposedAction, credentials: DownstreamCredentials
    ):
        raise UnsupportedActionError(action.action_type)

    async def execute(
        self,
        action: ProposedAction,
        approved_payload: dict[str, Any],
        credentials: DownstreamCredentials,
    ):
        raise UnsupportedActionError(action.action_type)
