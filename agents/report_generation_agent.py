"""
Report Generation AI Agent
==========================
Extracts structured report intent from a ServiceNow service request.

This agent does not run SQL and never receives database result data. Its
job is only to translate ticket text into validated parameters for the
deterministic report execution layer.
"""
import logging
from typing import TYPE_CHECKING

from common.models import ReportRequestDetails, Ticket

if TYPE_CHECKING:
    from common.llm_client import AnthropicAgentClient
    from config import AnthropicSettings

logger = logging.getLogger(__name__)

KNOWN_REPORTS = {
    "claims_failure",
    "member_enrollment",
    "billing_summary",
    "batch_job_failure",
}
KNOWN_FREQUENCIES = {"daily", "weekly", "monthly", "adhoc"}
KNOWN_DATE_RANGES = {"today", "yesterday", "previous_week", "previous_month", "custom"}

SYSTEM_PROMPT = """You are the Report Generation AI Agent for an AMS service catalog flow. \
Extract the user's report request into structured fields only.

Approved reports:
- claims_failure
- member_enrollment
- billing_summary
- batch_job_failure

Rules:
- Do not generate SQL.
- Do not execute a report.
- Do not calculate actual dates. Use date_range labels only: today, yesterday, previous_week, \
  previous_month, or custom. Only return start_date/end_date when the user gives explicit custom dates.
- Put business filters such as state=Texas in the filters object.
- If the request does not name an email recipient, use the ServiceNow requester email when it is available.
- Use output_format="excel" unless the user explicitly asks for another format. If they do, still \
  return excel and explain the limitation in rationale.
- If the report name cannot be matched to the approved report list, set insufficient_information=true.

Return strict JSON with exactly these keys: report_name, frequency, date_range, start_date, end_date, \
filters, output_format, recipient, rationale, insufficient_information."""


class ReportGenerationAgent:
    def __init__(self, llm_client: "AnthropicAgentClient", anthropic_settings: "AnthropicSettings"):
        self.llm_client = llm_client
        self.model = anthropic_settings.model_report_request_parser

    def parse(self, ticket: Ticket) -> ReportRequestDetails:
        user_prompt = f"""Service request:
Catalog task number: {ticket.number}
Catalog task short description: {ticket.short_description}
Catalog task description: {ticket.description}
Parent RITM number: {ticket.request_item_number or "unknown"}
Parent RITM short description: {ticket.request_item_short_description or "unknown"}
Parent RITM description: {ticket.request_item_description or "unknown"}
Requested for email: {ticket.requested_for_email or "unknown"}
Opened by email: {ticket.opened_by_email or "unknown"}

Extract the report request fields."""

        result = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt, model=self.model, temperature=0.1)

        report_name = self._normalize_report_name(result.get("report_name"))
        frequency = self._normalize_choice(result.get("frequency"), KNOWN_FREQUENCIES, default="adhoc")
        date_range = self._normalize_choice(result.get("date_range"), KNOWN_DATE_RANGES, default=None)
        start_date = result.get("start_date") if isinstance(result.get("start_date"), str) else None
        end_date = result.get("end_date") if isinstance(result.get("end_date"), str) else None
        filters = self._clean_filters(result.get("filters"))
        output_format = self._normalize_choice(result.get("output_format"), {"excel"}, default="excel")
        recipient = result.get("recipient") if isinstance(result.get("recipient"), str) else None
        if not recipient:
            recipient = ticket.requested_for_email or ticket.opened_by_email

        insufficient = bool(result.get("insufficient_information", False))
        if report_name not in KNOWN_REPORTS or date_range is None:
            insufficient = True

        details = ReportRequestDetails(
            ticket_number=ticket.number,
            report_name=report_name if report_name in KNOWN_REPORTS else None,
            frequency=frequency,
            date_range=date_range,
            start_date=start_date,
            end_date=end_date,
            filters=filters,
            output_format=output_format,
            recipient=recipient.strip() if recipient else None,
            rationale=result.get("rationale", ""),
            insufficient_information=insufficient,
        )

        logger.info(
            "Parsed report request %s: report=%s frequency=%s date_range=%s insufficient=%s",
            ticket.number,
            details.report_name,
            details.frequency,
            details.date_range,
            details.insufficient_information,
        )
        return details

    @staticmethod
    def _normalize_report_name(value) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower().replace("-", "_").replace(" ", "_")

    @staticmethod
    def _normalize_choice(value, allowed, default=None):
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "last_week": "previous_week",
            "last_month": "previous_month",
            "on_demand": "adhoc",
            "ad_hoc": "adhoc",
        }
        normalized = aliases.get(normalized, normalized)
        return normalized if normalized in allowed else default

    @staticmethod
    def _clean_filters(value) -> dict:
        if not isinstance(value, dict):
            return {}
        filters = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if item is None:
                continue
            if isinstance(item, (str, int, float, bool)):
                filters[key.strip().lower()] = str(item).strip()
        return filters
