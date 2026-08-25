"""
Local entry point for testing report service requests without ServiceNow.
"""
import argparse
import logging

from agents.report_generation_agent import ReportGenerationAgent
from common.llm_factory import build_llm_client
from common.models import Ticket
from common.report_catalog import ReportCatalog
from common.report_database import PostgresReportClient
from common.report_excel import ExcelReportGenerator
from common.report_service import ReportService
from config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Run one local report request without ServiceNow.")
    parser.add_argument("request", nargs="*", help="Natural-language report request.")
    args = parser.parse_args()

    request_text = " ".join(args.request).strip()
    if not request_text:
        request_text = input("Report request: ").strip()

    settings = get_settings()
    llm_client, llm_model_settings = build_llm_client(settings)
    parser_agent = ReportGenerationAgent(llm_client=llm_client, anthropic_settings=llm_model_settings)
    report_service = ReportService(
        catalog=ReportCatalog(settings.reports.catalog_path, settings.reports.sql_dir),
        database_client=PostgresReportClient(settings.reports.database_url),
        excel_generator=ExcelReportGenerator(settings.reports.output_dir),
        ses_settings=settings.ses,
    )

    ticket = Ticket(
        sys_id="local-report-request",
        number="LOCAL-REPORT",
        table="local",
        short_description=request_text,
        description=request_text,
    )
    details = parser_agent.parse(ticket)
    print(f"Parsed report request: {details.model_dump()}")

    if details.insufficient_information:
        print(f"FAILED: {details.rationale or 'The report request is missing required information.'}")
        return

    result = report_service.run(details)
    print(
        f"{result.status}: report={result.report_name}, records={result.record_count}, "
        f"recipient={result.recipient}, output={result.output_path}"
    )
    if result.message:
        print(result.message)


if __name__ == "__main__":
    main()
