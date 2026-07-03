from __future__ import annotations

from app.auth.api_key import verify_api_key

KEYS = {"consumer-a": "secret-aaaa-1111", "consumer-b": "secret-bbbb-2222"}


def test_valid_key_returns_consumer_id():
    assert verify_api_key("secret-aaaa-1111", KEYS) == "consumer-a"
    assert verify_api_key("secret-bbbb-2222", KEYS) == "consumer-b"


def test_invalid_key_returns_none():
    assert verify_api_key("secret-aaaa-1112", KEYS) is None
    assert verify_api_key("", KEYS) is None
    assert verify_api_key("secret-aaaa-1111x", KEYS) is None


def test_no_keys_configured_rejects_everything():
    assert verify_api_key("anything", {}) is None
