"""TEMPLATE_CORE — the DomainAdapter contract.

ALL domain-specific behavior lives behind this ABC: read tools for research,
payload schemas + executors for mutations, precondition checks, and the
declared ``auth_mode``. The service core (API, worker, store) never contains
domain logic — Jira, Confluence, Email are drop-in implementations.

Design invariants the ABC encodes:

* ``read_tools`` returns ONLY read operations. Read-only research is
  structural — mutation callables are simply never handed to the agent.
  There is deliberately no way to pass an executor into the agent harness.
* ``execute`` performs exactly one deterministic mutation. No LLM anywhere.
* Everything branches on ``auth_mode`` — never assume a PAT exists.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, ClassVar, Mapping

import httpx
from pydantic import BaseModel, ValidationError

from app.models.schemas import ProposedAction

logger = logging.getLogger(__name__)


class AuthMode(str, Enum):
    USER_PAT = "USER_PAT"                    # act as the end user (Jira, Confluence, Email)
    SERVICE_CREDENTIAL = "SERVICE_CREDENTIAL"  # service-owned key (web search)
    NONE = "NONE"                            # open/public sources


@dataclass(frozen=True)
class DownstreamCredentials:
    """Resolved layer-2 credentials for one research run or execute call.
    Exactly one field is populated, per the adapter's auth_mode."""

    user_token: str | None = None
    service_credential: str | None = None


@dataclass
class PreconditionResult:
    ok: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    result: dict[str, Any]
    resource_url: str | None = None


class UnsupportedActionError(Exception):
    def __init__(self, action_type: str) -> None:
        super().__init__(f"unsupported action_type: {action_type}")
        self.action_type = action_type


class PayloadValidationError(Exception):
    def __init__(self, action_type: str, errors: Any) -> None:
        super().__init__(f"invalid payload for {action_type}")
        self.action_type = action_type
        self.errors = errors


class DownstreamError(Exception):
    """Target-system failure during precondition check or execution."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DomainAdapter(ABC):
    """One implementation per domain, registered in app/adapters/__init__.py."""

    name: ClassVar[str]
    auth_mode: ClassVar[AuthMode]

    # Logs never contain payload content unless a field is explicitly
    # allowlisted here (reviewed at Definition-of-Done time).
    log_content_allowlist: ClassVar[frozenset[str]] = frozenset()

    # ── research (read-only, agentic) ───────────────────────────────────────

    @abstractmethod
    def read_tools(self, credentials: DownstreamCredentials) -> list[Callable[..., Any]]:
        """Read-only tool callables bound into the research agent. Async
        functions with docstrings + type hints; the harness converts them.
        MUST NOT include anything that mutates the target system."""

    def research_instructions(self) -> str:
        """Domain-specific addendum to the core research prompt."""
        return ""

    # ── mutations (deterministic, synchronous) ──────────────────────────────

    @abstractmethod
    def action_schemas(self) -> Mapping[str, type[BaseModel]]:
        """action_type -> Pydantic payload schema. Empty mapping = research-only
        domain (no /execute path)."""

    def editable_fields(self, action_type: str) -> frozenset[str]:
        """Top-level payload fields the consumer may edit before approval.
        Default: nothing is editable — approved == proposed, byte for byte."""
        return frozenset()

    @abstractmethod
    async def check_preconditions(
        self, action: ProposedAction, credentials: DownstreamCredentials
    ) -> PreconditionResult:
        """Re-check live target state against ``action.preconditions`` right
        before mutating. Drift -> the caller returns 409 STALE_TARGET."""

    @abstractmethod
    async def execute(
        self,
        action: ProposedAction,
        approved_payload: dict[str, Any],
        credentials: DownstreamCredentials,
    ) -> ExecutionResult:
        """Perform the single approved mutation. Deterministic — no LLM, no
        retries (idempotency keys on /execute are the only replay mechanism)."""

    # ── credentials ─────────────────────────────────────────────────────────

    def service_credentials(self) -> DownstreamCredentials:
        """SERVICE_CREDENTIAL adapters override this to hand back their
        service-owned credential (loaded from config at construction)."""
        raise NotImplementedError(f"{self.name}: service_credentials not defined")

    # ── shared helpers (concrete) ───────────────────────────────────────────

    def validate_payload(self, action_type: str, payload: dict[str, Any]) -> BaseModel:
        schemas = self.action_schemas()
        schema = schemas.get(action_type)
        if schema is None:
            raise UnsupportedActionError(action_type)
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise PayloadValidationError(action_type, exc.errors()) from exc


async def read_request_with_backoff(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int = 3,
    base_delay_s: float = 0.5,
) -> httpx.Response:
    """Exponential backoff for READ calls inside agent tools (429/5xx).
    Never use this for mutations — mutations are never auto-retried."""
    last: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        response = await send()
        if response.status_code not in (429, 502, 503, 504):
            return response
        last = response
        if attempt < max_retries:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else base_delay_s * (2**attempt)
            logger.info(
                "backing off read call",
                extra={"event": "read_backoff", "status": response.status_code,
                       "duration_ms": round(delay * 1000)},
            )
            await asyncio.sleep(delay)
    assert last is not None
    return last
