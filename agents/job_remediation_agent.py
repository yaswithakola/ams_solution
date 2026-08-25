"""
Job Remediation AI Agent
=========================
Input : a Ticket already flagged as a job failure by the AMS Orchestrator.
Process:
    1. Match the incident against the real SOP library
       (sop_documents/*.json + *.txt, common/sop_store.py) using keyword
       matching on service/error type/title (e.g. "ECS", "RDS", "EC2",
       "Lambda", "Glue", "Step Functions").
    2. Fetch recent CloudWatch Logs error events for the same job/resource
       (common/cloudwatch_client.py) - best-effort, using the job name /
       CI name to find a matching log group. For jobs diagnosed from S3
       instead of CloudWatch (e.g. SOP-EAM-MMSO-701's "Enrollments MMS
       job"), also fetch S3-based evidence (common/s3_client.py): the
       latest log file from the SOP's error_bucket, and recent file
       listings from the SOP's keyword_bucket's error/ and input/
       folders. Both sources feed the same "log evidence" grounding slot.
    3. Search the SAME shared Qdrant vector database (source="job_failure")
       for similar past job failures and the action that was taken for
       them - this is the continuous-learning signal.
    4. DECISION RULE: this agent needs at least one grounding source (a
       matched SOP OR CloudWatch log events) to attempt a recommendation:
         - SOP + logs both available -> LLM synthesizes both into a
           recommendation, cross-checking that they agree.
         - Only one available -> LLM proceeds using whichever it has,
           and must say in its rationale that the other source was
           unavailable.
         - NEITHER available -> short-circuited to
           insufficient_information=True WITHOUT even calling the LLM -
           there is nothing to ground a recommendation in, so asking the
           model would just invite a guess.
    5. When at least one source is available, pass everything gathered to
       an Anthropic LLM model, asking it to pick ONE action (from the
       SOP's resolution_steps if a SOP matched; otherwise from the fixed
       action registry) and extract the AWS resource identifiers it needs
       (e.g. ECS cluster_name/service_name, RDS db_identifier, EC2
       instance_id, Lambda function_name, Step Functions
       state_machine_arn, Glue job_name) from the ticket text/CI/logs.
Output: RemediationRecommendation (including action_parameters), which
        the AMS Orchestrator runs through common/guardrails.py before
        deciding whether to auto-execute against real AWS
        (common/remediation_executor.py) or send it to a human for
        approval via the same approve-link email flow used for
        low-confidence incident routing.

Anti-hallucination behavior mirrors the Incident Router AI Agent: even
when at least one grounding source is available, the agent must still
say insufficient_information=True rather than invent an action or a
resource name if what it has doesn't actually support a confident,
specific recommendation. This also fails the guardrails automatically
(SOP checks and the missing_required_parameter check), so it's a layered
safety net, not a single point of failure.
"""
import logging
from typing import List, Optional, TYPE_CHECKING

from common.cloudwatch_client import CloudWatchLogsClient
from common.models import RemediationRecommendation, SimilarJobFailure, Ticket
from common.remediation_executor import REQUIRED_PARAMS
from common.s3_client import S3Client
from common.sop_store import SOP, SOPStore
from common.vector_db import VectorStore

if TYPE_CHECKING:
    from common.llm_base import AgentLLMClient
    from config import AnthropicSettings

logger = logging.getLogger(__name__)

# Floor for the "SOP matched, no logs" case - matches the bottom of the
# "SOP matched, evidence not exact/confirmed" confidence band documented
# in SYSTEM_PROMPT below. A real SOP match is real signal even without
# log confirmation, so a recommended action should never be reported at
# a literal 0.0 confidence.
SOP_ONLY_NO_LOGS_MIN_CONFIDENCE = 0.5

