"""Adapter registry — maps DOMAIN config to a DomainAdapter builder.

CUSTOMIZATION POINT: `scripts/new_domain.py` appends a builder here for each
new domain; nothing else in the service changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from app.adapters.base import AuthMode, DomainAdapter, DownstreamCredentials

if TYPE_CHECKING:
    from app.config import Settings


def _build_jira(settings: "Settings") -> DomainAdapter:
    from app.adapters.jira import JiraAdapter

    return JiraAdapter(base_url=settings.jira_base_url)


def _build_websearch(settings: "Settings") -> DomainAdapter:
    from app.adapters.websearch import WebSearchAdapter

    return WebSearchAdapter(
        api_key=settings.websearch_api_key, base_url=settings.websearch_base_url
    )


ADAPTER_BUILDERS: dict[str, Callable[["Settings"], DomainAdapter]] = {
    "jira": _build_jira,
    "websearch": _build_websearch,
    # scaffold:register (scripts/new_domain.py inserts new builders above)
}


def build_adapter(settings: "Settings") -> DomainAdapter:
    try:
        return ADAPTER_BUILDERS[settings.domain](settings)
    except KeyError:
        raise ValueError(
            f"Unknown DOMAIN '{settings.domain}'. Registered: {sorted(ADAPTER_BUILDERS)}"
        ) from None


def resolve_credentials(
    adapter: DomainAdapter, user_token: str | None = None
) -> DownstreamCredentials:
    """Resolve layer-2 credentials per auth_mode. The ONLY place this branch
    exists for credential material."""
    if adapter.auth_mode is AuthMode.USER_PAT:
        return DownstreamCredentials(user_token=user_token)
    if adapter.auth_mode is AuthMode.SERVICE_CREDENTIAL:
        return adapter.service_credentials()
    return DownstreamCredentials()
