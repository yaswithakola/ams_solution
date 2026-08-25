"""
Runs approved report requests after the LLM has extracted intent.
"""
import logging

from common.models import ReportExecutionResult, ReportRequestDetails
from common.report_catalog import ReportCatalog
from common.report_date_resolver import ReportDateResolver
from common.report_excel import ExcelReportGenerator
from common.report_sql import build_report_parameters

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(
        self,
        catalog: ReportCatalog,
        database_client,
        date_resolver: ReportDateResolver = None,
        excel_generator: ExcelReportGenerator = None,
        ses_settings=None,
        email_sender=None,
    ):
        self.catalog = catalog
        self.database_client = database_client
        self.date_resolver = date_resolver or ReportDateResolver()
        self.excel_generator = excel_generator or ExcelReportGenerator()
        self.ses_settings = ses_settings
        self.email_sender = email_sender

    def run(self, request: ReportRequestDetails) -> ReportExecutionResult:
        try:
            definition = self.catalog.get(request.report_name)
            date_range = self.date_resolver.resolve(request)
            params = build_report_parameters(request, definition, date_range)
            sql = self.catalog.load_sql(definition)

            logger.info(
                "Running report %s for %s to %s",
                definition.report_name,
                date_range.start_date,
                date_range.end_date,
            )
            rows = self.database_client.query(sql, params)
            recipient = request.recipient or definition.default_recipient
            output_path = self.excel_generator.generate(rows, definition, request, date_range)
            email_sent = False
            message = "Excel report generated."

            if recipient and (self.ses_settings or self.email_sender):
                email_sender = self.email_sender
                if email_sender is None:
                    from common.email_utils import send_report_email_ses

                    email_sender = send_report_email_ses

                email_sent = email_sender(
                    self.ses_settings,
                    recipient=recipient,
                    subject=f"[AMS AI] {definition.title}",
                    text_body=(
                        f"Attached report: {definition.title}\n"
                        f"Date range: {date_range.start_date} to {date_range.end_date}\n"
                        f"Record count: {len(rows)}"
                    ),
                    attachment_path=output_path,
                )
                message = "Excel report generated and email sent through SES." if email_sent else (
                    "Excel report generated, but email was not sent. Check SES configuration."
                )

            return ReportExecutionResult(
                ticket_number=request.ticket_number,
                report_name=definition.report_name,
                status="EMAIL_SENT" if email_sent else "REPORT_GENERATED",
                record_count=len(rows),
                recipient=recipient,
                message=message,
                output_path=output_path,
                email_sent=email_sent,
            )
        except Exception as exc:
            logger.exception("Report request %s failed", request.ticket_number)
            return ReportExecutionResult(
                ticket_number=request.ticket_number,
                report_name=request.report_name or "unknown",
                status="FAILED",
                message=str(exc),
            )
