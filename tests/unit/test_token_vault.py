from __future__ import annotations

import base64

import pytest

from app.state.token_vault import (
    LocalTokenVault,
    NullTokenVault,
    TokenVaultError,
    build_token_vault,
    principal_hash,
)

KEY = b"k" * 32


async def test_roundtrip():
    vault = LocalTokenVault(KEY)
    blob = await vault.encrypt("my-user-pat")
    assert "my-user-pat" not in blob
    assert await vault.decrypt(blob) == "my-user-pat"


async def test_fresh_dek_per_job():
    vault = LocalTokenVault(KEY)
    assert await vault.encrypt("same") != await vault.encrypt("same")


async def test_wrong_kek_fails_closed():
    blob = await LocalTokenVault(KEY).encrypt("my-user-pat")
    with pytest.raises(TokenVaultError):
        await LocalTokenVault(b"x" * 32).decrypt(blob)


async def test_null_vault_refuses_everything():
    vault = NullTokenVault()
    with pytest.raises(TokenVaultError):
        await vault.encrypt("pat")
    with pytest.raises(TokenVaultError):
        await vault.decrypt("blob")


def test_local_vault_forbidden_in_prod(settings):
    prod = settings.model_copy(update={"env": "prod"})
    with pytest.raises(TokenVaultError):
        build_token_vault(prod)


def test_principal_hash_is_salted_and_deterministic():
    assert principal_hash("tok", "salt-a") == principal_hash("tok", "salt-a")
    assert principal_hash("tok", "salt-a") != principal_hash("tok", "salt-b")
    assert principal_hash("tok", "salt-a") != principal_hash("other", "salt-a")
    assert "tok" not in principal_hash("tok", "salt-a")


def test_local_key_must_be_32_bytes():
    with pytest.raises(TokenVaultError):
        LocalTokenVault(b"short")
