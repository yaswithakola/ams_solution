"""
Offline tests for the service-request branch of the AMS orchestrator.
"""
from unittest.mock import MagicMock

from agents.report_generation_agent import ReportGenerationAgent
from agents.restart_agent import RestartAgent
from agents.service_request_router_agent import ServiceRequestRouterAgent
from common.models import (
    ReportExecutionResult,
    ReportRequestDetails,
    RestartExecutionResult,
    RestartRequestDetails,
    ServiceRequestRoute,
    Ticket,
)


class FakeAnthropicSettings:
    model_service_request_router = "test-service-request-router"
    model_report_request_parser = "test-report-request-parser"
    model_restart_request_parser = "test-restart-request-parser"


def make_catalog_task(number="SCTASK0010001") -> Ticket:
    return Ticket(
        sys_id="sctask123",
        number=number,
        table="sc_task",
        sys_class_name="sc_task",
        short_description="Fulfill reporting request",
        description="Task generated from the reporting catalog item.",
        request_item_sys_id="ritm123",
        request_item_number="RITM0010001",
        request_item_short_description="Generate monthly member enrollment report for last month",
        request_item_description=(
            "Please generate monthly member enrollment report for Texas for last month "
            "and email it to ops@example.com."
        ),
    )


def make_ritm(number="RITM0010001") -> Ticket:
    return Ticket(
        sys_id="ritm123",
        number=number,
        table="sc_req_item",
        sys_class_name="sc_req_item",
        short_description="Generate monthly member enrollment report for last month",
        description=(
            "Please generate monthly member enrollment report for Texas for last month "
            "and email it to ops@example.com."
        ),
        requested_for_email="ops@example.com",
    )


def _orchestrator_dependencies_available():
    required_modules = ("psycopg2", "anthropic", "qdrant_client", "boto3", "requests")
    for module_name in required_modules:
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            return False, module_name
    return True, None


def make_orchestrator(
    service_request_router,
    report_generation_agent,
    report_service=None,
    restart_agent=None,
    restart_service=None,
):
    from agents.ams_orchestrator_agent import AMSOrchestratorAgent
    from config import get_settings

    settings = get_settings()
    return AMSOrchestratorAgent(
        settings=settings,
        servicenow_client=MagicMock(),
        incident_router_agent=MagicMock(),
        job_remediation_agent=MagicMock(),
        llm_client=MagicMock(),
        approval_store=MagicMock(),
        vector_store=MagicMock(),
        sop_store=MagicMock(),
        guardrails=MagicMock(),
        remediation_executor=MagicMock(),
        audit_store=MagicMock(),
        service_request_router_agent=service_request_router,
        report_generation_agent=report_generation_agent,
        report_service=report_service,
        restart_agent=restart_agent,
        restart_service=restart_service,
    )


