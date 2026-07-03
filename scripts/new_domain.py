#!/usr/bin/env python3
"""Scaffold a new domain adapter: `make new-domain NAME=confluence`.

Generates:
  app/adapters/{name}.py            adapter skeleton (choose an auth_mode!)
  tests/unit/test_{name}_adapter.py unit-test stub
and registers the domain in:
  app/adapters/__init__.py          (builder entry)
  tests/contract/cases.py           (contract-case stub)

Then prints the remaining manual steps (config fields, DoD checklist).
Every domain starts identical — see CONTRIBUTING.md for the Definition of
Done before shipping.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ADAPTER_TEMPLATE = '''"""{title} DomainAdapter.

CUSTOMIZATION POINT: domain-owned file, scaffolded from TEMPLATE_VERSION.
Reference implementations: app/adapters/jira.py (USER_PAT),
app/adapters/websearch.py (SERVICE_CREDENTIAL, research-only).
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Mapping

import httpx
from pydantic import BaseModel, Field

from app.adapters.base import (
    AuthMode,
    DomainAdapter,
    DownstreamCredentials,
    ExecutionResult,
    PreconditionResult,
    read_request_with_backoff,
)
from app.models.schemas import ProposedAction
from app.observability.correlation import downstream_headers


# TODO: one Pydantic schema per mutation this domain supports.
class ExamplePayload(BaseModel):
    target_id: str = Field(min_length=1)


class {class_name}(DomainAdapter):
    name: ClassVar[str] = "{name}"
    # TODO: declare the real auth mode — USER_PAT (acts as the end user),
    # SERVICE_CREDENTIAL (service-owned key), or NONE (open sources).
    auth_mode: ClassVar[AuthMode] = AuthMode.NONE
    log_content_allowlist: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        base_url: str,
        http_client_factory: Callable[[DownstreamCredentials], httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_client_factory = http_client_factory or self._default_client

    def _default_client(self, credentials: DownstreamCredentials) -> httpx.AsyncClient:
        headers = dict(downstream_headers())
        if self.auth_mode is AuthMode.USER_PAT:
            headers["Authorization"] = f"Bearer {{credentials.user_token}}"
        return httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=15.0)

    def read_tools(self, credentials: DownstreamCredentials) -> list[Callable[..., Any]]:
        factory = self._http_client_factory

        async def search(query: str, max_results: int = 20) -> str:
            """TODO: describe this read-only tool for the agent."""
            # DOMAIN-SPECIFIC: implement the read call
            async with factory(credentials) as client:
                response = await read_request_with_backoff(
                    lambda: client.get("/search", params={{"q": query}})
                )
                response.raise_for_status()
                return response.text

        return [search]

    def research_instructions(self) -> str:
        return "TODO: domain-specific research guidance."

    def action_schemas(self) -> Mapping[str, type[BaseModel]]:
        # Empty mapping = research-only domain (no /execute path).
        return {{"example_action": ExamplePayload}}

    async def check_preconditions(
        self, action: ProposedAction, credentials: DownstreamCredentials
    ) -> PreconditionResult:
        # DOMAIN-SPECIFIC: fetch live target state, compare to
        # action.preconditions; return ok=False with details on drift.
        return PreconditionResult(ok=True)

    async def execute(
        self,
        action: ProposedAction,
        approved_payload: dict[str, Any],
        credentials: DownstreamCredentials,
    ) -> ExecutionResult:
        validated = self.validate_payload(action.action_type, approved_payload)
        # DOMAIN-SPECIFIC: perform the single approved mutation.
        raise NotImplementedError("implement the mutation call")
'''

UNIT_TEST_TEMPLATE = '''from __future__ import annotations

import pytest

from app.adapters.{name} import {class_name}


@pytest.fixture
def adapter() -> {class_name}:
    # TODO: wire an httpx.MockTransport like tests/unit/test_jira_adapter.py
    return {class_name}(base_url="https://{name}.local")


def test_auth_mode_is_declared(adapter):
    assert adapter.name == "{name}"
    assert adapter.auth_mode is not None


# TODO: schema validation, editable fields, precondition drift, executors.
'''

BUILDER_SNIPPET = '''def _build_{name}(settings: "Settings") -> DomainAdapter:
    from app.adapters.{name} import {class_name}

    return {class_name}(base_url=settings.{name}_base_url)


'''

CASE_SNIPPET = '''def _{name}_case() -> ContractCase:
    # TODO: build the adapter fully offline (httpx.MockTransport), give it a
    # canned agent output, and (for USER_PAT) a fake user token. See
    # _jira_case for the pattern. The contract suite must pass UNCHANGED.
    def build() -> DomainAdapter:
        from app.adapters.{name} import {class_name}

        return {class_name}(base_url="https://{name}.local")

    return ContractCase(
        name="{name}",
        user_token=None,  # set for USER_PAT domains
        agent_output={{"summary": "TODO", "sources": [], "details": {{}},
                      "proposed_actions": []}},
        build=build,
        has_actions=False,
    )


'''


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/new_domain.py <name>   (e.g. confluence)")
        return 2
    name = sys.argv[1].lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]+", name):
        print(f"invalid domain name: {name!r} (want lowercase identifier)")
        return 2
    class_name = f"{name.title().replace('_', '')}Adapter"

    adapter_path = REPO / "app" / "adapters" / f"{name}.py"
    test_path = REPO / "tests" / "unit" / f"test_{name}_adapter.py"
    if adapter_path.exists():
        print(f"refusing to overwrite existing {adapter_path}")
        return 1

    adapter_path.write_text(ADAPTER_TEMPLATE.format(name=name, class_name=class_name, title=name.title()))
    test_path.write_text(UNIT_TEST_TEMPLATE.format(name=name, class_name=class_name))

    registry = REPO / "app" / "adapters" / "__init__.py"
    marker = "ADAPTER_BUILDERS"
    text = registry.read_text()
    text = text.replace(
        f"{marker}:", f"{marker}:", 1
    ).replace(
        "ADAPTER_BUILDERS: dict",
        BUILDER_SNIPPET.format(name=name, class_name=class_name) + "ADAPTER_BUILDERS: dict",
        1,
    ).replace(
        "    # scaffold:register",
        f'    "{name}": _build_{name},\n    # scaffold:register',
        1,
    )
    registry.write_text(text)

    cases = REPO / "tests" / "contract" / "cases.py"
    text = cases.read_text()
    text = text.replace(
        "CASES: list", CASE_SNIPPET.format(name=name, class_name=class_name) + "CASES: list", 1
    ).replace(
        "    # scaffold:contract-case",
        f"    _{name}_case,\n    # scaffold:contract-case",
        1,
    )
    cases.write_text(text)

    print(f"""scaffolded domain '{name}':
  created   {adapter_path.relative_to(REPO)}
  created   {test_path.relative_to(REPO)}
  updated   app/adapters/__init__.py   (builder registered)
  updated   tests/contract/cases.py    (contract case stub)

next steps:
  1. Add config to app/config.py DOMAIN-SPECIFIC block + .env.example:
         {name}_base_url: str = ""
  2. Pick the auth_mode in {adapter_path.name} and implement tools/executors.
  3. Flesh out the contract case (offline transports, canned agent output).
  4. Run `make test` — the contract suite must pass UNCHANGED.
  5. Work through the Definition of Done in CONTRIBUTING.md.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
