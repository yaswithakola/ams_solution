"""
Restart Agent
=============
Extracts structured restart/enable/disable intent from a ServiceNow
catalog task.

The agent does not call AWS directly. It only translates the ticket text
into fields that the deterministic RestartService can validate and run.
"""
import logging
from typing import TYPE_CHECKING

from common.models import RestartRequestDetails, Ticket

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
