"""Contract: layer-1 API-key auth and layer-2 auth-mode branching."""

from __future__ import annotations

import pytest

from app.adapters.base import AuthMode
from tests.contract.conftest import API_KEYS


@pytest.mark.parametrize("path,method", [("/research", "POST"), ("/health", "GET")])
async def test_missing_api_key_is_401(env, path, method):
    response = await env.client.request(method, path, headers=env.headers(api_key=None))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


async def test_invalid_api_key_is_401(env):
    response = await env.submit(api_key="wrong-key-000000")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_API_KEY"


async def test_all_active_keys_accepted_for_rotation(env):
    for key in API_KEYS.values():
        response = await env.client.get("/health", headers=env.headers(api_key=key))
        assert response.status_code == 200


async def test_liveness_probe_is_unauthenticated(env):
    response = await env.client.get("/health/live")
    assert response.status_code == 200


async def test_user_token_requirement_branches_on_auth_mode(env):
    response = await env.submit(token=False)
    if env.case.user_token is not None:  # USER_PAT
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "MISSING_USER_TOKEN"
    else:  # SERVICE_CREDENTIAL / NONE — no PAT required
        assert response.status_code == 202


async def test_non_pat_domains_ignore_user_token_header(env):
    if env.case.user_token is not None:
        pytest.skip("USER_PAT domain")
    response = await env.submit(extra={"X-User-Token": "unsolicited-token-12345"})
    assert response.status_code == 202
    # ...and nothing was staged for it.
    keys = await env.store.scan_keys(f"{env.settings.domain}:tok:*")
    assert keys == []


async def test_auth_mode_is_declared(env):
    adapter = env.worker_deps.adapter
    assert adapter.auth_mode in AuthMode
    assert adapter.name == env.settings.domain
