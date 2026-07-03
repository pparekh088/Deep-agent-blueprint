from __future__ import annotations

import json
import logging

import pytest

from app.observability.logging import (
    REDACTED,
    build_formatter,
    clear_secrets,
    register_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


def _format(settings, **record_kwargs) -> str:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=record_kwargs.pop("msg", "hello"), args=(), exc_info=record_kwargs.pop("exc_info", None),
    )
    for key, value in record_kwargs.items():
        setattr(record, key, value)
    return build_formatter(settings).format(record)


def test_output_is_single_line_json_with_canonical_keys(settings):
    line = _format(settings, event="job_submitted", duration_ms=12, status="queued")
    assert "\n" not in line
    record = json.loads(line)
    assert record["service"] == settings.service_name
    assert record["domain"] == "jira"
    assert record["event"] == "job_submitted"
    assert record["duration_ms"] == 12
    assert record["status"] == "queued"
    assert record["message"] == "hello"
    # Null fields allowed, keys never vary:
    for key in ("correlation_id", "request_id", "job_id", "action_id", "consumer_id"):
        assert key in record


def test_registered_secrets_are_redacted_everywhere(settings):
    register_secret("super-secret-pat-123")
    line = _format(settings, msg="token super-secret-pat-123 rejected")
    assert "super-secret-pat-123" not in line
    assert REDACTED in line


def test_exceptions_serialize_to_a_single_line_and_are_redacted(settings):
    register_secret("super-secret-pat-123")
    try:
        raise ValueError("boom with super-secret-pat-123")
    except ValueError:
        import sys

        line = _format(settings, msg="failed", exc_info=sys.exc_info())
    assert "\n" not in line
    record = json.loads(line)
    assert "ValueError" in record["exception"]
    assert "super-secret-pat-123" not in line


def test_short_values_are_not_registered(settings):
    register_secret("abc")  # too short — would redact legitimate substrings
    line = _format(settings, msg="abcdef")
    assert json.loads(line)["message"] == "abcdef"
