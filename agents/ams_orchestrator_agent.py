"""
AMS Orchestrator AI Agent
=========================
Input : ServiceNow incidents + ServiceNow request items.
Process:
    1. Classify each fetched record as an incident or a service request.
       Incidents are handed off to the Incident Router AI Agent.
    2. Receive back the suggested L2 assignment group + confidence score.
    3. If confidence is below threshold -> email a human for review.
       Otherwise -> update the L2 assignment group on the incident via
       the ServiceNow REST API, then feed the confirmed routing back into
       the vector DB for continuous learning.
    4. After the assignment group update, send the incident details to
       an Anthropic LLM model to determine whether this is a job-failure
       incident.
    5. If it is a job failure -> call the Job Remediation AI Agent to get
       a recommended action, run it through the guardrails validator, and
       either auto-execute it (RemediationExecutor) or send it to a human
       for approval via the same approve-link email pattern used for
       low-confidence routing. Every outcome is written to the audit
       trail and, once confirmed, fed back into the vector DB.
    6. Requested items (sc_req_item / RITM) are routed through the
       Service Request Router. If matching catalog tasks (sc_task) exist
       for the same RITM, the result comment is mirrored there and the
       task is closed after successful fulfillment.

Configure: connects to ServiceNow via common.servicenow_client, using
credentials from config.ServiceNowSettings (placeholders - replace with
real values via environment variables).
"""
import logging
import time
from typing import Optional, TYPE_CHECKING

from common.approval_store import ApprovalStore
from common.audit_store import AuditStore
from common.email_utils import (
    send_human_review_email,
    send_insufficient_information_email,
    send_remediation_approval_email,
)
from common.guardrails import GuardrailsValidator
from common.models import JobFailureAssessment, Ticket
from common.remediation_executor import RemediationExecutor
from common.servicenow_client import ServiceNowClient
from common.sop_store import SOPStore
from common.vector_db import VectorStore
from config import Settings

from agents.incident_router_agent import IncidentRouterAgent
from agents.job_remediation_agent import JobRemediationAgent
from agents.report_generation_agent import ReportGenerationAgent
from agents.restart_agent import RestartAgent
from agents.service_request_router_agent import (
    REQUEST_REPORT_GENERATION,
    REQUEST_RESTART,
    ServiceRequestRouterAgent,
)

if TYPE_CHECKING:
    from common.llm_base import AgentLLMClient
    from common.report_service import ReportService
    from common.restart_service import RestartService

logger = logging.getLogger(__name__)

TICKET_TYPE_SYSTEM_PROMPT = """You classify a ServiceNow record as either "incident" or \
"service_request". Use the sys_class_name / table hints given, and the text content, to decide. \
Return strict JSON with keys: ticket_type ("incident" or "service_request"), rationale."""

JOB_FAILURE_SYSTEM_PROMPT = """You are a production-support classifier. Given an incident's \
short description and description, decide whether this incident describes a BATCH JOB / \
SCHEDULED JOB / ETL JOB failure (e.g. a cron job, Control-M job, Autosys job, scheduled task, \
batch process, or similar automated job erroring out, failing, or not completing). \
Return strict JSON with keys: is_job_failure (true/false), confidence (0.0-1.0), rationale, \
and job_name (best-guess job name if mentioned, else null)."""


def _humanize_report_name(report_name: Optional[str]) -> str:
    if not report_name:
        return "report"
    return report_name.replace("_", " ")


def _format_restart_success_message(action: Optional[str], job_name: Optional[str], trigger_name: Optional[str]) -> str:
    action = (action or "").lower()
    if action == "enable":
        if trigger_name:
            return f"Enabled the Glue trigger {trigger_name} for job {job_name}."
        return f"Enabled the Glue job {job_name}."
    if action == "disable":
        if trigger_name:
            return f"Disabled the Glue trigger {trigger_name} for job {job_name}."
        return f"Disabled the Glue job {job_name}."
    if action == "restart":
        return f"Restarted the Glue job {job_name}."
    return f"Completed the requested action for {job_name}."