def test_servicenow_fetches_open_catalog_tasks_with_parent_ritm_context():
    try:
        from common.servicenow_client import ServiceNowClient
    except ModuleNotFoundError as exc:
        print(f"test_servicenow_fetches_open_catalog_tasks_with_parent_ritm_context: SKIPPED (missing {exc.name})")
        return

    class FakeServiceNowSettings:
        instance_url = "https://example.service-now.com"
        username = "user"
        password = "pass"
        verify_ssl = False
        timeout_seconds = 5
        incident_query_filter = "active=true"
        service_request_query_filter = "active=true^state=1"
        service_request_task_query_filter = "active=true^assignment_group=AMS Automation"

    class FakeResponse:
        def __init__(self, result):
            self.result = result

        def json(self):
            return {"result": self.result}

    class CapturingClient(ServiceNowClient):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls = []

        def _request(self, method, path, **kwargs):
            self.calls.append({"method": method, "path": path, "kwargs": kwargs})
            if path == "/api/now/table/sc_task":
                return FakeResponse([
                    {
                        "sys_id": "sctask123",
                        "number": "SCTASK0010001",
                        "sys_class_name": "sc_task",
                        "short_description": "Fulfill report request",
                        "description": "Catalog task for reporting.",
                        "request_item": {"value": "ritm123"},
                        "request_item.number": "RITM0010001",
                        "request_item.short_description": "Generate report",
                        "request_item.description": "Generate member enrollment report.",
                        "state": "1",
                    }
                ])
            if path == "/api/now/table/sc_req_item/ritm123":
                return FakeResponse({
                    "sys_id": "ritm123",
                    "number": "RITM0010001",
                    "sys_class_name": "sc_req_item",
                    "short_description": "Generate monthly member enrollment report",
                    "description": "Generate monthly member enrollment report for Texas for last month.",
                    "requested_for.email": "requester@example.com",
                    "opened_by.email": "openedby@example.com",
                })
            raise AssertionError(f"Unexpected ServiceNow path: {path}")

    client = CapturingClient(FakeServiceNowSettings())
    tickets = client.get_new_catalog_tasks(extra_query="short_descriptionLIKEREPORT", limit=10)

    assert len(tickets) == 1
    assert tickets[0].number == "SCTASK0010001"
    assert tickets[0].table == "sc_task"
    assert tickets[0].request_item_number == "RITM0010001"
    assert tickets[0].request_item_description == "Generate monthly member enrollment report for Texas for last month."
    assert tickets[0].requested_for_email == "requester@example.com"
    list_call = client.calls[0]
    assert list_call["method"] == "GET"
    assert list_call["path"] == "/api/now/table/sc_task"
    params = list_call["kwargs"]["params"]
    assert params["sysparm_query"] == "active=true^assignment_group=AMS Automation^short_descriptionLIKEREPORT"
    assert params["sysparm_limit"] == 10
    assert "request_item.description" in params["sysparm_fields"]
    print("test_servicenow_fetches_open_catalog_tasks_with_parent_ritm_context: PASSED")


def test_service_request_router_classifies_report_generation():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "request_type": "report_generation",
        "confidence": 0.93,
        "rationale": "The request asks to generate and email a report.",
        "insufficient_information": False,
    }

    agent = ServiceRequestRouterAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    route = agent.route(make_catalog_task())

    assert route.request_type == "report_generation"
    assert route.confidence == 0.93
    assert route.insufficient_information is False
    llm_client.complete_json.assert_called_once()
    assert "Parent RITM description" in llm_client.complete_json.call_args.args[1]
    print("test_service_request_router_classifies_report_generation: PASSED")


def test_service_request_router_classifies_restart_request():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "request_type": "restart",
        "confidence": 0.91,
        "rationale": "The request asks to disable an approved Glue job schedule.",
        "insufficient_information": False,
    }

    ticket = make_catalog_task().model_copy(update={
        "request_item_short_description": "Disable daily member load Glue job",
        "request_item_description": "Please disable the daily member load Glue job schedule.",
    })
    agent = ServiceRequestRouterAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    route = agent.route(ticket)

    assert route.request_type == "restart"
    assert route.confidence == 0.91
    assert route.insufficient_information is False
    print("test_service_request_router_classifies_restart_request: PASSED")


def test_service_request_router_accepts_old_glue_label_as_restart():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "request_type": "glue_job_control",
        "confidence": 0.88,
        "rationale": "Older label returned by the model.",
        "insufficient_information": False,
    }

    agent = ServiceRequestRouterAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    route = agent.route(make_catalog_task())

    assert route.request_type == "restart"
    print("test_service_request_router_accepts_old_glue_label_as_restart: PASSED")


def test_report_generation_agent_extracts_report_request():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "report_name": "member enrollment",
        "frequency": "monthly",
        "date_range": "last month",
        "start_date": None,
        "end_date": None,
        "filters": {"state": "Texas"},
        "output_format": "excel",
        "recipient": "ops@example.com",
        "rationale": "The request contains an approved report, date range, state filter, and recipient.",
        "insufficient_information": False,
    }

    agent = ReportGenerationAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    details = agent.parse(make_catalog_task())

    assert details.report_name == "member_enrollment"
    assert details.frequency == "monthly"
    assert details.date_range == "previous_month"
    assert details.filters == {"state": "Texas"}
    assert details.recipient == "ops@example.com"
    assert details.insufficient_information is False
    assert "Parent RITM description" in llm_client.complete_json.call_args.args[1]
    print("test_report_generation_agent_extracts_report_request: PASSED")


