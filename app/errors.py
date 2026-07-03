"""TEMPLATE_CORE — typed API errors.

Raise ``ApiError`` anywhere in the request path; the handler in ``app.main``
turns it into the canonical error envelope with the correlation ID attached.
"""

from __future__ import annotations

from typing import Any

from app.models.schemas import ErrorCode


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
