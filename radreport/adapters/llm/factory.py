"""Builds the client for a resolved model: the Anthropic SDK for Anthropic, the OpenAI-compatible client for everything else.

Defines: client_for.
"""

from __future__ import annotations

import os

from radreport.adapters.llm.base import LLMClient, ResolvedModelRef
from radreport.core.errors import ModelResolutionError
from radreport.core.types import ProviderKind


def client_for(ref: ResolvedModelRef, *, api_key_env_var: str | None = None) -> LLMClient:
    """A client for this model; the key is read from the environment, never from the database."""
    key = os.environ.get(api_key_env_var) if api_key_env_var else None
    if ref.provider_kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE or (ref.endpoint and ref.provider_name != "anthropic"):
        from radreport.adapters.llm.openai_compat import OpenAICompatibleClient

        return OpenAICompatibleClient(model_ref=ref, api_key=key)
    if ref.provider_name == "anthropic":
        from radreport.adapters.llm.anthropic_client import AnthropicClient

        key = key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ModelResolutionError("the anthropic provider needs an API key in its api_key_env_var (or ANTHROPIC_API_KEY)")
        return AnthropicClient(key, model_ref=ref)
    raise ModelResolutionError(f"no client for provider {ref.provider_name!r} without an endpoint")
