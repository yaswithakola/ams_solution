"""
Entry point for the AMS Agentic AI Solution.

Wires together:
    - ServiceNowClient      (common/servicenow_client.py)
    - LLM client            (common/llm_factory.py)    - Ollama locally or Claude later
    - CloudWatchLogsClient  (common/cloudwatch_client.py) - log-based grounding
    - S3Client              (common/s3_client.py)      - S3-based grounding (e.g. SOP-EAM-MMSO-701)
    - VectorStore           (common/vector_db.py)      - BGE-M3 + Qdrant, shared
    - ApprovalStore         (common/approval_store.py) - approval tokens (routing + remediation)
    - AuditStore            (common/audit_store.py)    - remediation audit trail
    - SOPStore              (common/sop_store.py)      - SOP documents for remediation
    - GuardrailsValidator   (common/guardrails.py)     - safety gate before auto-remediation
    - RemediationExecutor   (common/remediation_executor.py) - actually runs remediation actions
    - IncidentRouterAgent   (agents/incident_router_agent.py)
    - JobRemediationAgent   (agents/job_remediation_agent.py)
    - AMSOrchestratorAgent  (agents/ams_orchestrator_agent.py) [entry point]

Usage:
    python main.py --once        # single poll-and-process pass
    python main.py                # continuous polling loop

NOTE: also run `python approval_server.py` alongside this (separate
process) so the Approve/Reject links in emails actually work.
"""
import argparse
import logging

from agents.ams_orchestrator_agent import AMSOrchestratorAgent
from agents.incident_router_agent import IncidentRouterAgent
from agents.job_remediation_agent import JobRemediationAgent
from agents.report_generation_agent import (
    ExcelReportGenerator,
    PostgresReportClient,
    ReportCatalog,
    ReportGenerationAgent,
    ReportService,
)
from agents.restart_agent import RestartAgent, RestartJobCatalog, RestartService
from agents.service_request_router_agent import ServiceRequestRouterAgent
from common.approval_store import ApprovalStore
from common.audit_store import AuditStore
from common.cloudwatch_client import CloudWatchLogsClient
from common.guardrails import GuardrailsValidator
from common.llm_factory import build_llm_client
from common.remediation_executor import RemediationExecutor
from common.s3_client import S3Client
from common.servicenow_client import ServiceNowClient
from common.sop_store import SOPStore
from common.vector_db import VectorStore
from config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def build_orchestrator() -> AMSOrchestratorAgent:
    settings = get_settings()

    # --- ServiceNow: PLACEHOLDER credentials come from config/.env ------
    servicenow_client = ServiceNowClient(settings.servicenow)

    # --- LLM client, shared/instantiated once. Defaults to local Ollama;
    # set LLM_PROVIDER=anthropic later to test Claude/Sonnet. -----------
    llm_client, llm_model_settings = build_llm_client(settings)
    logger.info("Using %s LLM provider", settings.llm_provider)

    # --- Shared vector database (BGE-M3 + Qdrant) -----------------------
    vector_store = VectorStore(settings.qdrant, settings.embedding)
    vector_store.ensure_collection()  # no-op if it already exists

    # --- SOP library for the Job Remediation AI Agent --------------------
    sop_store = SOPStore(settings.remediation.sop_dir, vector_store=vector_store)
    # Optional RAG indexing - keyword match (sop_store.match()) still works
    # without it. Off by default so a plain startup doesn't force-load the
    # (otherwise lazy) embedding model just to re-embed unchanged SOPs every
    # run. Run `python -m ingestion.ingest_sops` after editing/adding SOPs,
    # or set SOP_REINDEX_ON_STARTUP=true to always reindex here instead.
    if settings.remediation.reindex_sops_on_startup:
        sop_store.index_into_vector_db()

    # --- Approval token store + audit trail (PostgreSQL, shared with approval_server.py)
    approval_store = ApprovalStore(settings.postgres.url)
    audit_store = AuditStore(settings.postgres.url)

    # --- Guardrails + executor for remediation actions --------------------
    guardrails = GuardrailsValidator(settings.remediation)
    remediation_executor = RemediationExecutor()

    # --- CloudWatch Logs (second grounding source for the Job Remediation
    # AI Agent, alongside SOPs) - disabled entirely via CLOUDWATCH_ENABLED
    # if you don't want it to attempt AWS log lookups at all. -------------
    cloudwatch_client = CloudWatchLogsClient(settings.cloudwatch) if settings.cloudwatch.enabled else None

    # --- S3 (grounding source for jobs diagnosed from S3 rather than
    # CloudWatch, e.g. SOP-EAM-MMSO-701's Enrollments MMS job) - disabled
    # entirely via S3_ENABLED if you don't want it to attempt AWS S3
    # lookups at all. ------------------------------------------------------
    s3_client = S3Client(settings.s3) if settings.s3.enabled else None

    # --- Agents ----------------------------------------------------------
    incident_router_agent = IncidentRouterAgent(
        vector_store=vector_store,
        llm_client=llm_client,
        anthropic_settings=llm_model_settings,
        top_k=settings.top_k_similar_incidents,
    )
    job_remediation_agent = JobRemediationAgent(
        vector_store=vector_store,
        llm_client=llm_client,
        anthropic_settings=llm_model_settings,
        sop_store=sop_store,
        cloudwatch_client=cloudwatch_client,
        s3_client=s3_client,
        top_k=settings.top_k_similar_incidents,
    )
    service_request_router_agent = ServiceRequestRouterAgent(
        llm_client=llm_client,
        anthropic_settings=llm_model_settings,
    )
    report_generation_agent = ReportGenerationAgent(
        llm_client=llm_client,
        anthropic_settings=llm_model_settings,
    )
    restart_agent = RestartAgent(
        llm_client=llm_client,
        anthropic_settings=llm_model_settings,
    )
    report_catalog = ReportCatalog(
        catalog_path=settings.reports.catalog_path,
        sql_dir=settings.reports.sql_dir,
    )
    report_service = ReportService(
        catalog=report_catalog,
        database_client=PostgresReportClient(settings.reports.database_url),
        excel_generator=ExcelReportGenerator(settings.reports.output_dir),
        ses_settings=settings.ses,
    )
    restart_service = RestartService(
        catalog=RestartJobCatalog(settings.restart.catalog_path),
    )

    orchestrator = AMSOrchestratorAgent(
        settings=settings,
        servicenow_client=servicenow_client,
        incident_router_agent=incident_router_agent,
        job_remediation_agent=job_remediation_agent,
        llm_client=llm_client,
        approval_store=approval_store,
        vector_store=vector_store,
        sop_store=sop_store,
        guardrails=guardrails,
        remediation_executor=remediation_executor,
        audit_store=audit_store,
        service_request_router_agent=service_request_router_agent,
        report_generation_agent=report_generation_agent,
        report_service=report_service,
        restart_agent=restart_agent,
        restart_service=restart_service,
        llm_model_settings=llm_model_settings,
    )
    return orchestrator


def main():
    parser = argparse.ArgumentParser(description="AMS Agentic AI Solution")
    parser.add_argument("--once", action="store_true", help="Run a single fetch-and-process pass, then exit.")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval in seconds.")
    args = parser.parse_args()

    orchestrator = build_orchestrator()

    if args.once:
        orchestrator.run_once()
    else:
        orchestrator.run_loop(poll_interval_seconds=args.interval)


if __name__ == "__main__":
    main()
