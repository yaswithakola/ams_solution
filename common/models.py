"""
Shared data models passed between agents.
"""
from datetime import date
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field


class Ticket(BaseModel):
    """Normalized representation of a ServiceNow record (incident or
    service catalog task/request item)."""

    sys_id: str
    number: str
    table: str  # "incident", "sc_task", "sc_req_item", or "sc_request"
    sys_class_name: Optional[str] = None
    short_description: str = ""
    description: str = ""
    cmdb_ci: Optional[str] = None
    cmdb_ci_name: Optional[str] = None
    assignment_group: Optional[str] = None
    request_item_sys_id: Optional[str] = None
    request_item_number: Optional[str] = None
    request_item_short_description: Optional[str] = None
    request_item_description: Optional[str] = None
    requested_for_email: Optional[str] = None
    opened_by_email: Optional[str] = None
    priority: Optional[str] = None
    state: Optional[str] = None
    raw: dict = Field(default_factory=dict)  # original ServiceNow payload

    def to_embedding_text(self) -> str:
        parts = [
            self.short_description or "",
            self.description or "",
            f"Configuration Item: {self.cmdb_ci_name}" if self.cmdb_ci_name else "",
        ]
        return "\n".join(p for p in parts if p).strip()


class SimilarIncident(BaseModel):
    """A historical incident retrieved from the vector database."""

    number: str
    short_description: str
    assignment_group: str
    close_notes: Optional[str] = None
    score: float


class RoutingResult(BaseModel):
    """Output of the Incident Router AI Agent."""

    ticket_number: str
    assignment_group: Optional[str] = None
    confidence: float
    rationale: str
    insufficient_information: bool = False
    similar_incidents: List[SimilarIncident] = Field(default_factory=list)


class JobFailureAssessment(BaseModel):
    """Output of the AMS Orchestrator's job-failure classification step."""

    ticket_number: str
    is_job_failure: bool
    confidence: float
    rationale: str
    job_name: Optional[str] = None


class ServiceRequestRoute(BaseModel):
    """Output of the service-request routing step."""

    ticket_number: str
    request_type: str
    confidence: float
    rationale: str
    insufficient_information: bool = False


class ReportRequestDetails(BaseModel):
    """Structured report request extracted from a service catalog ticket."""

    ticket_number: str
    report_name: Optional[str] = None
    frequency: str = "adhoc"
    date_range: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    filters: Dict[str, str] = Field(default_factory=dict)
    output_format: str = "excel"
    recipient: Optional[str] = None
    rationale: str = ""
    insufficient_information: bool = False


class ReportFilterDefinition(BaseModel):
    """Allowed input filter for one approved report."""

    name: str
    value_type: str = "string"
    required: bool = False
    allowed_values: Optional[List[str]] = None


class ReportDefinition(BaseModel):
    """One approved report entry from the local report catalog."""

    report_name: str
    title: str
    database: str = "postgres"
    sql_file: str
    default_recipient: Optional[str] = None
    allowed_filters: List[ReportFilterDefinition] = Field(default_factory=list)


class ResolvedReportDateRange(BaseModel):
    start_date: date
    end_date: date
    label: str


class ReportExecutionResult(BaseModel):
    ticket_number: str
    report_name: str
    status: str
    record_count: int = 0
    recipient: Optional[str] = None
    message: str = ""
    output_path: Optional[str] = None
    email_sent: bool = False


class RestartRequestDetails(BaseModel):
    """Structured restart/enable/disable request extracted from a catalog task."""

    ticket_number: str
    service: str = "glue"
    action: Optional[str] = None
    job_name: Optional[str] = None
    confidence: float = 0.0
    rationale: str = ""
    insufficient_information: bool = False


class RestartJobDefinition(BaseModel):
    """One approved job entry that the Restart Agent is allowed to operate on."""

    job_name: str
    service: str = "glue"
    trigger_name: Optional[str] = None
    allowed_actions: List[str] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    description: str = ""


class RestartExecutionResult(BaseModel):
    ticket_number: str
    status: str
    service: str = "glue"
    action: Optional[str] = None
    job_name: Optional[str] = None
    trigger_name: Optional[str] = None
    message: str = ""
    executed: bool = False


class SimilarJobFailure(BaseModel):
    """A historical job-failure record retrieved from the vector database."""

    number: str
    short_description: str
    action_taken: str
    outcome: Optional[str] = None
    score: float


class RemediationRecommendation(BaseModel):
    """Output of the Job Remediation AI Agent."""

    ticket_number: str
    job_name: Optional[str] = None
    action: Optional[str] = None            # must match one of the matched SOP's resolution_steps
    action_parameters: Dict[str, str] = Field(default_factory=dict)  # e.g. {"cluster_name": "...", "service_name": "..."}
    confidence: float = 0.0
    risk_level: Optional[str] = None        # "LOW" | "MEDIUM" | "HIGH", from the matched SOP
    rationale: str = ""
    requires_human_approval: bool = True    # LLM's own opinion; guardrails can still override this
    insufficient_information: bool = False
    sop_id: Optional[str] = None
    sop_title: Optional[str] = None
    similar_job_failures: List[SimilarJobFailure] = Field(default_factory=list)


class GuardrailResult(BaseModel):
    """Output of the guardrails validator - the safety gate before auto-execution."""

    passed: bool
    failed_checks: List[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    """Outcome of actually running a remediation action."""

    success: bool
    message: str