class AMSOrchestratorAgent:
    def __init__(
        self,
        settings: Settings,
        servicenow_client: ServiceNowClient,
        incident_router_agent: IncidentRouterAgent,
        job_remediation_agent: JobRemediationAgent,
        llm_client: "AgentLLMClient",
        approval_store: ApprovalStore,
        vector_store: VectorStore,
        sop_store: SOPStore,
        guardrails: GuardrailsValidator,
        remediation_executor: RemediationExecutor,
        audit_store: AuditStore,
        service_request_router_agent: Optional[ServiceRequestRouterAgent] = None,
        report_generation_agent: Optional[ReportGenerationAgent] = None,
        report_service: Optional["ReportService"] = None,
        restart_agent: Optional[RestartAgent] = None,
        restart_service: Optional["RestartService"] = None,
        llm_model_settings=None,
    ):
        self.settings = settings
        self.servicenow = servicenow_client
        self.incident_router = incident_router_agent
        self.job_remediation = job_remediation_agent
        self.llm_client = llm_client
        self.approval_store = approval_store
        self.vector_store = vector_store
        self.sop_store = sop_store
        self.guardrails = guardrails
        self.remediation_executor = remediation_executor
        self.audit_store = audit_store
        self.service_request_router = service_request_router_agent
        self.report_generation = report_generation_agent
        self.report_service = report_service
        self.restart_agent = restart_agent
        self.restart_service = restart_service
        self.model = (llm_model_settings or settings.anthropic).model_orchestrator
        self._processed_service_requests = set()

    # ------------------------------------------------------------------
    # Ticket-type classification
    # ------------------------------------------------------------------
    def classify_ticket_type(self, ticket: Ticket) -> str:
        """
        Fast path: ServiceNow already tells us the table / sys_class_name,
        so we trust that first. We only fall back to the LLM for
        ambiguous / custom table cases.
        """
        if ticket.table == "incident" or ticket.sys_class_name == "incident":
            return "incident"
        if ticket.table in ("sc_task", "sc_req_item", "sc_request") or (ticket.sys_class_name or "").startswith("sc_"):
            return "service_request"

        user_prompt = (
            f"table: {ticket.table}\n"
            f"sys_class_name: {ticket.sys_class_name}\n"
            f"short_description: {ticket.short_description}\n"
            f"description: {ticket.description}\n"
        )
        result = self.llm_client.complete_json(TICKET_TYPE_SYSTEM_PROMPT, user_prompt, model=self.model)
        return result.get("ticket_type", "incident")

    # ------------------------------------------------------------------
    # Job-failure classification (post L2-assignment step)
    # ------------------------------------------------------------------
    def assess_job_failure(self, ticket: Ticket) -> JobFailureAssessment:
        user_prompt = (
            f"short_description: {ticket.short_description}\n"
            f"description: {ticket.description}\n"
        )
        result = self.llm_client.complete_json(JOB_FAILURE_SYSTEM_PROMPT, user_prompt, model=self.model)
        return JobFailureAssessment(
            ticket_number=ticket.number,
            is_job_failure=bool(result.get("is_job_failure", False)),
            confidence=float(result.get("confidence", 0.0)),
            rationale=result.get("rationale", ""),
            job_name=result.get("job_name"),
        )

    # ------------------------------------------------------------------
    # Job remediation pipeline: recommend -> guardrails -> execute or approve
    # ------------------------------------------------------------------
    def handle_job_remediation(self, ticket: Ticket, job_assessment: JobFailureAssessment) -> None:
        recommendation = self.job_remediation.get_remediation(ticket)
        sop = self.sop_store.get(recommendation.sop_id) if recommendation.sop_id else None
        guardrail_result = self.guardrails.validate(recommendation, sop)

        if guardrail_result.passed:
            # All safety checks passed - auto-execute.
            exec_result = self.remediation_executor.execute(ticket, recommendation)
            note = (
                f"[AMS AI] Job Remediation AI Agent auto-executed action '{recommendation.action}' "
                f"(confidence={recommendation.confidence:.2f}, sop={recommendation.sop_id}). "
                f"Result: {'SUCCESS' if exec_result.success else 'FAILED'} - {exec_result.message}"
            )
            try:
                self.servicenow.add_work_note(ticket.sys_id, note, table=ticket.table)
            except Exception:
                logger.exception("Failed to add auto-remediation work note for %s", ticket.number)

            self.audit_store.log(
                ticket_number=ticket.number, actor="ai_auto",
                result="success" if exec_result.success else "failed", message=exec_result.message,
                job_name=recommendation.job_name, action=recommendation.action,
                sop_id=recommendation.sop_id, risk_level=recommendation.risk_level,
                confidence=recommendation.confidence,
            )

            if exec_result.success:
                try:
                    self.vector_store.upsert_job_remediation_feedback(
                        number=ticket.number,
                        short_description=ticket.short_description,
                        description=ticket.description,
                        cmdb_ci_name=ticket.cmdb_ci_name,
                        action_taken=recommendation.action,
                        outcome="success (auto-executed)",
                    )
                except Exception:
                    logger.exception("Failed to feed remediation outcome for %s into vector DB", ticket.number)
            return

        # Guardrails failed - route to a human via approve/reject email.
        logger.warning(
            "Guardrails failed for %s remediation (%s): %s",
            ticket.number, recommendation.action, guardrail_result.failed_checks,
        )
        token = self.approval_store.create_approval(
            kind="remediation",
            ticket_number=ticket.number,
            sys_id=ticket.sys_id,
            table=ticket.table,
            confidence=recommendation.confidence,
            rationale=recommendation.rationale,
            action=recommendation.action,
            action_parameters=recommendation.action_parameters,
            job_name=recommendation.job_name or job_assessment.job_name,
            sop_id=recommendation.sop_id,
            risk_level=recommendation.risk_level,
            short_description=ticket.short_description,
            description=ticket.description,
            cmdb_ci_name=ticket.cmdb_ci_name,
            ttl_hours=self.settings.approval.token_ttl_hours,
        )
        approve_url = f"{self.settings.approval.base_url}/approve?token={token}"
        reject_url = f"{self.settings.approval.base_url}/reject?token={token}"

        send_remediation_approval_email(
            self.settings.smtp,
            ticket_number=ticket.number,
            short_description=ticket.short_description,
            job_name=recommendation.job_name or job_assessment.job_name or "unknown",
            action=recommendation.action or "no action recommended",
            action_parameters=recommendation.action_parameters,
            risk_level=recommendation.risk_level or "UNKNOWN",
            confidence=recommendation.confidence,
            rationale=recommendation.rationale,
            failed_checks=guardrail_result.failed_checks,
            approve_url=approve_url,
            reject_url=reject_url,
        )
        try:
            self.servicenow.add_work_note(
                ticket.sys_id,
                f"[AMS AI] Job Remediation AI Agent proposed action '{recommendation.action}' but it "
                f"needs human approval (failed checks: {guardrail_result.failed_checks}). "
                f"Approval link emailed.",
                table=ticket.table,
            )
        except Exception:
            logger.exception("Failed to add remediation-approval work note for %s", ticket.number)

        self.audit_store.log(
            ticket_number=ticket.number, actor="ai_auto", result="pending_approval",
            message=f"Failed checks: {guardrail_result.failed_checks}",
            job_name=recommendation.job_name, action=recommendation.action,
            sop_id=recommendation.sop_id, risk_level=recommendation.risk_level,
            confidence=recommendation.confidence,
        )

    # ------------------------------------------------------------------
    # Service-request pipeline. The RITM is the primary request record.
    # Matching catalog tasks are optional follower records that receive
    # the same human-readable comment, and are closed after success.
    # ------------------------------------------------------------------
    def handle_service_request(self, ticket: Ticket) -> None:
        if self.service_request_router is None:
            logger.info("Ticket %s is a service request, but no service-request router is configured.", ticket.number)
            return

        route = self.service_request_router.route(ticket)
        logger.info(
            "Ticket %s service request route=%s confidence=%.2f",
            ticket.number,
            route.request_type,
            route.confidence,
        )

        if route.request_type == REQUEST_REPORT_GENERATION:
            self.handle_report_generation_request(ticket)
            return

        if route.request_type == REQUEST_RESTART:
            self.handle_restart_request(ticket)
            return

        self._add_service_request_comment(
            ticket,
            f"Could not match this service request to a supported automation path. {route.rationale}",
        )

    def handle_report_generation_request(self, ticket: Ticket) -> None:
        if self.report_generation is None:
            logger.info("Ticket %s is a report request, but no report-generation agent is configured.", ticket.number)
            return

        details = self.report_generation.parse(ticket)
        if details.insufficient_information:
            self._add_service_request_comment(
                ticket,
                "Could not process the report request because some details were missing. "
                f"{details.rationale or 'Please provide the report name and date range.'}",
            )
            return

        if self.report_service is None:
            self._add_service_request_comment(
                ticket,
                f"Understood the request for the {_humanize_report_name(details.report_name)} report, "
                "but report execution is not configured in this environment yet.",
            )
            return

        result = self.report_service.run(details)
        if result.status == "FAILED":
            self._add_service_request_comment(
                ticket,
                f"Could not generate the {_humanize_report_name(result.report_name)} report. {result.message}",
            )
            return

        if result.email_sent and result.recipient:
            note = f"Generated the {_humanize_report_name(result.report_name)} report and emailed it to {result.recipient}."
        elif result.output_path:
            note = (
                f"Generated the {_humanize_report_name(result.report_name)} report. "
                "The file is available in the local output folder."
            )
        else:
            note = f"Generated the {_humanize_report_name(result.report_name)} report."
        self._add_service_request_comment(ticket, note, close_related_tasks=True)

        logger.info("RITM %s processed; any related catalog tasks were updated and closed.", ticket.number)

    def handle_restart_request(self, ticket: Ticket) -> None:
        if self.restart_agent is None:
            self._add_service_request_comment(ticket, "Restart automation is not configured in this environment.")
            return

        details = self.restart_agent.parse(ticket)
        if details.insufficient_information:
            self._add_service_request_comment(
                ticket,
                "Could not process the restart request because some details were missing. "
                f"{details.rationale or 'Please provide the action and job name.'}",
            )
            return

        if self.restart_service is None:
            self._add_service_request_comment(
                ticket,
                f"Understood the request for {details.job_name}, but restart execution is not configured in this environment yet.",
            )
            return

        result = self.restart_service.run(details)
        if result.status == "FAILED":
            self._add_service_request_comment(
                ticket,
                f"Could not complete the request for {result.job_name}. {result.message}",
            )
            return

        self._add_service_request_comment(
            ticket,
            _format_restart_success_message(result.action, result.job_name, result.trigger_name),
            close_related_tasks=True,
        )

        logger.info("RITM %s processed by Restart Agent; any related catalog tasks were updated and closed.", ticket.number)

    def _add_service_request_comment(self, ticket: Ticket, comment: str, close_related_tasks: bool = False) -> None:
        try:
            self.servicenow.add_comment(ticket.sys_id, comment, table=ticket.table)
        except Exception:
            logger.exception("Failed to add service-request comment for %s", ticket.number)

        if ticket.table != "sc_req_item":
            return

        try:
            related_tasks = self.servicenow.get_catalog_tasks_for_request_item(ticket.sys_id)
        except Exception:
            logger.exception("Failed to fetch related catalog tasks for %s", ticket.number)
            return

        for task in related_tasks:
            try:
                self.servicenow.add_comment(task.sys_id, comment, table=task.table)
            except Exception:
                logger.exception("Failed to mirror service-request comment to %s", task.number)
                continue

            if not close_related_tasks:
                continue

            try:
                self.servicenow.close_catalog_task(
                    task.sys_id,
                    close_state=self.settings.servicenow.service_request_task_closed_state,
                )
            except Exception:
                logger.exception("Failed to close related catalog task %s", task.number)

    # ------------------------------------------------------------------
    # Per-ticket pipeline
    # ------------------------------------------------------------------
    def process_ticket(self, ticket: Ticket) -> None:
        ticket_type = self.classify_ticket_type(ticket)
        logger.info("Ticket %s classified as %s", ticket.number, ticket_type)

        if ticket_type != "incident":
            service_request_key = ticket.sys_id or ticket.number
            if service_request_key in self._processed_service_requests:
                logger.info("Skipping service request %s because it was already processed in this run.", ticket.number)
                return
            self.handle_service_request(ticket)
            self._processed_service_requests.add(service_request_key)
            return

        # 1. Route the incident to get the suggested L2 assignment group
        routing_result = self.incident_router.route(ticket)

        # 2a. The router explicitly said it doesn't have enough information -
        # this is not a "low confidence guess", there's no group to approve.
        if routing_result.insufficient_information or not routing_result.assignment_group:
            logger.warning(
                "Incident Router AI Agent has insufficient information for %s: %s",
                ticket.number, routing_result.rationale,
            )
            send_insufficient_information_email(
                self.settings.smtp,
                ticket_number=ticket.number,
                short_description=ticket.short_description,
                rationale=routing_result.rationale,
            )
            try:
                self.servicenow.add_work_note(
                    ticket.sys_id,
                    f"[AMS AI] Incident Router AI Agent reported insufficient information to route "
                    f"this incident ({routing_result.rationale}). Needs manual L2 assignment.",
                    table=ticket.table,
                )
            except Exception:
                logger.exception("Failed to add insufficient-information work note for %s", ticket.number)
            return

        # 2b. Confidence gate: a candidate group exists but confidence is low ->
        # human approval-link email.
        if routing_result.confidence < self.settings.confidence_threshold:
            logger.warning(
                "Low confidence (%.2f) routing %s -> %s; emailing approval link to human.",
                routing_result.confidence, ticket.number, routing_result.assignment_group,
            )
            token = self.approval_store.create_approval(
                kind="routing",
                ticket_number=ticket.number,
                sys_id=ticket.sys_id,
                table=ticket.table,
                assignment_group=routing_result.assignment_group,
                confidence=routing_result.confidence,
                rationale=routing_result.rationale,
                short_description=ticket.short_description,
                description=ticket.description,
                cmdb_ci_name=ticket.cmdb_ci_name,
                ttl_hours=self.settings.approval.token_ttl_hours,
            )
            approve_url = f"{self.settings.approval.base_url}/approve?token={token}"

            send_human_review_email(
                self.settings.smtp,
                ticket_number=ticket.number,
                short_description=ticket.short_description,
                suggested_group=routing_result.assignment_group,
                confidence=routing_result.confidence,
                rationale=routing_result.rationale,
                approve_url=approve_url,
            )
            try:
                self.servicenow.add_work_note(
                    ticket.sys_id,
                    f"[AMS AI] Low-confidence L2 suggestion: {routing_result.assignment_group} "
                    f"(confidence={routing_result.confidence:.2f}). Approval link emailed for human review.",
                    table=ticket.table,
                )
            except Exception:
                logger.exception("Failed to add low-confidence work note for %s", ticket.number)
            return  # do not auto-assign when confidence is too low

        # 3. Confidence sufficient -> update L2 assignment group in ServiceNow
        try:
            self.servicenow.update_assignment_group(
                ticket.sys_id, routing_result.assignment_group, table=ticket.table
            )
            self.servicenow.add_work_note(
                ticket.sys_id,
                f"[AMS AI] Auto-assigned to L2 group '{routing_result.assignment_group}' "
                f"(confidence={routing_result.confidence:.2f}). Rationale: {routing_result.rationale}",
                table=ticket.table,
            )
        except Exception:
            logger.exception("Failed to update assignment group for %s", ticket.number)
            return

        # 3b. Continuous learning: feed this confirmed routing decision back
        # into the shared vector DB.
        try:
            self.vector_store.upsert_routing_feedback(
                number=ticket.number,
                short_description=ticket.short_description,
                description=ticket.description,
                cmdb_ci_name=ticket.cmdb_ci_name,
                assignment_group=routing_result.assignment_group,
            )
        except Exception:
            logger.exception("Failed to feed routing decision for %s back into the vector DB", ticket.number)

        # 4. After L2 assignment, ask the LLM whether this is a job failure
        job_assessment = self.assess_job_failure(ticket)
        logger.info(
            "Job-failure assessment for %s: is_job_failure=%s confidence=%.2f",
            ticket.number, job_assessment.is_job_failure, job_assessment.confidence,
        )

        # 5. Job Remediation AI Agent: recommend -> guardrails -> execute or approve
        if job_assessment.is_job_failure:
            self.handle_job_remediation(ticket, job_assessment)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        incidents = self.servicenow.get_new_incidents()
        service_requests = self.servicenow.get_new_service_requests()
        logger.info("Fetched %d incidents and %d request items", len(incidents), len(service_requests))

        for ticket in incidents + service_requests:
            try:
                self.process_ticket(ticket)
            except Exception:
                logger.exception("Unhandled error processing ticket %s", ticket.number)

    def run_loop(self, poll_interval_seconds: int = None) -> None:
        interval = poll_interval_seconds or self.settings.poll_interval_seconds
        logger.info("Starting AMS Orchestrator run loop (interval=%ss)", interval)
        while True:
            self.run_once()
            time.sleep(interval)
