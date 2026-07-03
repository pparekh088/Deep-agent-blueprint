"""TEMPLATE_CORE — PAT envelope encryption and principal hashing.

This module exists because of ONE documented exception to the "no credential
persistence" rule (BLUEPRINT.md §Token handling, ADR-0005): the deferred
worker model means a USER_PAT domain must stage the PAT between submit and
worker pickup. The staged form is ciphertext only:

    1. Generate a fresh 256-bit data-encryption key (DEK) per job.
    2. AES-256-GCM encrypt the PAT with the DEK.
    3. Wrap the DEK with an Azure Key Vault key (RSA-OAEP-256) — the KEK
       never leaves Key Vault.
    4. Store base64(JSON{wrapped_dek, nonce, ciphertext}) in Redis; the
       plaintext PAT exists only in request/worker memory.

If Key Vault is unreachable the service FAILS CLOSED: submission is rejected
(503 DEPENDENCY_UNAVAILABLE) rather than staging a weaker form of the token.

``LocalTokenVault`` is a dev/test stand-in (static local KEK). It refuses to
run when ENV=prod.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from app.config import Settings

_NONCE_LEN = 12
_DEK_LEN = 32


class TokenVaultError(Exception):
    """Encryption/decryption dependency failure — callers fail closed."""


def principal_hash(token: str, salt: str) -> str:
    """Salted hash binding a job/proposal to the submitting user. Stored in
    Redis for poll/cancel/execute authorization — never the token itself."""
    return hashlib.sha256(f"{salt}:{token}".encode("utf-8")).hexdigest()


def _pack(envelope: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")


def _unpack(blob: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(blob.encode("ascii")))


class BaseTokenVault(ABC):
    @abstractmethod
    async def encrypt(self, plaintext: str) -> str:
        """Plaintext PAT -> opaque ciphertext blob safe to stage in Redis."""

    @abstractmethod
    async def decrypt(self, blob: str) -> str:
        """Ciphertext blob -> plaintext PAT (worker memory only)."""

    async def aclose(self) -> None:  # noqa: B027 — optional override
        pass


class KeyVaultTokenVault(BaseTokenVault):
    """Production vault: per-job DEK wrapped by an Azure Key Vault key."""

    ALG = "kv-rsa-oaep-256+aesgcm"

    def __init__(self, key_id: str) -> None:
        self._key_id = key_id
        self._client: Any = None
        self._credential: Any = None

    def _crypto_client(self) -> Any:
        if self._client is None:
            # Lazy import: dev/test paths never need the Azure SDK loaded.
            from azure.identity.aio import DefaultAzureCredential
            from azure.keyvault.keys.crypto.aio import CryptographyClient

            self._credential = DefaultAzureCredential()
            self._client = CryptographyClient(self._key_id, credential=self._credential)
        return self._client

    async def encrypt(self, plaintext: str) -> str:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        try:
            dek = os.urandom(_DEK_LEN)
            nonce = os.urandom(_NONCE_LEN)
            ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
            wrap_result = await self._crypto_client().wrap_key(
                KeyWrapAlgorithm.rsa_oaep_256, dek
            )
            return _pack(
                {
                    "v": 1,
                    "alg": self.ALG,
                    "kid": self._key_id,
                    "wrapped_dek": base64.b64encode(wrap_result.encrypted_key).decode("ascii"),
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ct": base64.b64encode(ciphertext).decode("ascii"),
                }
            )
        except Exception as exc:  # noqa: BLE001 — fail closed with a typed error
            raise TokenVaultError("Key Vault token encryption failed") from exc

    async def decrypt(self, blob: str) -> str:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        try:
            envelope = _unpack(blob)
            unwrap_result = await self._crypto_client().unwrap_key(
                KeyWrapAlgorithm.rsa_oaep_256,
                base64.b64decode(envelope["wrapped_dek"]),
            )
            return (
                AESGCM(unwrap_result.key)
                .decrypt(
                    base64.b64decode(envelope["nonce"]),
                    base64.b64decode(envelope["ct"]),
                    None,
                )
                .decode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise TokenVaultError("Key Vault token decryption failed") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._credential is not None:
            await self._credential.close()


class LocalTokenVault(BaseTokenVault):
    """Dev/test vault: same envelope shape, local AES-GCM KEK. NEVER prod."""

    ALG = "local-aesgcm+aesgcm"

    def __init__(self, key: bytes) -> None:
        if len(key) != _DEK_LEN:
            raise TokenVaultError("LocalTokenVault requires a 32-byte key")
        self._kek = key

    async def encrypt(self, plaintext: str) -> str:
        dek = os.urandom(_DEK_LEN)
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)
        wrap_nonce = os.urandom(_NONCE_LEN)
        wrapped = AESGCM(self._kek).encrypt(wrap_nonce, dek, None)
        return _pack(
            {
                "v": 1,
                "alg": self.ALG,
                "wrapped_dek": base64.b64encode(wrap_nonce + wrapped).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ct": base64.b64encode(ciphertext).decode("ascii"),
            }
        )

    async def decrypt(self, blob: str) -> str:
        try:
            envelope = _unpack(blob)
            wrapped = base64.b64decode(envelope["wrapped_dek"])
            dek = AESGCM(self._kek).decrypt(wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:], None)
            return (
                AESGCM(dek)
                .decrypt(
                    base64.b64decode(envelope["nonce"]),
                    base64.b64decode(envelope["ct"]),
                    None,
                )
                .decode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            raise TokenVaultError("local token decryption failed") from exc


class NullTokenVault(BaseTokenVault):
    """Bound for SERVICE_CREDENTIAL / NONE domains — token staging is a
    contract violation there, so any use raises."""

    async def encrypt(self, plaintext: str) -> str:
        raise TokenVaultError("token staging is not available for this auth_mode")

    async def decrypt(self, blob: str) -> str:
        raise TokenVaultError("token staging is not available for this auth_mode")


def build_token_vault(settings: "Settings") -> BaseTokenVault:
    if settings.azure_keyvault_key_id:
        return KeyVaultTokenVault(settings.azure_keyvault_key_id)
    if settings.local_crypto_key_b64:
        if settings.env == "prod":
            raise TokenVaultError("LOCAL_CRYPTO_KEY_B64 is forbidden when ENV=prod")
        return LocalTokenVault(base64.b64decode(settings.local_crypto_key_b64))
    return NullTokenVault()
