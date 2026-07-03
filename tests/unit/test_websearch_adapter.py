from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.base import AuthMode, UnsupportedActionError
from app.adapters.websearch import WebSearchAdapter


@pytest.fixture
def adapter() -> WebSearchAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The service-owned key travels in the request, sourced from config.
        assert body["api_key"] == "svc-key-123456"
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "T", "url": "https://x", "content": "snippet", "score": 0.9}
            ]})
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    return WebSearchAdapter(
        api_key="svc-key-123456",
        base_url="https://search.local",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=transport, base_url="https://search.local"
        ),
    )


def test_declares_service_credential(adapter):
    assert adapter.auth_mode is AuthMode.SERVICE_CREDENTIAL
    assert adapter.service_credentials().service_credential == "svc-key-123456"
    assert adapter.service_credentials().user_token is None


def test_research_only_no_action_schemas(adapter):
    assert adapter.action_schemas() == {}
    with pytest.raises(UnsupportedActionError):
        adapter.validate_payload("send_email", {})


async def test_web_search_tool(adapter):
    tools = adapter.read_tools(adapter.service_credentials())
    assert [t.__name__ for t in tools] == ["web_search", "extract_page"]
    result = json.loads(await tools[0]("cve advisory"))
    assert result["results"][0]["url"] == "https://x"
