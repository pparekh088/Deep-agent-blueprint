"""TEMPLATE_CORE — API contract models, job records, and error codes.

These models ARE the service contract. They never leak harness concepts
(no Deep Agents / LangGraph types) and never change per domain — domain
variability lives entirely in the free-form ``target`` / ``payload`` dicts,
validated against the adapter's action schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ────────────────────────────────────────────────────────────────────


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class ErrorCode(str, Enum):
    INVALID_API_KEY = "INVALID_API_KEY"
    MISSING_USER_TOKEN = "MISSING_USER_TOKEN"
    ACTION_EXPIRED = "ACTION_EXPIRED"
    PAYLOAD_MISMATCH = "PAYLOAD_MISMATCH"
    STALE_TARGET = "STALE_TARGET"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DOWNSTREAM_ERROR = "DOWNSTREAM_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ── /research ────────────────────────────────────────────────────────────────


class ResearchRequest(BaseModel):
    task: str = Field(min_length=1, description="Natural-language research task.")
    session_id: str = Field(min_length=1)
    context: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None


class ResearchAccepted(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    poll_url: str
    estimated_wait_s: int
    correlation_id: str


class JobProgress(BaseModel):
    last_tool: str | None = None
    steps: int = 0


class JobError(BaseModel):
    code: ErrorCode
    message: str


class Findings(BaseModel):
    summary: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ProposedAction(BaseModel):
    action_id: str
    action_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    preview: str = ""
    preconditions: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime
    # Top-level payload fields the consumer may edit before approval; anything
    # else must be byte-for-byte what research proposed (409 PAYLOAD_MISMATCH).
    editable_fields: list[str] = Field(default_factory=list)
    correlation_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    session_id: str
    correlation_id: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt: int = 0
    cancel_requested: bool = False
    progress: JobProgress | None = None
    findings: Findings | None = None
    proposed_actions: list[ProposedAction] | None = None
    error: JobError | None = None


# ── /execute ─────────────────────────────────────────────────────────────────


class Approval(BaseModel):
    approved_by: str = Field(min_length=1)
    approved_at: datetime


class ExecuteRequest(BaseModel):
    action_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    approved_payload: dict[str, Any]
    approval: Approval


class ExecuteResponse(BaseModel):
    status: str = "executed"
    action_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    resource_url: str | None = None
    idempotent_replay: bool = False
    correlation_id: str


class ActionStatusResponse(BaseModel):
    action_id: str
    status: str  # "proposed" | "rejected"
    expires_at: datetime | None = None
    action: ProposedAction | None = None
    correlation_id: str


# ── Error envelope (every non-2xx response) ─────────────────────────────────


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    correlation_id: str


# ── Internal records (Redis) ────────────────────────────────────────────────


class JobRecord(BaseModel):
    """Redis job record at ``{domain}:job:{job_id}``."""

    job_id: str
    domain: str
    status: JobStatus = JobStatus.QUEUED
    task: str
    session_id: str
    context: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    consumer_id: str
    # Salted hash of the submitting user's PAT (USER_PAT domains) — used to
    # reject cross-user polling. Never the token itself.
    principal_hash: str | None = None
    correlation_id: str
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt: int = 0
    max_attempts: int = 2
    progress: JobProgress = Field(default_factory=JobProgress)
    error: JobError | None = None


class JobResultDoc(BaseModel):
    """Redis result document at ``{domain}:result:{job_id}``."""

    findings: Findings
    proposed_actions: list[ProposedAction] = Field(default_factory=list)


class StoredProposal(BaseModel):
    """Redis proposal record at ``{domain}:proposal:{action_id}``."""

    action: ProposedAction
    job_id: str
    session_id: str
    consumer_id: str
    principal_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class IdempotencyRecord(BaseModel):
    """Redis idempotency record at ``{domain}:idem:{key}``."""

    request_hash: str
    response: dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)


# ── Agent output (worker-internal parse target — never leaves the worker) ───


class AgentProposedAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    preview: str = ""
    preconditions: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    proposed_actions: list[AgentProposedAction] = Field(default_factory=list)
