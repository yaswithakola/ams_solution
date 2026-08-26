"""
Deterministic executor for Restart Agent requests.

Only jobs listed in RestartJobCatalog can be operated on. The current
backend is AWS Glue through boto3.
"""
import logging

from common.models import RestartExecutionResult, RestartRequestDetails
from common.restart_catalog import RestartJobCatalog

logger = logging.getLogger(__name__)


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
