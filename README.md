# AMS Agentic AI Solution — Production Support

Multi-agent system for ServiceNow production support:

| Component | Status |
|---|---|
| AMS Orchestrator AI Agent | ✅ Built |
| Incident Router AI Agent | ✅ Built (anti-hallucination: says "insufficient information" instead of guessing) |
| Job Remediation AI Agent | ✅ Built - real AWS remediation via boto3, gated by SOP-driven guardrails |
| Service Request Router + Report Generation Agent | ✅ Built - routes report requests, generates Excel, emails through SES |
| Restart Agent | ✅ Built - routes approved Glue restart/enable/disable requests from ServiceNow catalog tasks |
| Shared Vector Database (BGE-M3 + Qdrant) | ✅ Built - incidents + SOPs, continuously updated |
| Human approval via email (routing + remediation) | ✅ Built - one-click Approve/Reject links |
| Audit trail | ✅ Built (PostgreSQL) |

Agents are backed by a configurable LLM provider. Local development defaults to **Ollama `qwen3:4b`** through `common/ollama_client.py`; switch `LLM_PROVIDER` later to test Claude/Sonnet through the shared LLM interface without rewriting the agents.

## Architecture

```
                 ServiceNow (incidents + sc_task under RITM)
                              │ REST
                              ▼
                 AMS Orchestrator AI Agent  ──classify──> service request
                              │                         │
                              │                         ▼
                              │              Service Request Router
                              │              - polls catalog tasks (sc_task)
                              │              - reads parent RITM context
                              │                         │
                              │           ┌─────────────┴─────────────┐
                              │           ▼                           ▼
                              │   Report Generation Agent        Restart Agent
                              │   - extracts report fields       - extracts service/action/job
                              │   - runs approved SQL            - validates approved job catalog
                              │   - emails Excel via SES         - runs Glue job/trigger action
                              │ incident
                              ▼
                 Incident Router AI Agent
                    - embed (BGE-M3) + search Qdrant (source=incident)
                    - LLM -> L2 group + confidence, or "insufficient information"
                              │
              ┌───────────────┼────────────────────┐
      insufficient      low confidence         confident
      information       (group exists)              │
              │               │                       ▼
        plain email     approve-link email     update ServiceNow +
        (no group to      (Approve -> apply)   feed vector DB back
         approve)                                     │
                                                        ▼
                                          LLM: is this a job/AWS failure?
                                                        │ yes
                                                        ▼
                                          Job Remediation AI Agent
                                            - match SOP (sop_documents/)
                                            - search Qdrant (source=job_failure)
                                            - LLM -> action + AWS params + confidence
                                                        │
                                                        ▼
                                          Guardrails (common/guardrails.py)
                                     ┌──────────────────┴───────────────────┐
                                 ALL PASS                              ANY FAIL
                                     │                                       │
                                     ▼                                       ▼
                     RemediationExecutor (real boto3         Approve/Reject email
                     calls against AWS) + audit log          (Approve -> executes
                     + feed vector DB back                    against AWS, same as
                                                                auto path) + audit log
```

## Project layout

