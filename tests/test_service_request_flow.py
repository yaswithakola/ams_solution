"""
Offline tests for the service-request branch of the AMS orchestrator.
"""
from unittest.mock import MagicMock

from agents.report_generation_agent import ReportGenerationAgent
from agents.service_request_router_agent import ServiceRequestRouterAgent
from common.models import ReportRequestDetails, ServiceRequestRoute, Ticket


class FakeAnthropicSettings:
    model_service_request_router = "test-service-request-router"
    model_report_request_parser = "test-report-request-parser"


def make_service_request(number="RITM0010001") -> Ticket:
    return Ticket(
        sys_id="ritm123",
        number=number,
        table="sc_req_item",
        sys_class_name="sc_req_item",
        short_description="Generate monthly member enrollment report for last month",
        description="Please generate monthly member enrollment report for Texas for last month and email it to ops@example.com.",
    )


def _orchestrator_dependencies_available():
    required_modules = ("psycopg2", "anthropic", "qdrant_client", "boto3", "requests")
    for module_name in required_modules:
        try:
            __import__(module_name)
        except ModuleNotFoundError:
            return False, module_name
    return True, None


def make_orchestrator(service_request_router, report_generation_agent):
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
        report_service=None,
    )


def test_service_request_router_classifies_report_generation():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "request_type": "report_generation",
        "confidence": 0.93,
        "rationale": "The request asks to generate and email a report.",
        "insufficient_information": False,
    }

    agent = ServiceRequestRouterAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    route = agent.route(make_service_request())

    assert route.request_type == "report_generation"
    assert route.confidence == 0.93
    assert route.insufficient_information is False
    llm_client.complete_json.assert_called_once()
    print("test_service_request_router_classifies_report_generation: PASSED")


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
    details = agent.parse(make_service_request())

    assert details.report_name == "member_enrollment"
    assert details.frequency == "monthly"
    assert details.date_range == "previous_month"
    assert details.filters == {"state": "Texas"}
    assert details.recipient == "ops@example.com"
    assert details.insufficient_information is False
    print("test_report_generation_agent_extracts_report_request: PASSED")


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
    ticket = make_service_request()

    orchestrator.process_ticket(ticket)

    router.route.assert_called_once_with(ticket)
    report_agent.parse.assert_called_once_with(ticket)
    orchestrator.incident_router.route.assert_not_called()
    orchestrator.servicenow.add_work_note.assert_called_once()
    print("test_orchestrator_routes_service_request_to_report_agent: PASSED")


if __name__ == "__main__":
    test_service_request_router_classifies_report_generation()
    test_report_generation_agent_extracts_report_request()
    test_orchestrator_routes_service_request_to_report_agent()
    print("All service-request tests passed.")
