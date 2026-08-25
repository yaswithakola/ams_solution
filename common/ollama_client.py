"""
Local Ollama client used by AMS agents.
"""
import json
import logging
import re
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from config import OllamaSettings

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINKING_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaAgentClient:
    """
    Matches the AnthropicAgentClient interface while running models locally.
    """

    def __init__(self, settings: "OllamaSettings"):
        self.base_url = settings.base_url.rstrip("/")
        self.timeout_seconds = settings.timeout_seconds
        self.max_tokens = settings.max_tokens
        self.default_model = settings.model

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        payload = {
            "model": model or self.default_model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": 0.0 if temperature is None else temperature,
                "num_predict": max_tokens or self.max_tokens,
            },
        }
        return self._chat(payload)

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        strict_system = (
            f"{system_prompt}\n\n"
            "Return only one valid JSON object. Do not include markdown, commentary, or reasoning."
        )
        payload = {
            "model": model or self.default_model,
            "stream": False,
            "format": "json",
            "think": False,
            "messages": [
                {"role": "system", "content": strict_system},
                {"role": "user", "content": f"/no_think\n{user_prompt}"},
            ],
            "options": {
                "temperature": 0.0 if temperature is None else temperature,
                "num_predict": max_tokens or self.max_tokens,
            },
        }
        raw = self._chat(payload)
        return _load_json(raw)

    def _chat(self, payload: dict) -> str:
        last_error = None
        for attempt in range(2):
            try:
                return self._post_chat(payload)
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning("Ollama request failed; retrying once: %s", exc)
                    time.sleep(1)
        raise RuntimeError("Ollama request failed after retry") from last_error

    def _post_chat(self, payload: dict) -> str:
        import requests

        response = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        message = body.get("message") or {}
        content = message.get("content") or body.get("response") or ""
        if not content.strip():
            raise ValueError("Ollama returned an empty response")
        return content


def _load_json(raw: str) -> dict:
    cleaned = _THINKING_RE.sub("", raw).strip()
    cleaned = _JSON_FENCE_RE.sub("", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        logger.error("Failed to parse JSON from Ollama response: %s", raw)
        raise ValueError("Ollama returned invalid JSON")
