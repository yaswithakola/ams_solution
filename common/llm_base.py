"""
Shared interface for agent LLM clients.
"""
from typing import Optional, Protocol


class AgentLLMClient(Protocol):
    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        ...

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        ...
