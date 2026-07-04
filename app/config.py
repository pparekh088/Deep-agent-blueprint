"""TEMPLATE_CORE — service configuration via environment variables.

All config is env-driven (12-factor). Domain adapters get their own clearly
marked block at the bottom; everything else is template core and identical
across domains.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Identity ────────────────────────────────────────────────────────────
    service_name: str = "domain-deep-agent"
    domain: str = "jira"  # selects the DomainAdapter (see app/adapters/__init__.py)
    env: str = "dev"

    # ── Layer-1 auth: consumer API keys ─────────────────────────────────────
    # JSON object {"<consumer-id>": "<secret>"}. Several keys may be active at
    # once so consumers can rotate with zero downtime. In production this env
    # var is projected from Azure Key Vault at deploy time.
    api_keys: dict[str, str] = {}

    # ── Redis / queue ───────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "arq:queue"  # arq default; KEDA scales workers on its depth

    # ── Job semantics & TTLs (seconds) ──────────────────────────────────────
    result_ttl_s: int = 7200        # completed research results
    proposal_ttl_s: int = 1800      # proposed actions, from research completion
    idempotency_ttl_s: int = 86400  # /execute idempotency records
    job_timeout_s: int = 600        # per-attempt research budget
    job_max_attempts: int = 2       # research is read-only → safe to re-run
    estimated_wait_s: int = 5       # advertised in the 202 response

    # Per-run cap on concurrent downstream READ calls. Parallel fan-out is
    # encouraged (agent prompt) and this cap keeps it from stampeding a
    # rate-limited target — every 429 costs a backoff sleep that is slower
    # than briefly queueing here. 0 disables the cap.
    max_concurrent_reads: int = 6

    # ── Agent harness ───────────────────────────────────────────────────────
    # Registered factory name (app/agent/factory.py). Swapping the harness is
    # a config change + factory registration — never an API contract change.
    agent_factory: str = "deepagents"

    # ── Azure OpenAI (Entra ID auth — no API keys) ──────────────────────────
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    # Optional cheaper/faster deployment for retrieval sub-agents (model
    # tiering). Empty = single-model behavior: planning, retrieval, and
    # synthesis all run on azure_openai_deployment.
    azure_openai_fast_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"

    # ── PAT envelope encryption (USER_PAT domains only) ─────────────────────
    # Production: Key Vault key identifier URL, e.g.
    #   https://<vault>.vault.azure.net/keys/<name>/<version>
    # Dev/test fallback: LOCAL_CRYPTO_KEY_B64 (base64 of 32 bytes). The local
    # vault refuses to start when ENV=prod.
    azure_keyvault_key_id: str = ""
    local_crypto_key_b64: str = ""

    # Salt for the salted principal hash used to authorize poll/cancel/execute
    # against the submitting user (USER_PAT domains).
    principal_hash_salt: str = "change-me"

    # ── DOMAIN-SPECIFIC config surface ──────────────────────────────────────
    # Only the active domain's block is used. New domains append here (the
    # scaffold prints the exact snippet); unused fields are inert.
    jira_base_url: str = ""
    websearch_api_key: str = ""
    websearch_base_url: str = "https://api.tavily.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
