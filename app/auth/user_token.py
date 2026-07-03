"""TEMPLATE_CORE — X-User-Token handling (Layer 2: this service -> target).

All branching on the domain's ``auth_mode`` lives here — endpoints never
assume a PAT exists:

* USER_PAT            → header required (400 MISSING_USER_TOKEN when absent);
                        value registered for log redaction immediately.
* SERVICE_CREDENTIAL  → header ignored if sent; credential is service-owned.
* NONE                → header ignored.
"""

from __future__ import annotations

from fastapi import Request

from app.adapters.base import AuthMode
from app.errors import ApiError
from app.models.schemas import ErrorCode
from app.observability.logging import register_secret

USER_TOKEN_HEADER = "X-User-Token"


async def get_user_token(request: Request) -> str | None:
    """FastAPI dependency: resolve the user PAT according to auth_mode."""
    adapter = request.app.state.deps.adapter
    token = request.headers.get(USER_TOKEN_HEADER)

    if adapter.auth_mode is not AuthMode.USER_PAT:
        # SERVICE_CREDENTIAL / NONE: never accepted from the caller.
        return None

    if not token:
        raise ApiError(
            400,
            ErrorCode.MISSING_USER_TOKEN,
            f"{USER_TOKEN_HEADER} header is required for the "
            f"'{adapter.name}' domain (auth_mode=USER_PAT).",
        )
    register_secret(token)  # scrub from any log line before anything else runs
    return token