```
ams_solution/
├── config.py                        # all settings, env-var driven
├── main.py                          # entry point: wires agents together
├── approval_server.py               # Flask server handling Approve/Reject email links
├── requirements.txt
├── .env.example                     # copy to .env and fill in credentials
├── infra/iam/
│   └── ams-agent-runtime-policy.json # IAM policy template for whoever runs the app
├── sop_documents/                   # real SOP library (ECS/RDS/EC2/Lambda/Glue/SFN)
│   ├── SOP-*.json                   # structured metadata (resolution_steps, risk, etc.)
│   ├── SOP-*.txt                    # rich narrative version, indexed for RAG
│   └── runbooks/*.json              # supplementary diagnostic guides
├── common/
│   ├── models.py                    # Ticket, RoutingResult, RemediationRecommendation...
│   ├── servicenow_client.py         # ServiceNow REST client (placeholders for creds)
│   ├── llm_factory.py               # chooses Ollama locally or Anthropic later
│   ├── ollama_client.py             # local Ollama wrapper
│   ├── embeddings.py                # BGE-M3 embedder
│   ├── vector_db.py                 # Qdrant wrapper (shared collection: incidents + SOPs)
│   ├── sop_store.py                 # loads/matches SOPs, indexes them into Qdrant
│   ├── guardrails.py                # safety gate before any auto-remediation
│   ├── aws_client.py                # boto3 session/client factory
│   ├── remediation_executor.py      # REAL AWS actions (ECS/RDS/EC2/Lambda/SFN/Glue)
│   ├── restart_catalog.py           # approved job catalog for Restart Agent
│   ├── restart_service.py           # deterministic Glue restart/enable/disable executor
│   ├── approval_store.py            # PostgreSQL - pending human-approval tokens
│   ├── audit_store.py               # PostgreSQL - remediation audit trail
│   └── email_utils.py               # SES: report emails; SMTP: approval emails
├── agents/
│   ├── ams_orchestrator_agent.py    # AMS Orchestrator AI Agent
│   ├── incident_router_agent.py     # Incident Router AI Agent
│   ├── report_generation_agent.py   # Service-request report parser
│   ├── restart_agent.py             # Service-request restart parser
│   └── job_remediation_agent.py     # Job Remediation AI Agent
├── restart/
│   └── jobs.json                    # allowlist of jobs/actions Restart Agent can execute
├── ingestion/
│   ├── ingest_incidents.py          # one-time historical incident load into Qdrant
│   ├── ingest_sops.py               # one-time SOP load into Qdrant
│   └── inspect_collection.py        # CLI to browse/search the vector DB
└── tests/
    └── test_orchestrator_flow.py    # offline tests with mocked ServiceNow/LLM/AWS/vector store
```

## Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure credentials** — copy `.env.example` to `.env` and fill in:
   - `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=qwen3:4b`, and Ollama running locally
   - `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` (bare instance root URL, real decoded password - see comments in `.env.example` for common pitfalls)
   - `QDRANT_URL` (run locally: `docker run -p 6333:6333 qdrant/qdrant`)
   - `POSTGRES_URL` (run locally: `docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=ams_agentic postgres`) - backs the audit trail (`common/audit_store.py`) and human-approval tokens (`common/approval_store.py`), one shared database, two tables (`audit_trail`, `approvals`), created automatically on first connect
   - `REPORT_POSTGRES_URL`, `REPORT_CATALOG_PATH`, `REPORT_SQL_DIR`, and `REPORT_OUTPUT_DIR` for report generation
   - `RESTART_JOB_CATALOG_PATH` for the Restart Agent's approved job/action allowlist
   - `AWS_REGION` and `SES_SOURCE_EMAIL` for report emails through Amazon SES
   - `SERVICE_REQUEST_TASK_QUERY_FILTER` for catalog-task polling, for example `active=true^assignment_group=<your automation group sys_id>`
   - SMTP settings only if you use the existing human approval emails for routing/remediation
   - `APPROVAL_BASE_URL` (must be reachable from wherever you open the approval email)
   - **AWS credentials** — see "AWS Prerequisites" below before enabling real remediation

3. **One-time: ingest historical ServiceNow incidents**:
   ```bash
   python -m ingestion.ingest_incidents
   ```

4. **One-time: ingest the SOP library** into the same vector DB:
   ```bash
   python -m ingestion.ingest_sops
   ```
   Re-run this whenever you add/edit SOP documents. `main.py` does NOT re-index SOPs on every startup by default (it would otherwise force-load the embedding model on every run, slowing down testing) - set `SOP_REINDEX_ON_STARTUP=true` in `.env` if you want it to always reindex on startup instead.

5. **Run both processes**:
   ```bash
   python approval_server.py     # handles Approve/Reject email links
   python main.py --once         # single pass, or omit --once for continuous polling
   ```

