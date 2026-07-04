"""TEMPLATE_CORE — AzureChatOpenAI factory with Entra ID (AAD) auth.

No API keys anywhere: DefaultAzureCredential resolves workload identity on
AKS, `az login` locally. Imports are lazy so only the worker pays them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def _build(settings: "Settings", deployment: str) -> Any:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import AzureChatOpenAI

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SCOPE)
    return AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_deployment=deployment,
        api_version=settings.azure_openai_api_version,
        azure_ad_token_provider=token_provider,
        temperature=0,
    )


def build_llm(settings: "Settings") -> Any:
    """Primary model: planning, synthesis, and action proposals."""
    return _build(settings, settings.azure_openai_deployment)


def build_fast_llm(settings: "Settings") -> Any | None:
    """Optional fast tier for retrieval sub-agents (model tiering). Returns
    None when unconfigured — the harness then runs single-model."""
    if not settings.azure_openai_fast_deployment:
        return None
    return _build(settings, settings.azure_openai_fast_deployment)


def llm_configured(settings: "Settings") -> bool:
    return bool(settings.azure_openai_endpoint and settings.azure_openai_deployment)
