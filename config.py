"""
Centralized configuration for the AMS Agentic AI Solution.

All values are loaded from environment variables (see .env.example).
Nothing sensitive is hard-coded - ServiceNow credentials and the
LLM credentials must be supplied via environment variables or a
.env file that you create locally (never commit real secrets).
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()  # loads a local .env file if present


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y")


@dataclass
class AnthropicSettings:
    api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model_orchestrator: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_ORCHESTRATOR", "claude-sonnet-4-5-20250929")
    )
    model_router: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_ROUTER", "claude-sonnet-4-5-20250929")
    )
    model_job_classifier: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_JOB_CLASSIFIER", "claude-sonnet-4-5-20250929")
    )
    model_service_request_router: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_SERVICE_REQUEST_ROUTER", "claude-sonnet-4-5-20250929")
    )
    model_report_request_parser: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_REPORT_REQUEST_PARSER", "claude-sonnet-4-5-20250929")
    )
    model_restart_request_parser: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL_RESTART_REQUEST_PARSER", "claude-sonnet-4-5-20250929")
    )
    max_tokens: int = field(default_factory=lambda: int(os.getenv("ANTHROPIC_MAX_TOKENS", "1024")))


@dataclass
class OllamaSettings:
    base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    model_orchestrator: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL_ORCHESTRATOR", os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    )
    model_router: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL_ROUTER", os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    )
    model_job_classifier: str = field(
        default_factory=lambda: os.getenv("OLLAMA_MODEL_JOB_CLASSIFIER", os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    )
    model_service_request_router: str = field(
        default_factory=lambda: os.getenv(
            "OLLAMA_MODEL_SERVICE_REQUEST_ROUTER", os.getenv("OLLAMA_MODEL", "qwen3:4b")
        )
    )
    model_report_request_parser: str = field(
        default_factory=lambda: os.getenv(
            "OLLAMA_MODEL_REPORT_REQUEST_PARSER", os.getenv("OLLAMA_MODEL", "qwen3:4b")
        )
    )
    model_restart_request_parser: str = field(
        default_factory=lambda: os.getenv(
            "OLLAMA_MODEL_RESTART_REQUEST_PARSER", os.getenv("OLLAMA_MODEL", "qwen3:4b")
        )
    )
    max_tokens: int = field(default_factory=lambda: int(os.getenv("OLLAMA_MAX_TOKENS", "512")))
    timeout_seconds: int = field(default_factory=lambda: int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300")))


@dataclass
class ServiceNowSettings:
    # PLACEHOLDERS - replace via environment variables / secrets manager.
    instance_url: str = field(default_factory=lambda: os.getenv("SERVICENOW_INSTANCE_URL", ""))
    username: str = field(default_factory=lambda: os.getenv("SERVICENOW_USERNAME", "REPLACE_ME_USERNAME"))
    password: str = field(default_factory=lambda: os.getenv("SERVICENOW_PASSWORD", "REPLACE_ME_PASSWORD"))
    verify_ssl: bool = field(default_factory=lambda: _bool("SERVICENOW_VERIFY_SSL", "true"))
    timeout_seconds: int = field(default_factory=lambda: int(os.getenv("SERVICENOW_TIMEOUT_SECONDS", "30")))
    incident_query_filter: str = field(
        default_factory=lambda: os.getenv(
            "INCIDENT_QUERY_FILTER", "active=true^assignment_group=^ORassignment_groupISEMPTY"
        )
    )
    service_request_query_filter: str = field(
        default_factory=lambda: os.getenv("SERVICE_REQUEST_QUERY_FILTER", "active=true")
    )
    service_request_task_query_filter: str = field(
        default_factory=lambda: os.getenv("SERVICE_REQUEST_TASK_QUERY_FILTER", "active=true")
    )
    service_request_task_closed_state: str = field(
        default_factory=lambda: os.getenv("SERVICE_REQUEST_TASK_CLOSED_STATE", "3")
    )


@dataclass
class QdrantSettings:
    url: str = field(default_factory=lambda: os.getenv("QDRANT_URL", "http://localhost:6333"))
    api_key: str = field(default_factory=lambda: os.getenv("QDRANT_API_KEY", "") or None)
    collection: str = field(default_factory=lambda: os.getenv("QDRANT_COLLECTION", "snow_incidents"))


@dataclass
class EmbeddingSettings:
    model_name: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"))
    dim: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIM", "1024")))
    use_fp16: bool = field(default_factory=lambda: _bool("EMBEDDING_USE_FP16", "true"))


@dataclass
class SmtpSettings:
    # Defaults point at Gmail's SMTP server. Gmail requires an "App
    # Password" for SMTP (not your normal login password) once 2FA is on:
    # https://myaccount.google.com/apppasswords
    host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", "smtp.gmail.com"))
    port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    use_tls: bool = field(default_factory=lambda: _bool("SMTP_USE_TLS", "true"))
    username: str = field(default_factory=lambda: os.getenv("SMTP_USERNAME", ""))  # your Gmail address
    password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))  # Gmail App Password
    from_addr: str = field(default_factory=lambda: os.getenv("SMTP_FROM", ""))     # usually same as SMTP_USERNAME
    to_human_review: str = field(
        default_factory=lambda: os.getenv("SMTP_TO_HUMAN_REVIEW", "")  # the gmail address to notify
    )


@dataclass
class SesSettings:
    region_name: str = field(default_factory=lambda: os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1")))
    source_email: str = field(default_factory=lambda: os.getenv("SES_SOURCE_EMAIL", ""))
    configuration_set: str = field(default_factory=lambda: os.getenv("SES_CONFIGURATION_SET", ""))


@dataclass
class ApprovalSettings:
    """
    Config for the human-approval-via-email-link flow. When the Incident
    Router AI Agent's confidence is below threshold, the orchestrator
    generates a one-time approval token, emails a link containing it, and
    a small local web server (approval_server.py) listens for the click
    to actually apply the L2 assignment in ServiceNow.
    """
    base_url: str = field(default_factory=lambda: os.getenv("APPROVAL_BASE_URL", "http://localhost:8000"))
    host: str = field(default_factory=lambda: os.getenv("APPROVAL_SERVER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("APPROVAL_SERVER_PORT", "8000")))
    token_ttl_hours: int = field(default_factory=lambda: int(os.getenv("APPROVAL_TOKEN_TTL_HOURS", "72")))


@dataclass
class RemediationSettings:
    """
    Config for the Job Remediation AI Agent's guardrails - modeled on the
    SOP + guardrails pattern (SOP exists, action is in the SOP's allowed
    steps, auto_resolvable, risk_level, blocked actions, confidence,
    LLM-flagged human approval) so remediation only auto-executes when
    every safety check passes; otherwise it goes through the same
    approval-link email flow as low-confidence routing.
    """
    confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv("REMEDIATION_CONFIDENCE_THRESHOLD", "0.65"))
    )
    allowed_risk_levels: tuple = field(
        default_factory=lambda: tuple(
            x.strip().upper() for x in os.getenv("REMEDIATION_ALLOWED_RISK_LEVELS", "LOW,MEDIUM").split(",") if x.strip()
        )
    )
    blocked_actions: tuple = field(
        default_factory=lambda: tuple(
            x.strip() for x in os.getenv("REMEDIATION_BLOCKED_ACTIONS", "").split(",") if x.strip()
        )
    )
    sop_dir: str = field(default_factory=lambda: os.getenv("SOP_DOCUMENTS_DIR", "sop_documents"))
    reindex_sops_on_startup: bool = field(default_factory=lambda: _bool("SOP_REINDEX_ON_STARTUP", "false"))


@dataclass
class CloudWatchSettings:
    """
    Config for the Job Remediation AI Agent's CloudWatch Logs lookup -
    the second grounding source alongside SOPs (see
    agents/job_remediation_agent.py). Requires logs:DescribeLogGroups and
    logs:FilterLogEvents IAM permissions - see the README's AWS
    Prerequisites section.
    """
    lookback_minutes: int = field(default_factory=lambda: int(os.getenv("CLOUDWATCH_LOOKBACK_MINUTES", "30")))
    max_log_events: int = field(default_factory=lambda: int(os.getenv("CLOUDWATCH_MAX_LOG_EVENTS", "50")))
    filter_pattern: str = field(
        default_factory=lambda: os.getenv("CLOUDWATCH_FILTER_PATTERN", "?ERROR ?Exception ?FAILED ?Failed")
    )
    enabled: bool = field(default_factory=lambda: _bool("CLOUDWATCH_ENABLED", "true"))


@dataclass
class S3Settings:
    """
    Config for the Job Remediation AI Agent's S3-based grounding lookups -
    for jobs diagnosed from S3 log/data buckets rather than CloudWatch
    (e.g. SOP-EAM-MMSO-701's "Enrollments MMS job"), and the
    move_keyword_files_to_input remediation action. Requires
    s3:ListBucket, s3:GetObject, s3:CopyObject, s3:DeleteObject IAM
    permissions - see the README's AWS Prerequisites section.
    """
    max_files: int = field(default_factory=lambda: int(os.getenv("S3_MAX_FILES", "20")))
    max_log_bytes: int = field(default_factory=lambda: int(os.getenv("S3_MAX_LOG_BYTES", "20000")))
    enabled: bool = field(default_factory=lambda: _bool("S3_ENABLED", "true"))


@dataclass
class PostgresSettings:
    """
    Config for the shared PostgreSQL database backing common/audit_store.py
    (the remediation audit trail) and common/approval_store.py (human
    approval tokens) - one Postgres instance/database, two tables, mirroring
    how one shared Qdrant collection backs multiple sources via a `source`
    tag. Run locally the same way as Qdrant, e.g.:
        docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ams_agentic postgres
    """
    url: str = field(
        default_factory=lambda: os.getenv(
            "POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ams_agentic"
        )
    )


@dataclass
class ReportSettings:
    catalog_path: str = field(default_factory=lambda: os.getenv("REPORT_CATALOG_PATH", "reporting/reports.json"))
    sql_dir: str = field(default_factory=lambda: os.getenv("REPORT_SQL_DIR", "reporting/sql"))
    output_dir: str = field(default_factory=lambda: os.getenv("REPORT_OUTPUT_DIR", "output"))
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "REPORT_POSTGRES_URL",
            os.getenv("POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/ams_agentic"),
        )
    )


@dataclass
class RestartSettings:
    catalog_path: str = field(default_factory=lambda: os.getenv("RESTART_JOB_CATALOG_PATH", "restart/jobs.json"))


@dataclass
class Settings:
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama"))
    anthropic: AnthropicSettings = field(default_factory=AnthropicSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    servicenow: ServiceNowSettings = field(default_factory=ServiceNowSettings)
    qdrant: QdrantSettings = field(default_factory=QdrantSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    ses: SesSettings = field(default_factory=SesSettings)
    approval: ApprovalSettings = field(default_factory=ApprovalSettings)
    remediation: RemediationSettings = field(default_factory=RemediationSettings)
    cloudwatch: CloudWatchSettings = field(default_factory=CloudWatchSettings)
    s3: S3Settings = field(default_factory=S3Settings)
    postgres: PostgresSettings = field(default_factory=PostgresSettings)
    reports: ReportSettings = field(default_factory=ReportSettings)
    restart: RestartSettings = field(default_factory=RestartSettings)

    confidence_threshold: float = field(default_factory=lambda: float(os.getenv("CONFIDENCE_THRESHOLD", "0.65")))
    top_k_similar_incidents: int = field(default_factory=lambda: int(os.getenv("TOP_K_SIMILAR_INCIDENTS", "5")))
    poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "60")))


def get_settings() -> Settings:
    return Settings()
