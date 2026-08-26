"""
Offline tests for the Restart Agent flow.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agents.restart_agent import RestartAgent
from common.models import RestartRequestDetails, Ticket
from common.restart_catalog import RestartJobCatalog
from common.restart_service import RestartService


class FakeAnthropicSettings:
    model_restart_request_parser = "test-restart-request-parser"


def make_catalog_task() -> Ticket:
    return Ticket(
        sys_id="sctask123",
        number="SCTASK0010002",
        table="sc_task",
        sys_class_name="sc_task",
        short_description="Fulfill job restart request",
        description="Task generated from the restart catalog item.",
        request_item_sys_id="ritm456",
        request_item_number="RITM0010002",
        request_item_short_description="Disable daily member load Glue job",
        request_item_description="Please disable the daily_member_load Glue job schedule.",
    )


def make_catalog() -> RestartJobCatalog:
    raw = {
        "jobs": [
            {
                "job_name": "daily_member_load",
                "service": "glue",
                "trigger_name": "daily_member_load_schedule",
                "allowed_actions": ["restart", "enable", "disable"],
                "aliases": ["daily member load", "member load"],
                "description": "Loads members every day.",
            }
        ]
    }
    temp_dir = tempfile.TemporaryDirectory()
    catalog_path = Path(temp_dir.name) / "jobs.json"
    catalog_path.write_text(json.dumps(raw), encoding="utf-8")
    catalog = RestartJobCatalog(str(catalog_path))
    catalog._temp_dir = temp_dir
    return catalog


class FakeGlueClient:
    def __init__(self):
        self.started_jobs = []
        self.started_triggers = []
        self.stopped_triggers = []

    def start_job_run(self, JobName):
        self.started_jobs.append(JobName)
        return {"JobRunId": "jr_123"}

    def start_trigger(self, Name):
        self.started_triggers.append(Name)
        return {}

    def stop_trigger(self, Name):
        self.stopped_triggers.append(Name)
        return {}


def test_restart_agent_extracts_disable_request():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "service": "AWS Glue",
        "action": "turn off",
        "job_name": "daily_member_load",
        "confidence": 0.96,
        "rationale": "The RITM asks to disable the Glue job schedule.",
        "insufficient_information": False,
    }

    agent = RestartAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    result = agent.parse(make_catalog_task())

    assert result.service == "glue"
    assert result.action == "disable"
    assert result.job_name == "daily_member_load"
    assert result.insufficient_information is False
    assert "Parent RITM description" in llm_client.complete_json.call_args.args[1]
    print("test_restart_agent_extracts_disable_request: PASSED")


def test_restart_agent_uses_required_fields_for_sufficiency():
    llm_client = MagicMock()
    llm_client.complete_json.return_value = {
        "service": "glue",
        "action": "disable",
        "job_name": "usmg-dev-fulfillment-pso-fallout",
        "confidence": 0.95,
        "rationale": "The RITM asks to disable the Glue job trigger.",
        "insufficient_information": True,
    }

    agent = RestartAgent(llm_client=llm_client, anthropic_settings=FakeAnthropicSettings())
    result = agent.parse(make_catalog_task())

    assert result.service == "glue"
    assert result.action == "disable"
    assert result.job_name == "usmg-dev-fulfillment-pso-fallout"
    assert result.insufficient_information is False
    print("test_restart_agent_uses_required_fields_for_sufficiency: PASSED")


def test_restart_catalog_resolves_aliases():
    catalog = make_catalog()

    definition = catalog.get("daily member load")

    assert definition.job_name == "daily_member_load"
    assert definition.trigger_name == "daily_member_load_schedule"
    print("test_restart_catalog_resolves_aliases: PASSED")


def test_restart_service_rejects_unknown_job():
    service = RestartService(catalog=make_catalog(), glue_client=FakeGlueClient())
    request = RestartRequestDetails(
        ticket_number="SCTASK0010002",
        service="glue",
        action="disable",
        job_name="unknown_job",
        confidence=0.9,
    )

    result = service.run(request)

    assert result.status == "FAILED"
    assert result.executed is False
    assert "not in the approved catalog" in result.message
    print("test_restart_service_rejects_unknown_job: PASSED")


def test_restart_service_disables_approved_trigger():
    glue = FakeGlueClient()
    service = RestartService(catalog=make_catalog(), glue_client=glue)
    request = RestartRequestDetails(
        ticket_number="SCTASK0010002",
        service="glue",
        action="disable",
        job_name="daily_member_load",
        confidence=0.9,
    )

    result = service.run(request)

    assert result.status == "RESTART_EXECUTED"
    assert result.executed is True
    assert glue.stopped_triggers == ["daily_member_load_schedule"]
    assert glue.started_jobs == []
    print("test_restart_service_disables_approved_trigger: PASSED")


def test_restart_service_starts_approved_glue_job():
    glue = FakeGlueClient()
    service = RestartService(catalog=make_catalog(), glue_client=glue)
    request = RestartRequestDetails(
        ticket_number="SCTASK0010002",
        service="glue",
        action="restart",
        job_name="member load",
        confidence=0.9,
    )

    result = service.run(request)

    assert result.status == "RESTART_EXECUTED"
    assert result.executed is True
    assert result.message == "Started Glue job 'daily_member_load' with run id jr_123."
    assert glue.started_jobs == ["daily_member_load"]
    print("test_restart_service_starts_approved_glue_job: PASSED")


if __name__ == "__main__":
    test_restart_agent_extracts_disable_request()
    test_restart_agent_uses_required_fields_for_sufficiency()
    test_restart_catalog_resolves_aliases()
    test_restart_service_rejects_unknown_job()
    test_restart_service_disables_approved_trigger()
    test_restart_service_starts_approved_glue_job()
    print("All restart-agent tests passed.")
