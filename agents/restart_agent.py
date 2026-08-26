"""
Restart Agent
=============
Extracts structured restart/enable/disable intent from a ServiceNow
service request and owns the deterministic execution helpers used by the
AMS flow.

The LLM does not call AWS directly. It only translates the ticket text
into fields that the approved job catalog and runtime executor can
validate and run.
"""
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict

from common.models import RestartExecutionResult, RestartJobDefinition, RestartRequestDetails, Ticket

if TYPE_CHECKING:
    from common.llm_client import AnthropicAgentClient
    from config import AnthropicSettings

logger = logging.getLogger(__name__)

KNOWN_SERVICES = {"glue"}
KNOWN_ACTIONS = {"restart", "enable", "disable"}

SYSTEM_PROMPT = """You are the Restart Agent for an AMS service catalog flow. \
Extract the user's job restart or job state-change request into structured fields only.

Supported service:
- glue

Supported actions:
- restart: rerun, retry, run now, or start a Glue job run
- enable: enable/start a Glue trigger or schedule
- disable: disable/stop a Glue trigger or schedule

Rules:
- Do not execute anything.
- Do not call AWS.
- Do not invent job names.
- Use service="glue" for AWS Glue job requests.
- Set insufficient_information=true if the job name or action is missing.

Return strict JSON with exactly these keys: service, action, job_name, confidence, rationale, \
insufficient_information."""


class RestartAgent:
    def __init__(self, llm_client: "AnthropicAgentClient", anthropic_settings: "AnthropicSettings"):
        self.llm_client = llm_client
        self.model = anthropic_settings.model_restart_request_parser

    def parse(self, ticket: Ticket) -> RestartRequestDetails:
        user_prompt = f"""Service request:
Catalog task number: {ticket.number}
Catalog task short description: {ticket.short_description}
Catalog task description: {ticket.description}
Parent RITM number: {ticket.request_item_number or "unknown"}
Parent RITM short description: {ticket.request_item_short_description or "unknown"}
Parent RITM description: {ticket.request_item_description or "unknown"}
Configuration Item: {ticket.cmdb_ci_name or "unknown"}

Extract the restart request fields."""

        result = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt, model=self.model, temperature=0.1)

        service = self._normalize_service(result.get("service"))
        action = self._normalize_action(result.get("action"))
        job_name = self._clean_job_name(result.get("job_name"))
        confidence = self._confidence(result.get("confidence"))

        # The model extracts intent, but code decides whether required fields are
        # present. This prevents a contradictory model flag from blocking a valid
        # request; the approved-job catalog still validates execution separately.
        insufficient = service not in KNOWN_SERVICES or action not in KNOWN_ACTIONS or not job_name

        details = RestartRequestDetails(
            ticket_number=ticket.number,
            service=service if service in KNOWN_SERVICES else "glue",
            action=action if action in KNOWN_ACTIONS else None,
            job_name=job_name,
            confidence=confidence,
            rationale=result.get("rationale", ""),
            insufficient_information=insufficient,
        )

        logger.info(
            "Parsed restart request %s: service=%s action=%s job=%s insufficient=%s",
            ticket.number,
            details.service,
            details.action,
            details.job_name,
            details.insufficient_information,
        )
        return details

    @staticmethod
    def _normalize_service(value) -> str:
        if not isinstance(value, str):
            return "glue"
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "aws_glue": "glue",
            "glue_job": "glue",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_action(value) -> str:
        if not isinstance(value, str):
            return ""
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "rerun": "restart",
            "retry": "restart",
            "run": "restart",
            "run_now": "restart",
            "start": "restart",
            "start_job": "restart",
            "turn_on": "enable",
            "activate": "enable",
            "enabled": "enable",
            "turn_off": "disable",
            "deactivate": "disable",
            "disabled": "disable",
            "stop": "disable",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _clean_job_name(value) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()

    @staticmethod
    def _confidence(value) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))


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


class RestartService:
    def __init__(self, catalog: RestartJobCatalog, glue_client=None):
        self.catalog = catalog
        self.glue = glue_client

    def run(self, request: RestartRequestDetails) -> RestartExecutionResult:
        try:
            if request.insufficient_information:
                raise ValueError(request.rationale or "Restart request is missing required information.")
            if request.service != "glue":
                raise ValueError(f"Unsupported restart service '{request.service}'.")
            if not request.action or not request.job_name:
                raise ValueError("Restart request requires both action and job_name.")

            definition = self.catalog.get(request.job_name)
            self.catalog.validate_action(definition, request.action)
            glue = self.glue or self._glue_client()

            if request.action == "restart":
                message = self._start_glue_job(glue, definition.job_name)
            elif request.action == "enable":
                message = self._start_glue_trigger(glue, definition.trigger_name)
            elif request.action == "disable":
                message = self._stop_glue_trigger(glue, definition.trigger_name)
            else:
                raise ValueError(f"Unsupported restart action '{request.action}'.")

            return RestartExecutionResult(
                ticket_number=request.ticket_number,
                status="RESTART_EXECUTED",
                service=definition.service,
                action=request.action,
                job_name=definition.job_name,
                trigger_name=definition.trigger_name,
                message=message,
                executed=True,
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Restart request %s rejected: %s", request.ticket_number, exc)
            return RestartExecutionResult(
                ticket_number=request.ticket_number,
                status="FAILED",
                service=request.service,
                action=request.action,
                job_name=request.job_name,
                message=str(exc),
                executed=False,
            )
        except Exception as exc:
            logger.exception("Restart request %s failed", request.ticket_number)
            return RestartExecutionResult(
                ticket_number=request.ticket_number,
                status="FAILED",
                service=request.service,
                action=request.action,
                job_name=request.job_name,
                message=str(exc),
                executed=False,
            )

    @staticmethod
    def _glue_client():
        from common.aws_client import get_client

        return get_client("glue")

    @staticmethod
    def _start_glue_job(glue, job_name: str) -> str:
        response = glue.start_job_run(JobName=job_name)
        run_id = response.get("JobRunId", "unknown")
        return f"Started Glue job '{job_name}' with run id {run_id}."

    @staticmethod
    def _start_glue_trigger(glue, trigger_name: str) -> str:
        glue.start_trigger(Name=trigger_name)
        return f"Enabled Glue trigger '{trigger_name}'."

    @staticmethod
    def _stop_glue_trigger(glue, trigger_name: str) -> str:
        glue.stop_trigger(Name=trigger_name)
        return f"Disabled Glue trigger '{trigger_name}'."