SYSTEM_PROMPT = """You are the Job Remediation AI Agent - a specialized production-support agent for \
an Application Managed Services (AMS) team, focused exclusively on recommending remediation actions \
for AWS resource / batch job failures (ECS services, RDS instances, EC2 instances, Lambda functions, \
Step Functions executions, Glue ETL jobs, S3-driven batch/file-processing jobs, and similar). You do not \
perform any other task.

You are given up to three sources of grounding for your recommendation - use whichever are present:
  - A matched Standard Operating Procedure (SOP): its service, ALLOWED resolution_steps, whether it is \
    auto_resolvable, and its risk_level - or a note that no SOP matched.
  - Recent log/file evidence for this job/resource - CloudWatch log events for CloudWatch-backed jobs, \
    and/or, for S3-driven jobs, the latest log file content from an "error bucket" plus recent file \
    listings from a "keyword bucket"'s error/ and input/ folders - or a note that none was available. \
    Treat "one or more files listed in the error/ folder" as equivalent to a log-content match for a \
    SOP's decision_tree condition that asks whether files are pending in that folder.
  - Similar historical job failures from the vector database and what action was taken for them.
Plus the list of parameters each candidate action requires (e.g. restart_ecs_service needs cluster_name \
and service_name) so you know exactly what to extract.

How to use what's available:
- If the matched SOP's block below includes a "Match rule" line and/or "Decision tree conditions" (these \
  are the SOP's own literal, pre-documented policy - when present, they appear verbatim in the "Matched \
  SOP" section of the user message), APPLY THEM LITERALLY as written, even if you personally judge the \
  true root cause to be something else (e.g. a resource lock rather than a network-level refusal). The \
  SOP's authors already made that judgment call - your job is to apply their documented rule correctly, \
  not to override it with your own independent diagnosis. If the SOP block also includes a "Worked \
  example" of a matching log event, compare the ticket's actual log content against that example's \
  pattern (not just the rule text in the abstract) - if the ticket's logs are essentially the same \
  pattern (e.g. the same key substring appears), treat it as matching too, per the same "approval" \
  (auto/human) the matching condition specifies.
- If BOTH a SOP and log/file evidence are present, cross-check them: the evidence should support the \
  failure mode the SOP describes. If they agree, your confidence can be higher. If they conflict (e.g. \
  the SOP is for connection exhaustion but the logs show an out-of-memory error), say so in your \
  rationale and lower your confidence or fall back to insufficient_information if the conflict is \
  significant.
- If ONLY a SOP is present (no logs): DO NOT decline with insufficient_information merely because logs \
  are unavailable. Instead, recommend the SOP's primary corrective action from its resolution_steps - \
  the actual remediation action the SOP is designed to perform (e.g. "retry_glue_job" for a Glue SOP), \
  NOT its generic escalation step (e.g. "manual_review") unless the SOP genuinely has no other resolution \
  step available. Since the specific failure cause could not be confirmed by logs, you MUST always set \
  "requires_human_approval" to true for this case regardless of confidence, so a human reviews it before \
  it runs - see the matching anti-hallucination rule below for the exact required fields.
- If ONLY logs are present (no SOP matched), you may still recommend an action if the log content \
  clearly indicates a known, safe, standard fix (e.g. a plain connection/timeout error suggesting a \
  restart) - but you may ONLY choose from this fixed action registry in that case, and must extract \
  its required parameters from the logs/ticket the same way you would for a SOP-backed recommendation: \
  restart_ecs_service, restart_rds_instance, restart_ec2_instance, restart_lambda, retry_step_function, \
  retry_glue_job. Do not invent action names outside this registry or a matched SOP's resolution_steps.
- If NEITHER a SOP nor logs are present, you will not be called at all for this ticket - the orchestrating \
  code short-circuits straight to insufficient_information without invoking you, so you will never see \
  an empty-of-both-sources case in practice.

Anti-hallucination rules - follow these strictly:
- You may ONLY recommend an action that is literally listed in the matched SOP's resolution_steps, or - \
  when no SOP matched but logs are available - one of the fixed registry actions listed above. Never \
  invent an action name.
- Extract action parameters ONLY from information actually present in the incident text, CI name, or \
  CloudWatch log content given to you (e.g. an ECS cluster name, an RDS instance identifier, an EC2 \
  instance ID, a Lambda function name, a Step Functions state machine ARN, a Glue job name). NEVER \
  invent, guess, or fabricate a resource identifier - a made-up cluster/instance/ARN would cause an \
  action to run against the wrong (or a nonexistent) AWS resource, which is far worse than declining.
- If what you have (SOP and/or logs and/or similar-incident history) doesn't actually support a specific, \
  confident action - even though at least one source was technically available - you do NOT have enough \
  information to make a safe recommendation. In that case:
    - set "insufficient_information" to true
    - set "action" to null
    - set "action_parameters" to {{}}
    - set "confidence" to 0.0
    - set "requires_human_approval" to true
    - set "rationale" to a short explanation starting with "I do not have enough information to \
      recommend a remediation action because ..." (say specifically what's missing or why the available \
      sources don't resolve to a clear action)
  This is the CORRECT and PREFERRED behavior whenever you're not genuinely confident.
- If the matched SOP itself is marked auto_resolvable=false, or its risk_level is HIGH, you should \
  still recommend the best action from resolution_steps if one clearly applies, but set \
  "requires_human_approval" to true regardless of your confidence - some job classes always need a \
  human in the loop by policy, not because the AI is unsure.
- If a SOP matched but NO log/file evidence was available (CloudWatch or S3): still recommend the SOP's \
  primary corrective action from resolution_steps (do NOT set insufficient_information=true, do NOT set \
  action to null, solely because evidence is missing) - set:
    - "action" to the SOP's actual remediation action (not its generic escalation step, unless that is \
      truly the only resolution step it has)
    - "action_parameters" extracted from the ticket text/CI/SOP job_name as usual (logs are not required \
      to extract these - e.g. a Glue job_name is already known from the SOP/ticket, not the logs)
    - "requires_human_approval" to true (always, for this case - unconfirmed by logs)
    - "confidence" per the calibration bands below (a real SOP match is real signal even without logs - \
      do not zero it out)
    - "rationale" stating explicitly that no CloudWatch log evidence was available to confirm the \
      specific failure cause, AND briefly describing what the recommended action will do (its \
      remediation steps) so a human approver has enough information to decide without re-deriving it \
      themselves
  This lets a human quickly approve/reject a concrete, well-formed suggestion instead of receiving a bare \
  "insufficient information" with nothing to act on.
- Confidence calibration - use these concrete anchors instead of a vague gut feeling (float 0.0-1.0):
    - 0.85-1.0: log content directly and unambiguously satisfies a SOP's documented match rule/decision \
      tree condition (or, logs-only, the log content is an exact, well-known standard failure pattern), \
      AND all required action parameters were cleanly extracted.
    - 0.5-0.84: a SOP matched and available evidence is consistent with it but not an exact/literal match \
      (e.g. a SOP matched but no logs were available to confirm the specific failure cause - still real \
      signal, just unconfirmed; or logs only partially confirm the pattern), or a required parameter was \
      extracted with minor ambiguity.
    - Below 0.5, or insufficient_information: no rule can actually be verified from what you have, the \
      evidence is ambiguous or conflicting, or a required parameter is missing. Prefer \
      insufficient_information over a low-confidence guess whenever you're this unsure.
  Also weigh how completely you extracted required parameters and the historical pattern of similar job \
  failures into where in these bands you land.
- Always return strict JSON with exactly these keys: job_name (string or null), action (string or \
  null), action_parameters (object - string keys/values, empty object if none extracted), \
  confidence (float), risk_level (string, copy from the SOP if one matched, else your best estimate or \
  null), rationale (string), requires_human_approval (boolean), insufficient_information (boolean).
"""


