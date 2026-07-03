"""TEMPLATE_CORE — POST /execute (synchronous, deterministic) and the
supporting /actions endpoints.

/execute contains NO LLM call. What the human approved is byte-for-byte what
executes:

  0. Idempotency replay check (must run before the proposal lookup so a
     replay still succeeds after the proposal was consumed).
  1. Load the proposal — missing/expired/consumed -> 409 ACTION_EXPIRED.
  2. Verify approved_payload against the stored proposal; only fields the
     proposal marked editable may differ -> 409 PAYLOAD_MISMATCH.
  3. Validate the payload against the adapter's action schema.
  4. Re-check live target preconditions -> 409 STALE_TARGET on drift.
  5. Execute via the adapter; credentials per auth_mode — USER_PAT uses the
     PAT from THIS request's header (never a staged token).
  6. Record idempotency result, consume the proposal, return.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request

from app.adapters import resolve_credentials
from app.adapters.base import (
    AuthMode,
    DownstreamError,
    PayloadValidationError,
    UnsupportedActionError,
)
from app.auth.api_key import require_api_key
from app.auth.user_token import get_user_token
from app.errors import ApiError
from app.models.schemas import (
    ActionStatusResponse,
    ErrorCode,
    ExecuteRequest,
    ExecuteResponse,
    IdempotencyRecord,
    StoredProposal,
    utcnow,
)
from app.observability.correlation import (
    action_id_var,
    correlation_from_header_var,
    correlation_id_var,
    current_correlation_id,
    job_id_var,
)
from app.observability.logging import log_event
from app.state.token_vault import principal_hash

logger = logging.getLogger(__name__)

router = APIRouter(tags=["execute"])

_EXPIRED_MESSAGE = "Unknown, expired, or already-consumed action: {action_id}"


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _request_hash(action_id: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(f"{action_id}:{_canonical(payload)}".encode("utf-8")).hexdigest()


async def _authorized_proposal(
    request: Request, action_id: str, consumer_id: str, user_token: str | None
) -> StoredProposal:
    """Load + authorize a proposal. Cross-principal access is deliberately
    indistinguishable from expiry (no existence oracle)."""
    deps = request.app.state.deps
    stored = await deps.store.get_proposal(action_id)
    if stored is None:
        raise ApiError(409, ErrorCode.ACTION_EXPIRED, _EXPIRED_MESSAGE.format(action_id=action_id))

    if deps.adapter.auth_mode is AuthMode.USER_PAT:
        presented = principal_hash(user_token or "", deps.settings.principal_hash_salt)
        if presented != stored.principal_hash:
            log_event(logger, "action_principal_mismatch", level=logging.WARNING,
                      action_id=action_id)
            raise ApiError(
                409, ErrorCode.ACTION_EXPIRED, _EXPIRED_MESSAGE.format(action_id=action_id)
            )
    elif stored.consumer_id != consumer_id:
        log_event(logger, "action_consumer_mismatch", level=logging.WARNING, action_id=action_id)
        raise ApiError(409, ErrorCode.ACTION_EXPIRED, _EXPIRED_MESSAGE.format(action_id=action_id))

    action_id_var.set(action_id)
    job_id_var.set(stored.job_id)
    # End-to-end tracing: if the caller didn't send X-Correlation-Id, join
    # this call to its originating research run via the stored proposal.
    if not correlation_from_header_var.get():
        correlation_id_var.set(stored.action.correlation_id)
    return stored


def _verify_payload_against_proposal(stored: StoredProposal, approved: dict[str, Any]) -> None:
    proposed = stored.action.payload
    editable = set(stored.action.editable_fields)

    unexpected = {
        key
        for key in set(proposed) | set(approved)
        if key not in editable and proposed.get(key) != approved.get(key)
    }
    if unexpected:
        raise ApiError(
            409,
            ErrorCode.PAYLOAD_MISMATCH,
            "approved_payload differs from the stored proposal on non-editable fields.",
            details={"fields": sorted(unexpected), "editable_fields": sorted(editable)},
        )


@router.post("/execute", response_model=ExecuteResponse, response_model_exclude_none=True)
async def execute_action(
    body: ExecuteRequest,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> ExecuteResponse:
    deps = request.app.state.deps
    action_id_var.set(body.action_id)
    request_hash = _request_hash(body.action_id, body.approved_payload)
    log_event(logger, "execute_requested", action_id=body.action_id)

    # (0) Idempotency — replay returns the recorded outcome; reusing a key
    # for a different request is a caller bug and must fail loudly.
    existing = await deps.store.get_idempotency(idempotency_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise ApiError(
                409,
                ErrorCode.IDEMPOTENT_REPLAY,
                "Idempotency-Key was already used for a different action/payload.",
            )
        log_event(logger, "execute_replayed", action_id=body.action_id)
        replay = ExecuteResponse.model_validate(existing.response)
        replay.idempotent_replay = True
        replay.correlation_id = current_correlation_id()
        return replay

    # (1) Load + authorize the proposal.
    stored = await _authorized_proposal(request, body.action_id, consumer_id, user_token)
    if stored.action.expires_at <= utcnow():
        await deps.store.delete_proposal(body.action_id)
        raise ApiError(
            409, ErrorCode.ACTION_EXPIRED, _EXPIRED_MESSAGE.format(action_id=body.action_id)
        )

    # (2) Approved == proposed, except explicitly editable fields.
    _verify_payload_against_proposal(stored, body.approved_payload)

    # (3) Schema validation (edits could have broken a constraint).
    try:
        deps.adapter.validate_payload(stored.action.action_type, body.approved_payload)
    except UnsupportedActionError as exc:
        raise ApiError(409, ErrorCode.UNSUPPORTED_ACTION, str(exc)) from exc
    except PayloadValidationError as exc:
        raise ApiError(
            422, ErrorCode.VALIDATION_ERROR,
            f"approved_payload failed schema validation for {exc.action_type}",
            details={"errors": exc.errors},
        ) from exc
    log_event(logger, "execute_validated", action_id=body.action_id)

    credentials = resolve_credentials(deps.adapter, user_token=user_token)

    # (4) Live precondition re-check — approval is only valid for the state
    # the human saw.
    try:
        precheck = await deps.adapter.check_preconditions(stored.action, credentials)
    except DownstreamError as exc:
        raise ApiError(502, ErrorCode.DOWNSTREAM_ERROR, str(exc)) from exc
    if not precheck.ok:
        raise ApiError(
            409,
            ErrorCode.STALE_TARGET,
            "Target state drifted since research; re-run research and re-approve.",
            details=precheck.details,
        )

    # (5) The mutation — exactly once, no auto-retry.
    started = time.perf_counter()
    try:
        outcome = await deps.adapter.execute(stored.action, body.approved_payload, credentials)
    except UnsupportedActionError as exc:
        raise ApiError(409, ErrorCode.UNSUPPORTED_ACTION, str(exc)) from exc
    except DownstreamError as exc:
        log_event(logger, "execute_failed", level=logging.ERROR, action_id=body.action_id,
                  status=exc.status_code)
        raise ApiError(502, ErrorCode.DOWNSTREAM_ERROR, str(exc)) from exc

    response = ExecuteResponse(
        action_id=body.action_id,
        result=outcome.result,
        resource_url=outcome.resource_url,
        correlation_id=current_correlation_id(),
    )

    # (6) Record outcome, then consume the proposal (this order: a crash in
    # between leaves a replayable idempotency record, never a re-executable
    # proposal AND a lost result).
    await deps.store.put_idempotency(
        idempotency_key,
        IdempotencyRecord(request_hash=request_hash, response=response.model_dump(mode="json")),
    )
    await deps.store.delete_proposal(body.action_id)
    log_event(
        logger, "execute_succeeded", action_id=body.action_id,
        duration_ms=round((time.perf_counter() - started) * 1000),
        message=f"approved_by={body.approval.approved_by}",
    )
    log_event(logger, "proposal_consumed", action_id=body.action_id)
    return response


@router.get(
    "/actions/{action_id}", response_model=ActionStatusResponse, response_model_exclude_none=True
)
async def get_action(
    action_id: str,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
) -> ActionStatusResponse:
    stored = await _authorized_proposal(request, action_id, consumer_id, user_token)
    return ActionStatusResponse(
        action_id=action_id,
        status="proposed",
        expires_at=stored.action.expires_at,
        action=stored.action,
        correlation_id=current_correlation_id(),
    )


@router.delete(
    "/actions/{action_id}", response_model=ActionStatusResponse, response_model_exclude_none=True
)
async def reject_action(
    action_id: str,
    request: Request,
    consumer_id: str = Depends(require_api_key),
    user_token: str | None = Depends(get_user_token),
) -> ActionStatusResponse:
    """Consumer-initiated rejection cleanup (human said no in the approval UI)."""
    deps = request.app.state.deps
    await _authorized_proposal(request, action_id, consumer_id, user_token)
    await deps.store.delete_proposal(action_id)
    log_event(logger, "proposal_rejected_by_consumer", action_id=action_id)
    return ActionStatusResponse(
        action_id=action_id, status="rejected", correlation_id=current_correlation_id()
    )
