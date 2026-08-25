"""
Factory for choosing the configured LLM provider.
"""
from typing import Tuple

from common.llm_base import AgentLLMClient
from common.ollama_client import OllamaAgentClient
from config import Settings


def build_llm_client(settings: Settings) -> Tuple[AgentLLMClient, object]:
    provider = settings.llm_provider.strip().lower()

    if provider == "ollama":
        return OllamaAgentClient(settings.ollama), settings.ollama

    if provider in ("anthropic", "claude", "sonnet"):
        from common.llm_client import AnthropicAgentClient

        return AnthropicAgentClient(settings.anthropic), settings.anthropic

    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