def test_report_generation_agent_uses_requested_for_email_when_recipient_is_missing():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "report_name": "member enrollment",
        "frequency": "monthly",
        "date_range": "previous_month",
        "start_date": None,
        "end_date": None,
        "filters": {"state": "Texas"},
        "output_format": "excel",
        "recipient": None,
        "rationale": "The request contains an approved report and date range.",
        "insufficient_information": False,
    }

    ticket = make_catalog_task().model_copy(update={"requested_for_email": "requester@example.com"})
    agent = ReportGenerationAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    details = agent.parse(ticket)

    assert details.recipient == "requester@example.com"
    print("test_report_generation_agent_uses_requested_for_email_when_recipient_is_missing: PASSED")


def test_restart_agent_extracts_restart_request():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "service": "aws glue",
        "action": "disable",
        "job_name": "daily_member_load",
        "confidence": 0.94,
        "rationale": "The RITM asks to disable this Glue job.",
        "insufficient_information": False,
    }

    ticket = make_catalog_task().model_copy(update={
        "request_item_short_description": "Disable daily member load Glue job",
        "request_item_description": "Disable the daily_member_load Glue schedule.",
    })
    agent = RestartAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    details = agent.parse(ticket)

    assert details.service == "glue"
    assert details.action == "disable"
    assert details.job_name == "daily_member_load"
    assert details.confidence == 0.94
    assert details.insufficient_information is False
    print("test_restart_agent_extracts_restart_request: PASSED")


def test_orchestrator_routes_service_request_to_report_agent():
    available, missing_module = _orchestrator_dependencies_available()
    if not available:
        print(
            "test_orchestrator_routes_service_request_to_report_agent: "
            f"SKIPPED (missing {missing_module})"
        )
        return

    router = MagicMock()
    router.route.return_value = ServiceRequestRoute(
        ticket_number="RITM0010001",
        request_type="report_generation",
        confidence=0.9,
        rationale="Report request.",
    )
    report_agent = MagicMock()
    report_agent.parse.return_value = ReportRequestDetails(
        ticket_number="RITM0010001",
        report_name="member_enrollment",
        frequency="monthly",
        date_range="previous_month",
        start_date=None,
        end_date=None,
        filters={"state": "Texas"},
        recipient="ops@example.com",
    )

    orchestrator = make_orchestrator(router, report_agent)
    ticket = make_ritm()

    orchestrator.process_ticket(ticket)

    router.route.assert_called_once_with(ticket)
    report_agent.parse.assert_called_once_with(ticket)
    orchestrator.incident_router.route.assert_not_called()
    orchestrator.servicenow.add_comment.assert_called_once()
    orchestrator.servicenow.add_work_note.assert_not_called()
    print("test_orchestrator_routes_service_request_to_report_agent: PASSED")


def test_orchestrator_runs_report_service_and_leaves_request_open():
    available, missing_module = _orchestrator_dependencies_available()
    if not available:
        print(
            "test_orchestrator_runs_report_service_and_leaves_request_open: "
            f"SKIPPED (missing {missing_module})"
        )
        return

    router = MagicMock()
    router.route.return_value = ServiceRequestRoute(
        ticket_number="RITM0010001",
        request_type="report_generation",
        confidence=0.9,
        rationale="Report request.",
    )
    report_details = ReportRequestDetails(
        ticket_number="RITM0010001",
        report_name="member_enrollment",
        frequency="monthly",
        date_range="previous_month",
        filters={"state": "Texas"},
        recipient="ops@example.com",
    )
    report_agent = MagicMock()
    report_agent.parse.return_value = report_details
    report_service = MagicMock()
    report_service.run.return_value = ReportExecutionResult(
        ticket_number="RITM0010001",
        report_name="member_enrollment",
        status="EMAIL_SENT",
        record_count=2,
        recipient="ops@example.com",
        message="Excel report generated and email sent through SES.",
        output_path="output/member_enrollment.xlsx",
        email_sent=True,
    )

    orchestrator = make_orchestrator(router, report_agent, report_service=report_service)
    ticket = make_ritm()

    orchestrator.process_ticket(ticket)
    orchestrator.process_ticket(ticket)

    report_service.run.assert_called_once_with(report_details)
    orchestrator.servicenow.add_comment.assert_called_once_with(
        ticket.sys_id,
        "Generated the member enrollment report and emailed it to ops@example.com.",
        table=ticket.table,
    )
    orchestrator.servicenow.get_catalog_tasks_for_request_item.assert_called_once_with(ticket.sys_id)
    orchestrator.servicenow.close_catalog_task.assert_not_called()
    orchestrator.servicenow.add_work_note.assert_not_called()
    print("test_orchestrator_runs_report_service_and_leaves_request_open: PASSED")


