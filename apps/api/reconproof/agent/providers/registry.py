"""Provider selection.

Resolution order under ``auto``: a configured Anthropic key, then a reachable
local Ollama model, then the deterministic provider. The deterministic provider
is last but never absent, so ``resolve_provider`` always returns something usable
and no caller has to handle "no provider".
"""

from __future__ import annotations

from typing import Any

import structlog

from reconproof.agent.providers.base import ReasoningProvider
from reconproof.agent.providers.deterministic import DeterministicProvider
from reconproof.agent.providers.llm import AnthropicProvider, OllamaProvider
from reconproof.config import Settings, get_settings

logger = structlog.get_logger(__name__)


def resolve_provider(settings: Settings | None = None) -> ReasoningProvider:
    settings = settings or get_settings()
    choice = settings.llm_provider.lower()

    if choice == "deterministic":
        return DeterministicProvider()
    if choice == "anthropic":
        provider = AnthropicProvider(settings)
        return provider if provider.available() else DeterministicProvider()
    if choice == "ollama":
        provider = OllamaProvider(settings)
        return provider if provider.available() else DeterministicProvider()

    anthropic = AnthropicProvider(settings)
    if anthropic.available():
        return anthropic
    ollama = OllamaProvider(settings)
    if ollama.available():
        return ollama
    logger.info("provider.fallback", reason="no language model reachable")
    return DeterministicProvider()


def describe_provider(settings: Settings | None = None) -> dict[str, Any]:
    """Report which provider is active, for the health endpoint and the UI."""
    settings = settings or get_settings()
    provider = resolve_provider(settings)
    return {
        "name": provider.name,
        "model": provider.model_name,
        "available": True,
        "configured": settings.llm_provider,
        "is_fallback": provider.name == "deterministic",
        "note": (
            "No language model is reachable. Investigations run on deterministic "
            "rules; reconciliation itself is unaffected."
            if provider.name == "deterministic"
            else None
        ),
    }