class JobRemediationAgent:
    def __init__(
        self,
        vector_store: VectorStore,
        llm_client: "AgentLLMClient",
        anthropic_settings: "AnthropicSettings",
        sop_store: SOPStore,
        cloudwatch_client: Optional[CloudWatchLogsClient] = None,
        s3_client: Optional[S3Client] = None,
        top_k: int = 5,
    ):
        self.vector_store = vector_store
        self.llm_client = llm_client
        self.model = anthropic_settings.model_job_classifier
        self.sop_store = sop_store
        self.cloudwatch_client = cloudwatch_client
        self.s3_client = s3_client
        self.top_k = top_k

    def _matched_sop(self, ticket: Ticket) -> Optional[SOP]:
        text = f"{ticket.short_description}\n{ticket.description}\n{ticket.cmdb_ci_name or ''}"
        return self.sop_store.match(text)

    @staticmethod
    def _render_sop_block(sop: SOP, required_params_block: str) -> str:
        """
        Renders the matched SOP's full policy - including, when present,
        its literal match_rule/decision_tree/example_log_events - so the
        LLM sees the SOP's actual documented rule verbatim instead of a
        paraphrased description. These fields are optional (only
        SOP-GLUE-CETL-501 defines them today); SOPs without them just
        render the fields that exist, unchanged from before.
        """
        lines = [
            f"SOP {sop.sop_id} - {sop.title}",
            f"Service: {sop.service or 'unspecified'}",
            f"Description: {sop.description}",
        ]
        if sop.error_bucket:
            lines.append(f"Error bucket (job run log files): {sop.error_bucket}")
        if sop.keyword_bucket:
            lines.append(f"Keyword bucket (input/processed/error folders): {sop.keyword_bucket}")
        if sop.match_rule:
            lines.append(f"Match rule (apply this literally, verbatim): {sop.match_rule}")
        if sop.decision_tree:
            lines.append("Decision tree conditions:")
            for step in sop.decision_tree:
                for cond in step.get("conditions", []):
                    note = f" ({cond['note']})" if cond.get("note") else ""
                    lines.append(
                        f"  - if {cond.get('if', '')} -> action: {cond.get('action', '')} "
                        f"(approval: {cond.get('approval', 'unspecified')}){note}"
                    )
        if sop.example_log_events:
            lines.append("Worked example(s) of a matching log event:")
            for ex in sop.example_log_events:
                if ex.get("description"):
                    lines.append(f"  - Example: {ex['description']}")
                lines.append(f"    Raw event: {str(ex.get('raw_event', ''))[:500]}")
                lines.append(f"    -> matched_action: {ex.get('matched_action', '')}. Why: {ex.get('why', '')}")
        lines.append(f"Allowed resolution_steps and their required parameters:\n{required_params_block}")
        lines.append(f"auto_resolvable: {sop.auto_resolvable}")
        lines.append(f"risk_level: {sop.risk_level}")
        lines.append(f"blocked_actions: {sop.blocked_actions}")
        lines.append(f"notes: {sop.feedback_notes}")
        return "\n".join(lines)

    @staticmethod
    def _extract_identifier_tokens(text: str) -> List[str]:
        """
        Pulls out resource-identifier-shaped tokens (hyphenated/underscored
        words like "agentic-dev-customer-etl-pipeline") from free text.
        These are far better CloudWatch log-group search hints than a
        whole sentence, since AWS resource/job names are almost always
        written this way.
        """
        import re
        tokens = re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+", text or "")
        # Longer, more specific tokens first - more likely to be the actual
        # resource name rather than an incidental hyphenated word.
        return sorted(set(tokens), key=len, reverse=True)

    def _fetch_logs(self, ticket: Ticket, sop: Optional[SOP]) -> List[str]:
        if not self.cloudwatch_client:
            return []

        # Priority order: SOP's declared job_name (most trustworthy - it's
        # documented procedure knowledge, not a guess), the ticket's CI
        # name (usually already a clean resource/job identifier), then
        # identifier-shaped tokens extracted from the free-text description
        # (never the raw sentence itself - see cloudwatch_client.py docstring
        # for why that was the main cause of failed lookups).
        hints: List[str] = []
        if sop and sop.job_name:
            hints.append(sop.job_name)
        if ticket.cmdb_ci_name:
            hints.append(ticket.cmdb_ci_name)
        hints.extend(self._extract_identifier_tokens(f"{ticket.short_description} {ticket.description}"))

        service_hint = sop.service if sop else ""
        explicit_log_group = sop.log_group if sop else ""

        try:
            return self.cloudwatch_client.fetch_logs_for(
                hints, service_hint=service_hint, explicit_log_group=explicit_log_group
            )
        except Exception:
            logger.warning("CloudWatch log fetch failed for %s - proceeding without logs.", ticket.number,
                            exc_info=True)
            return []

    # Fixed convention (not per-SOP configurable) - see SOP-EAM-MMSO-701.
    _KEYWORD_BUCKET_FOLDERS = ("error/", "input/")

    def _fetch_s3_context(self, ticket: Ticket, sop: Optional[SOP]) -> str:
        """
        For jobs diagnosed from S3 rather than CloudWatch (e.g. the
        Enrollments MMS job): fetches the SOP's error_bucket's latest log
        file (expected to contain a step-number failure marker) and the
        SOP's keyword_bucket's error/ and input/ folder listings. Returns
        an empty string if no S3 client is configured, no SOP matched, or
        the SOP doesn't declare either bucket - a normal, expected outcome
        for every other (CloudWatch-based) SOP.
        """
        if not self.s3_client or not sop:
            return ""

        pieces: List[str] = []
        if sop.error_bucket:
            log_text = self.s3_client.get_latest_object_text(sop.error_bucket)
            if log_text:
                pieces.append(f"Latest log file in error bucket '{sop.error_bucket}':\n{log_text[:2000]}")

        if sop.keyword_bucket:
            for folder in self._KEYWORD_BUCKET_FOLDERS:
                files = self.s3_client.list_recent_objects(sop.keyword_bucket, prefix=folder)
                file_list = [f["key"] for f in files] if files else "(none - folder is empty)"
                pieces.append(
                    f"Keyword bucket '{sop.keyword_bucket}' - {folder} folder recent files "
                    f"({len(files)} found): {file_list}"
                )

        return "\n\n".join(pieces)

    def get_remediation(self, ticket: Ticket) -> RemediationRecommendation:
        sop = self._matched_sop(ticket)
        log_events = self._fetch_logs(ticket, sop)
        s3_context = self._fetch_s3_context(ticket, sop)

        sop_available = sop is not None
        logs_available = bool(log_events) or bool(s3_context)

        # DECISION RULE: need at least one grounding source. Neither -> skip
        # the LLM call entirely and go straight to human intervention.
        if not sop_available and not logs_available:
            logger.warning(
                "Neither a matching SOP nor log/file evidence is available for %s - "
                "sending straight to human intervention without an LLM call.",
                ticket.number,
            )
            return RemediationRecommendation(
                ticket_number=ticket.number,
                action=None,
                confidence=0.0,
                requires_human_approval=True,
                insufficient_information=True,
                rationale=(
                    "I do not have enough information to recommend a remediation action because "
                    "no matching SOP was found and no log/file evidence (CloudWatch or S3) was found "
                    "for this job/resource. At least one of these is required before I can make a "
                    "safe recommendation."
                ),
            )

        embedding_text = ticket.to_embedding_text()
        similar_raw = self.vector_store.search(embedding_text, top_k=self.top_k, source_filter="job_failure")
        similar = [
            SimilarJobFailure(
                number=s.number, short_description=s.short_description,
                action_taken=s.assignment_group,  # reuses the same payload field name in the shared collection
                outcome=s.close_notes, score=s.score,
            )
            for s in similar_raw
        ]

        if sop:
            required_params_block = "\n".join(
                f"  - {action}: requires {REQUIRED_PARAMS.get(action, [])}" for action in sop.resolution_steps
            )
            sop_block = self._render_sop_block(sop, required_params_block)
        else:
            sop_block = "No matching SOP was found for this job/resource type."

        evidence_pieces = []
        if log_events:
            evidence_pieces.append("\n".join(f"  {line[:300]}" for line in log_events[-20:]))  # most recent 20
        if s3_context:
            evidence_pieces.append(s3_context)
        logs_block = "\n\n".join(evidence_pieces) if evidence_pieces else (
            "No CloudWatch log events or S3-based evidence were found for this job/resource."
        )

        similar_block = "\n".join(
            f"- {s.number}: \"{s.short_description}\" -> action taken: {s.action_taken} "
            f"(similarity={s.score:.3f}). Outcome notes: {(s.outcome or '')[:150]}"
            for s in similar
        ) or "No similar historical job failures found."

        user_prompt = f"""Incident to remediate:
Number: {ticket.number}
Short description: {ticket.short_description}
Description: {ticket.description}
Configuration Item: {ticket.cmdb_ci_name or "unknown"}

Matched SOP:
{sop_block}

Recent log/file evidence (CloudWatch log events most-recent-last, and/or S3-based evidence for S3-driven jobs):
{logs_block}

Similar historical job failures:
{similar_block}

Recommend a remediation action per your instructions, extracting only parameters that are actually \
present in the incident text/CI/logs above. Return JSON with: job_name, action, action_parameters, \
confidence, risk_level, rationale, requires_human_approval, insufficient_information."""

        # Low (not zero) temperature: this is a deterministic classification/
        # extraction task, not creative generation - reducing run-to-run
        # variance directly improves confidence-score consistency, while
        # still leaving some room for natural rationale phrasing.
        result = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt, model=self.model, temperature=0.1)

        insufficient = bool(result.get("insufficient_information", False))
        action = result.get("action") or None
        action_parameters = result.get("action_parameters") or {}
        if not isinstance(action_parameters, dict):
            action_parameters = {}
        action_parameters = {str(k): str(v) for k, v in action_parameters.items() if v}

        # Anti-hallucination backstop: if a SOP matched, the action must be
        # one of ITS resolution_steps; if no SOP matched (logs-only path),
        # the action must be one of the fixed registry actions.
        allowed_actions = sop.resolution_steps if sop else list(REQUIRED_PARAMS.keys())
        if action and action not in allowed_actions:
            logger.warning(
                "Model recommended action '%s' for %s which is outside the allowed set %s - "
                "overriding to insufficient_information.",
                action, ticket.number, allowed_actions,
            )
            insufficient = True

        if insufficient or not action:
            insufficient = True
            action = None
            action_parameters = {}
            confidence = 0.0
            requires_human_approval = True
            missing_bits = []
            if not sop_available:
                missing_bits.append("no matching SOP")
            if not logs_available:
                missing_bits.append("no CloudWatch logs")
            rationale = result.get("rationale") or (
                "I do not have enough information to recommend a remediation action confidently "
                f"({', '.join(missing_bits) if missing_bits else 'the available SOP/log evidence did not resolve to a clear action'})."
            )
        else:
            confidence = float(result.get("confidence", 0.0))
            if sop_available and not logs_available and confidence <= 0.0:
                # A real SOP match is real signal even without log confirmation
                # (see SYSTEM_PROMPT's confidence calibration bands) - a
                # concrete action recommendation paired with a literal 0.0
                # would misrepresent that signal as "no confidence at all",
                # so floor it to the bottom of the "SOP matched, evidence
                # not exact/confirmed" band rather than trust a stray 0.0
                # from the model.
                confidence = SOP_ONLY_NO_LOGS_MIN_CONFIDENCE
            requires_human_approval = bool(result.get("requires_human_approval", True))
            rationale = result.get("rationale", "")

        recommendation = RemediationRecommendation(
            ticket_number=ticket.number,
            job_name=result.get("job_name"),
            action=action,
            action_parameters=action_parameters,
            confidence=confidence,
            risk_level=(result.get("risk_level") or (sop.risk_level if sop else None)),
            rationale=rationale,
            requires_human_approval=requires_human_approval,
            insufficient_information=insufficient,
            sop_id=sop.sop_id if sop else None,
            sop_title=sop.title if sop else None,
            similar_job_failures=similar,
        )

        logger.info(
            "Remediation recommendation for %s: sop_available=%s logs_available=%s action=%s params=%s "
            "confidence=%.2f requires_human_approval=%s insufficient_information=%s sop=%s",
            ticket.number, sop_available, logs_available, recommendation.action, recommendation.action_parameters,
            recommendation.confidence, recommendation.requires_human_approval,
            recommendation.insufficient_information, recommendation.sop_id,
        )
        return recommendation
