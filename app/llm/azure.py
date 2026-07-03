"""TEMPLATE_CORE — AzureChatOpenAI factory with Entra ID (AAD) auth.

No API keys anywhere: DefaultAzureCredential resolves workload identity on
AKS, `az login` locally. Imports are lazy so only the worker pays them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


def build_llm(settings: "Settings") -> Any:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import AzureChatOpenAI

    token_provider = get_bearer_token_provider(DefaultAzureCredential(), _COGNITIVE_SCOPE)
    return AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_deployment=settings.azure_openai_deployment,
        api_version=settings.azure_openai_api_version,
        azure_ad_token_provider=token_provider,
        temperature=0,
    )


def llm_configured(settings: "Settings") -> bool:
    return bool(settings.azure_openai_endpoint and settings.azure_openai_deployment)