def test_orchestrator_runs_restart_service_and_closes_related_catalog_tasks():
    available, missing_module = _orchestrator_dependencies_available()
    if not available:
        print(
            "test_orchestrator_runs_restart_service_and_leaves_request_open: "
            f"SKIPPED (missing {missing_module})"
        )
        return

    router = MagicMock()
    router.route.return_value = ServiceRequestRoute(
        ticket_number="RITM0010001",
        request_type="restart",
        confidence=0.9,
        rationale="Restart request.",
    )
    restart_details = RestartRequestDetails(
        ticket_number="RITM0010001",
        service="glue",
        action="disable",
        job_name="daily_member_load",
        confidence=0.9,
    )
    restart_agent = MagicMock()
    restart_agent.parse.return_value = restart_details
    restart_service = MagicMock()
    restart_service.run.return_value = RestartExecutionResult(
        ticket_number="RITM0010001",
        status="RESTART_EXECUTED",
        service="glue",
        action="disable",
        job_name="daily_member_load",
        trigger_name="daily_member_load_schedule",
        message="Disabled Glue trigger 'daily_member_load_schedule'.",
        executed=True,
    )

    orchestrator = make_orchestrator(
        router,
        report_generation_agent=MagicMock(),
        restart_agent=restart_agent,
        restart_service=restart_service,
    )
    related_task = make_catalog_task()
    orchestrator.servicenow.get_catalog_tasks_for_request_item.return_value = [related_task]
    ticket = make_ritm().model_copy(update={
        "short_description": "Disable daily member load Glue job",
        "description": "Disable the daily_member_load Glue schedule.",
    })

    orchestrator.process_ticket(ticket)
    orchestrator.process_ticket(ticket)

    restart_agent.parse.assert_called_once_with(ticket)
    restart_service.run.assert_called_once_with(restart_details)
    orchestrator.servicenow.add_comment.assert_any_call(
        ticket.sys_id,
        "Disabled the Glue trigger daily_member_load_schedule for job daily_member_load.",
        table=ticket.table,
    )
    orchestrator.servicenow.add_comment.assert_any_call(
        related_task.sys_id,
        "Disabled the Glue trigger daily_member_load_schedule for job daily_member_load.",
        table=related_task.table,
    )
    orchestrator.servicenow.close_catalog_task.assert_called_once_with(
        related_task.sys_id,
        close_state=orchestrator.settings.servicenow.service_request_task_closed_state,
    )
    orchestrator.servicenow.add_work_note.assert_not_called()
    print("test_orchestrator_runs_restart_service_and_closes_related_catalog_tasks: PASSED")


if __name__ == "__main__":
    test_servicenow_fetches_open_catalog_tasks_with_parent_ritm_context()
    test_service_request_router_classifies_report_generation()
    test_service_request_router_classifies_restart_request()
    test_service_request_router_accepts_old_glue_label_as_restart()
    test_report_generation_agent_extracts_report_request()
    test_report_generation_agent_uses_requested_for_email_when_recipient_is_missing()
    test_restart_agent_extracts_restart_request()
    test_orchestrator_routes_service_request_to_report_agent()
    test_orchestrator_runs_report_service_and_leaves_request_open()
    test_orchestrator_runs_restart_service_and_closes_related_catalog_tasks()
    print("All service-request tests passed.")