## AWS Prerequisites

The Job Remediation AI Agent's executor (`common/remediation_executor.py`) makes **real boto3 calls against your AWS account** once a remediation action clears guardrails (or a human approves it). This section covers what needs to be in place before that will work.

### 1. AWS credentials

No custom credential system - this project uses boto3's standard credential chain, same as the AWS CLI. Pick one:
- **Env vars**: set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `.env`.
- **Named profile**: run `aws configure --profile ams-remediation`, then set `AWS_PROFILE=ams-remediation` in `.env`.
- **IAM role**: if `main.py`/`approval_server.py` run on an EC2 instance, ECS task, or Lambda, attach an IAM role instead - no keys needed at all. Preferred over long-lived keys.

Set `AWS_REGION` to match where your resources live.

### 2. IAM permissions

Attach `infra/iam/ams-agent-runtime-policy.json` to the IAM identity that runs this repo:

- Local PowerShell run: attach it to the IAM user/role shown by `aws sts get-caller-identity`.
- Lambda/ECS/EC2 run: attach it to the execution role used by that service.

The checked-in template includes SES report email, Restart Agent Glue actions, existing remediation actions, CloudWatch evidence lookup, and S3 remediation file movement. It currently uses `"Resource": "*"` so local testing is simple. For production, scope resources down to the specific SES identity, Glue jobs/triggers, log groups, buckets, and AWS resources this agent is allowed to touch.

