"""
Service Request Router AI Agent
===============================
Classifies ServiceNow service catalog requests into the specialist flow
that should handle them.
"""
import logging
from typing import TYPE_CHECKING

from common.models import ServiceRequestRoute, Ticket

if TYPE_CHECKING:
    from common.llm_client import AnthropicAgentClient
    from config import AnthropicSettings

logger = logging.getLogger(__name__)

REQUEST_REPORT_GENERATION = "report_generation"
REQUEST_GLUE_JOB_CONTROL = "glue_job_control"
REQUEST_UNSUPPORTED = "unsupported"

KNOWN_REQUEST_TYPES = {
    REQUEST_REPORT_GENERATION,
    REQUEST_GLUE_JOB_CONTROL,
    REQUEST_UNSUPPORTED,
}

SYSTEM_PROMPT = """You are the Service Request Router AI Agent for an Application Managed Services \
(AMS) team. Your only job is to classify a ServiceNow service request into exactly one route:

- report_generation: the user is asking for an operational/business report to be generated, scheduled, \
  or emailed.
- glue_job_control: the user is asking to enable, disable, start, stop, or change the run state of an \
  AWS Glue job.
- unsupported: the request is not one of the above.

Do not execute anything. Do not write SQL. Do not invent missing details.

Return strict JSON with exactly these keys: request_type, confidence, rationale, insufficient_information.
request_type must be one of: report_generation, glue_job_control, unsupported.
confidence must be a float between 0.0 and 1.0.
Set insufficient_information=true when the ticket text is too vague to choose a route safely."""


class ServiceRequestRouterAgent:
    def __init__(self, llm_client: "AnthropicAgentClient", anthropic_settings: "AnthropicSettings"):
        self.llm_client = llm_client
        self.model = anthropic_settings.model_service_request_router

    def route(self, ticket: Ticket) -> ServiceRequestRoute:
        user_prompt = f"""Service request to classify:
Number: {ticket.number}
Short description: {ticket.short_description}
Description: {ticket.description}
Configuration Item: {ticket.cmdb_ci_name or "unknown"}

Choose the route for this service request."""

        result = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt, model=self.model, temperature=0.1)

        request_type = self._normalize_request_type(result.get("request_type"))
        confidence = self._confidence(result.get("confidence"))
        insufficient = bool(result.get("insufficient_information", False))

        if request_type not in KNOWN_REQUEST_TYPES:
            request_type = REQUEST_UNSUPPORTED
            insufficient = True
            confidence = 0.0

        rationale = result.get("rationale") or "No rationale returned by the service request router."
        route = ServiceRequestRoute(
            ticket_number=ticket.number,
            request_type=request_type,
            confidence=confidence,
            rationale=rationale,
            insufficient_information=insufficient,
        )

        logger.info(
            "Service request %s routed to %s (confidence=%.2f)",
            ticket.number,
            route.request_type,
            route.confidence,
        )
        return route

    @staticmethod
    def _normalize_request_type(value) -> str:
        if not isinstance(value, str):
            return REQUEST_UNSUPPORTED
        return value.strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _confidence(value) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))
