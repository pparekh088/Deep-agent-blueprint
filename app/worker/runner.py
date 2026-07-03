"""TEMPLATE_CORE — the agent run loop.

One job = one full research run: unwrap the staged token (USER_PAT only),
build the read-only agent, stream it with cancellation checks between steps,
parse the final message into findings + proposed actions, persist, purge.

Deliberately arq-free: the queue framework adapts to this module
(app/worker/main.py), never the other way around — that keeps the loop unit-
testable and the queue swappable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from pydantic import ValidationError

from app.adapters import resolve_credentials
from app.adapters.base import AuthMode, DomainAdapter, PayloadValidationError, UnsupportedActionError
from app.agent.factory import AgentFactory
from app.agent.prompts import render_research_prompt
from app.config import Settings
from app.models.schemas import (
    AgentOutput,
    ErrorCode,
    Findings,
    JobError,
    JobRecord,
    JobResultDoc,
    JobStatus,
    ProposedAction,
    StoredProposal,
    utcnow,
)
from app.observability.correlation import bound_context
from app.observability.logging import log_event, register_secret
from app.state.redis_store import RedisStore
from app.state.token_vault import BaseTokenVault

logger = logging.getLogger(__name__)


@dataclass
class WorkerDeps:
    settings: Settings
    store: RedisStore
    vault: BaseTokenVault
    adapter: DomainAdapter
    agent_factory: AgentFactory
    build_llm: Callable[[Settings], Any]


class JobCancelled(Exception):
    pass


class RetryableJobError(Exception):
    """Raised when an attempt failed but attempts remain. The queue layer
    (app/worker/main.py) translates this into a redelivery; research is
    read-only, so a from-scratch re-run is always safe."""


async def run_research_job(deps: WorkerDeps, job_id: str, attempt: int) -> None:
    record = await deps.store.get_job(job_id)
    if record is None:
        log_event(logger, "job_record_missing", level=logging.WARNING, job_id=job_id)
        return

    with bound_context(
        correlation_id=record.correlation_id, job_id=job_id, consumer_id=record.consumer_id
    ):
        if record.status in (JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED):
            return  # raced with cancel or a duplicate delivery — nothing to do
        if await deps.store.is_cancel_requested(job_id):
            await deps.store.transition(job_id, JobStatus.CANCELLED)
            return

        record = await deps.store.transition(job_id, JobStatus.RUNNING, attempt=attempt)
        started = time.perf_counter()
        log_event(logger, "job_started", status="running")

        try:
            credentials = await _resolve_job_credentials(deps, record)
            agent = deps.agent_factory(
                model=deps.build_llm(deps.settings),
                tools=deps.adapter.read_tools(credentials),
                instructions=render_research_prompt(deps.adapter),
            )
            async with asyncio.timeout(deps.settings.job_timeout_s):
                final_text = await _stream_agent(deps, job_id, agent, record)

            output = _parse_agent_output(final_text)
            proposals = await _persist_proposals(deps, record, output)
            await deps.store.save_result(
                job_id,
                JobResultDoc(
                    findings=Findings(
                        summary=output.summary, sources=output.sources, details=output.details
                    ),
                    proposed_actions=proposals,
                ),
            )
            await deps.store.transition(job_id, JobStatus.COMPLETED)
            log_event(
                logger,
                "job_completed",
                status="completed",
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

        except (TimeoutError, asyncio.TimeoutError):
            await deps.store.transition(
                job_id,
                JobStatus.FAILED,
                error=JobError(
                    code=ErrorCode.JOB_TIMEOUT,
                    message=f"research exceeded {deps.settings.job_timeout_s}s",
                ),
            )
            log_event(logger, "job_failed", level=logging.ERROR, status="failed",
                      message="job timeout")
        except JobCancelled:
            await deps.store.transition(job_id, JobStatus.CANCELLED)
            log_event(logger, "job_cancelled", status="cancelled")
        except Exception as exc:  # noqa: BLE001 — single failure funnel
            if attempt < record.max_attempts:
                # Token ciphertext is intentionally NOT purged here — the
                # retry attempt still needs it. Terminal transitions purge.
                log_event(
                    logger, "job_attempt_failed", level=logging.WARNING,
                    message=f"attempt {attempt}/{record.max_attempts}: {type(exc).__name__}",
                )
                raise RetryableJobError(str(exc)) from exc
            await deps.store.transition(
                job_id,
                JobStatus.FAILED,
                error=JobError(
                    code=ErrorCode.INTERNAL_ERROR,
                    # type + class only: exception text may embed downstream
                    # response bodies; the log formatter additionally redacts.
                    message=f"research failed after {attempt} attempts ({type(exc).__name__})",
                ),
            )
            logger.error("job failed", exc_info=exc, extra={"event": "job_failed", "status": "failed"})


async def _resolve_job_credentials(deps: WorkerDeps, record: JobRecord):
    if deps.adapter.auth_mode is not AuthMode.USER_PAT:
        return resolve_credentials(deps.adapter)

    ciphertext = await deps.store.load_token(record.job_id)
    if ciphertext is None:
        raise RuntimeError("staged token missing (expired or purged) — cannot act as user")
    token = await deps.vault.decrypt(ciphertext)
    register_secret(token)  # plaintext exists in worker memory only; never logs
    return resolve_credentials(deps.adapter, user_token=token)


async def _stream_agent(deps: WorkerDeps, job_id: str, agent: Any, record: JobRecord) -> str:
    """Drive the agent step by step; between steps, honor cancellation and
    surface lightweight progress (last tool name + step count — never payloads)."""
    agent_input = {"messages": [{"role": "user", "content": _render_task(record)}]}
    steps = 0
    final_state: dict[str, Any] | None = None

    async for state in agent.astream(agent_input, stream_mode="values"):
        if await deps.store.is_cancel_requested(job_id):
            raise JobCancelled()
        steps += 1
        final_state = state
        tool_name = _last_tool_name(state)
        await deps.store.set_progress(job_id, last_tool=tool_name, steps=steps)
        if tool_name:
            log_event(logger, "agent_step", message=f"tool={tool_name} step={steps}")

    if not final_state or not final_state.get("messages"):
        raise RuntimeError("agent produced no output")
    return _message_text(final_state["messages"][-1])


def _render_task(record: JobRecord) -> str:
    parts = [f"Task: {record.task}"]
    if record.context:
        parts.append(f"Context: {json.dumps(record.context, default=str)}")
    if record.constraints:
        parts.append(f"Constraints: {json.dumps(record.constraints, default=str)}")
    return "\n\n".join(parts)


def _msg_attr(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _last_tool_name(state: dict[str, Any]) -> str | None:
    messages = state.get("messages") or []
    if not messages:
        return None
    tool_calls = _msg_attr(messages[-1], "tool_calls") or []
    if tool_calls:
        call = tool_calls[-1]
        return call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
    return None


def _message_text(message: Any) -> str:
    content = _msg_attr(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # langchain v1 content blocks
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_agent_output(text: str) -> AgentOutput:
    """Tolerant parse of the OUTPUT CONTRACT. A malformed block degrades to
    findings-only (summary = raw text) rather than failing the job — a run
    that found things but formatted badly is still useful."""
    candidates = [match.group(1) for match in _FENCED_JSON.finditer(text)]
    brace = text.find("{")
    if brace != -1:
        candidates.append(text[brace : text.rfind("}") + 1])
    for candidate in reversed(candidates):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            try:
                return AgentOutput.model_validate(data)
            except ValidationError:
                continue
    log_event(logger, "agent_output_unstructured", level=logging.WARNING)
    return AgentOutput(summary=text.strip())


async def _persist_proposals(
    deps: WorkerDeps, record: JobRecord, output: AgentOutput
) -> list[ProposedAction]:
    """Validate agent-proposed actions against the adapter's schemas and stage
    the survivors for approval. Invalid proposals are dropped (logged), never
    surfaced — the consumer must only ever see executable proposals."""
    proposals: list[ProposedAction] = []
    expires_at = utcnow() + timedelta(seconds=deps.settings.proposal_ttl_s)

    for candidate in output.proposed_actions:
        try:
            deps.adapter.validate_payload(candidate.action_type, candidate.payload)
        except (UnsupportedActionError, PayloadValidationError) as exc:
            log_event(
                logger, "proposal_rejected", level=logging.WARNING,
                message=f"{candidate.action_type}: {type(exc).__name__}",
            )
            continue

        action = ProposedAction(
            action_id=str(uuid.uuid4()),
            action_type=candidate.action_type,
            target=candidate.target,
            payload=candidate.payload,
            preview=candidate.preview,
            preconditions=candidate.preconditions,
            expires_at=expires_at,
            editable_fields=sorted(deps.adapter.editable_fields(candidate.action_type)),
            correlation_id=record.correlation_id,
        )
        await deps.store.save_proposal(
            StoredProposal(
                action=action,
                job_id=record.job_id,
                session_id=record.session_id,
                consumer_id=record.consumer_id,
                principal_hash=record.principal_hash,
            )
        )
        log_event(logger, "proposal_created", action_id=action.action_id,
                  message=f"action_type={action.action_type}")
        proposals.append(action)

    return proposals
