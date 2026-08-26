"""
Approved job catalog for the Restart Agent.

The LLM may extract a requested job name and action, but only this
catalog decides whether the job is approved and which Glue trigger may
be changed.
"""
import json
from pathlib import Path
from typing import Dict

from common.models import RestartJobDefinition


class RestartJobCatalog:
    def __init__(self, catalog_path: str):
        self.catalog_path = Path(catalog_path)
        self._jobs: Dict[str, RestartJobDefinition] = {}
        self._aliases: Dict[str, str] = {}
        self._load()

    def get(self, job_name: str) -> RestartJobDefinition:
        key = self._normalize(job_name)
        canonical = self._aliases.get(key, key)
        try:
            return self._jobs[canonical]
        except KeyError as exc:
            raise ValueError(f"Restart job '{job_name}' is not in the approved catalog.") from exc

    def validate_action(self, definition: RestartJobDefinition, action: str) -> None:
        if action not in definition.allowed_actions:
            allowed = ", ".join(definition.allowed_actions) or "none"
            raise ValueError(f"Action '{action}' is not allowed for job '{definition.job_name}'. Allowed: {allowed}.")
        if action in ("enable", "disable") and not definition.trigger_name:
            raise ValueError(f"Action '{action}' for job '{definition.job_name}' requires a trigger_name.")

    def _load(self) -> None:
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        for item in raw.get("jobs", []):
            definition = RestartJobDefinition(**item)
            key = self._normalize(definition.job_name)
            self._jobs[key] = definition
            self._aliases[key] = key
            for alias in definition.aliases:
                self._aliases[self._normalize(alias)] = key

    @staticmethod
    def _normalize(value: str) -> str:
        return (value or "").strip().lower().replace("-", "_").replace(" ", "_")