Current policy template:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SendReportEmailsWithSes",
      "Effect": "Allow",
      "Action": [
        "ses:SendEmail",
        "ses:SendRawEmail"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RunApprovedRestartAgentGlueActions",
      "Effect": "Allow",
      "Action": [
        "glue:StartJobRun",
        "glue:StartTrigger",
        "glue:StopTrigger"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RunApprovedIncidentRemediationActions",
      "Effect": "Allow",
      "Action": [
        "ecs:UpdateService",
        "ecs:DescribeServices",
        "ecs:StopTask",
        "ecs:ListContainerInstances",
        "ecs:DescribeContainerInstances",
        "rds:RebootDBInstance",
        "rds:DescribeDBInstances",
        "ec2:RebootInstances",
        "ec2:StopInstances",
        "ec2:StartInstances",
        "ec2:DescribeInstances",
        "lambda:GetFunctionConfiguration",
        "lambda:UpdateFunctionConfiguration",
        "states:StartExecution",
        "states:DescribeExecution",
        "glue:GetJobRun",
        "glue:GetJob",
        "glue:GetJobRuns"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadOperationalEvidence",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "s3:ListBucket",
        "s3:GetObject"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MoveApprovedS3RemediationFiles",
      "Effect": "Allow",
      "Action": [
        "s3:CopyObject",
        "s3:DeleteObject"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. Pre-existing AWS resources ("set up the job")

The agent **remediates existing resources - it does not create them.** Before testing end-to-end, make sure the specific resource each SOP targets already exists and is reachable:

| SOP | Needs to already exist |
|---|---|
| SOP-EC2-401 (EC2) | A running EC2 instance you can safely reboot/stop-start |
| SOP-RDS-201 (RDS) | An RDS instance you can safely reboot |
| SOP-LAMBDA-301 (Lambda) | A deployed Lambda function |
| SOP-GLUE-501 (Glue) | A defined Glue job (`aws glue get-job --job-name ...`) |
| SOP-SFN-601 (Step Functions) | A deployed state machine (this SOP is `.txt`-only - see note below) |
| SOP-EKS-101 (EKS) | Not wired to an executor action yet - see "What's not ported" below |
| SOP-EAM-MMSO-701 (S3, Enrollments MMS job) | An "error bucket" (`eam-mmso-error-bucket-2330`) with job run log files, and a "keyword bucket" (`eam-mmso-keyword-bucket-2330`) with `input/`, `processed/`, `error/` folders |

For a first end-to-end test, the cheapest option is usually a small EC2 instance or Lambda function you don't mind rebooting/restarting.

### 4. What's not (fully) ported from the reference architecture

This project reimplements the reference architecture's **Lambda/Step-Functions/Terraform** design as **plain Python function calls** from `main.py`/`approval_server.py` - simpler to run locally, no AWS deployment required for the agent logic itself (only for the resources being remediated). A few things follow from that:

- **`process_sfn_error_file`** (the MMSO-XML-file / S3 bucket workflow from the original `remediation_executor`) was intentionally **not ported** at first - it hardcoded a specific bucket name and business logic specific to the original environment. This is now reimplemented properly parameterized as **SOP-EAM-MMSO-701** (the "Enrollments MMS job"): bucket names (`error_bucket`/`keyword_bucket`) come from the SOP document (`common/sop_store.py`), not hardcoded, and grounding is fetched via `common/s3_client.py` / `agents/job_remediation_agent.py`'s `_fetch_s3_context` rather than the original Lambda-specific logic.
- **SOP-GLUE-501** and **SOP-SFN-601** don't declare `resolution_steps`/`auto_resolvable` in the same structured way the EC2/RDS/Lambda SOPs do (Glue uses a free-text `steps` list; SFN has no `.json` at all). `common/sop_store.py` loads them **fail-closed**: `auto_resolvable=False`, so any incident matched to them always requires human approval until you add explicit `resolution_steps`/`auto_resolvable`/`risk_level` fields to their JSON.
- The reference architecture's `decision_tree` step-by-step diagnostic logic (e.g. "check if Multi-AZ is enabled before rebooting") is passed to the LLM as **context**, not mechanically evaluated - this project's Job Remediation AI Agent doesn't have a live status-check feed (CloudWatch, `describe_db_instances`, etc.) the way the original Lambda pipeline did; it only sees the ServiceNow incident text. The LLM uses the decision tree as guidance in its rationale, but if the incident text doesn't answer a condition (e.g. "is Multi-AZ enabled?"), the agent should report insufficient information rather than assume.
- **SOP-EKS-101** has no corresponding action in `remediation_executor.py` (EKS remediation wasn't in the original `dispatch_action` handler map either) - it's indexed for RAG context but any match against it will always fail guardrails (`action_not_in_sop_resolution_steps`) and go to a human.

## How each agent works

### AMS Orchestrator AI Agent (`agents/ams_orchestrator_agent.py`)
Classifies incident vs. service request. Incidents go through the Incident Router, confidence gate, L2 assignment, and job-remediation check. Service requests are fulfilled from catalog tasks (`sc_task`): the task is polled, the parent RITM is read as the main request context, and the Service Request Router decides whether it is report generation, restart, or unsupported. Report-generation requests are parsed, executed through the approved report catalog, emailed through SES, and updated as ServiceNow task comments. Restart requests are parsed by the Restart Agent and executed only if the requested Glue job/action is listed in `restart/jobs.json`. For now, both the catalog task and parent RITM are intentionally left open after processing.

### Restart Agent (`agents/restart_agent.py`)
Handles service catalog requests such as "restart daily_member_load", "disable the daily member load Glue job", or "enable claims failure reprocess". The LLM only extracts `service`, `action`, and `job_name`; it does not call AWS. `common/restart_service.py` validates the extracted job against `restart/jobs.json`, then calls boto3 for the approved action:
- `restart` -> `glue.start_job_run`
- `enable` -> `glue.start_trigger`
- `disable` -> `glue.stop_trigger`

### Incident Router AI Agent (`agents/incident_router_agent.py`)
Embeds the incident, searches Qdrant (`source=incident`) for similar historical incidents, and asks the LLM for an L2 group + confidence - explicitly instructed to say "I do not have enough information" rather than guess when the evidence is too thin.

### Job Remediation AI Agent (`agents/job_remediation_agent.py`)
Matches the incident to a SOP (`common/sop_store.py`) **and** fetches recent log/file evidence for the same job/resource: CloudWatch Logs error events (`common/cloudwatch_client.py`, best-effort - finds a log group whose name contains the job/CI name) for CloudWatch-backed jobs, and/or, for S3-driven jobs (e.g. SOP-EAM-MMSO-701), the latest log file from the SOP's `error_bucket` plus recent file listings from its `keyword_bucket`'s `error/`/`input/` folders (`common/s3_client.py`). Also searches Qdrant (`source=job_failure`) for similar past remediations.

**Decision rule**: needs at least one of {SOP match, log/file evidence} to attempt a recommendation.
- **Both available** → the LLM cross-checks them for agreement (raises confidence) or conflict (lowers it / falls back to insufficient information).
- **Only one available** → proceeds using whichever it has, and says so in its rationale. Logs-only recommendations are restricted to a fixed, safe action registry (no SOP resolution_steps to constrain against).
- **Neither available** → short-circuited to `insufficient_information=True` **without even calling the LLM** - there's nothing to ground a recommendation in, so it goes straight to human intervention (plain notification, no approve link, same as the Incident Router's insufficient-information path).

Whatever action it recommends, it must also extract the AWS resource identifiers that action needs (cluster name, DB identifier, instance ID, etc.) from the ticket text/CI/logs - never inventing one. Returns `insufficient_information=true` if it can't do this confidently even with grounding available.

### Guardrails (`common/guardrails.py`)
Before any action auto-executes, checks: a SOP matched; the action is in that SOP's `resolution_steps`; the SOP is `auto_resolvable`; risk level is allowed; the action isn't blocked; confidence meets threshold; the LLM itself didn't flag `requires_human_approval`; the recommendation isn't `insufficient_information`; and every required AWS parameter for that action was actually extracted. All must pass to auto-execute - any failure routes to a human via approve/reject email.

### Remediation Executor (`common/remediation_executor.py`)
Real boto3 actions: `restart_ecs_service`, `restart_ecs_task`, `restart_ecs_node`, `restart_rds_instance`/`reboot_rds`, `restart_ec2_instance`/`reboot_ec2`, `stop_start_ec2`, `scale_ecs_service`, `scale_out_ecs`, `restart_lambda`/`clear_lambda_error`, `retry_step_function`, `retry_glue_job`, `move_keyword_files_to_input`, `no_action_required`. Each pulls its required parameters from the LLM-extracted `action_parameters`.

### Vector Database (`common/vector_db.py`, `common/sop_store.py`)
One shared Qdrant collection for incidents (`source=incident`), job-failure outcomes (`source=job_failure`), and SOPs (`source=sop`). Both `ingest_incidents.py` and `ingest_sops.py` seed it once; the orchestrator and approval_server feed confirmed outcomes back into it continuously (`upsert_routing_feedback`, `upsert_job_remediation_feedback`).

### Relational database (`common/audit_store.py`, `common/approval_store.py`)
One shared PostgreSQL database (`POSTGRES_URL`), alongside the shared Qdrant vector DB above - two tables: `audit_trail` (every remediation decision - ticket, action, SOP, risk level, confidence, actor `ai_auto`/`human_approved`/`human_rejected`, and result) and `approvals` (pending/approved/rejected/expired human-approval tokens, including the full recommended action + parameters so `approval_server.py` can execute it on click).

## Notes / assumptions
- ServiceNow states `6`/`7` (Resolved/Closed) are the default "trustworthy" filter for incident ingestion.
- `CONFIDENCE_THRESHOLD` (routing) and `REMEDIATION_CONFIDENCE_THRESHOLD` (remediation) are independently tunable in `.env`.
- `ANTHROPIC_MODEL_JOB_CLASSIFIER` backs both the orchestrator's job-failure classification step and the Job Remediation AI Agent.
- Network access was unavailable in the build sandbox, so dependencies couldn't be pip-installed or tests executed there - all files were syntax-checked (`py_compile`). Run `python -m tests.test_orchestrator_flow` locally after installing requirements.
