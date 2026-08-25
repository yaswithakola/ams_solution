"""
Incident Router AI Agent
========================
Input : incident short_description, description, and CI (from a Ticket)
Process:
    1. Embed the incident text (BGE-M3) and search the shared Qdrant
       vector database for similar historically-resolved incidents.
    2. Pass the incident + retrieved matches as a prompt to an Anthropic
       LLM model, asking it to pick the correct L2 assignment group and
       give a confidence score.
Output: RoutingResult (assignment_group, confidence, rationale) which is
        sent back to the AMS Orchestrator AI Agent. If the model does not
        have enough grounding to make a confident call, it explicitly says
        so (insufficient_information=True) instead of guessing.
"""
import logging
from typing import TYPE_CHECKING

from common.models import RoutingResult, Ticket
from common.vector_db import VectorStore

if TYPE_CHECKING:
    from common.llm_base import AgentLLMClient
    from config import AnthropicSettings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Incident Router AI Agent - a specialized production-support routing \
agent for an Application Managed Services (AMS) team. Your one and only job is to decide the correct \
L2 support assignment group for a newly raised ServiceNow incident, using historically similar \
resolved incidents as grounding context. You do not perform any other task, and you do not answer \
questions unrelated to routing this incident.

Anti-hallucination rules - follow these strictly:
- Only ever choose an assignment group that literally appears in the similar-incidents context given \
  to you. Never invent, guess, or paraphrase a plausible-sounding group name that isn't present there.
- If the similar incidents are too few, too dissimilar to the new incident, or disagree with each \
  other on the assignment group, you do NOT have enough information to make a confident decision. \
  In that case you must NOT produce a best-effort guess. Instead:
    - set "insufficient_information" to true
    - set "assignment_group" to null
    - set "confidence" to 0.0
    - set "rationale" to a short explanation starting with "I do not have enough information to route \
      this incident because ..." (state specifically what's missing - e.g. no similar incidents found, \
      similar incidents point to conflicting groups, the CI/description is too vague, etc.)
  Saying "I do not have enough information" is the CORRECT and PREFERRED behavior whenever you are not \
  genuinely confident - it is always better than a fabricated recommendation, because a wrong guess \
  routed to the wrong team causes real delay in a production incident.
- Only when the similar incidents clearly and consistently support one specific group should you set \
  "insufficient_information" to false and return that group with a genuine confidence score.
- Confidence must honestly reflect the strength of the evidence: strong agreement across multiple \
  closely-matching similar incidents earns a high confidence; weak, partial, or conflicting evidence \
  earns a low confidence (and, per the rule above, sufficiently weak evidence should instead be reported \
  as insufficient_information=true rather than a low-confidence guess).
- Confidence is a float between 0.0 and 1.0.
- Always return strict JSON with exactly these keys: assignment_group (string or null), \
  confidence (float), rationale (string), insufficient_information (boolean).
"""


class IncidentRouterAgent:
    def __init__(self, vector_store: VectorStore, llm_client: "AgentLLMClient",
                 anthropic_settings: "AnthropicSettings", top_k: int = 5):
        self.vector_store = vector_store
        self.llm_client = llm_client
        self.model = anthropic_settings.model_router
        self.top_k = top_k

    def route(self, ticket: Ticket) -> RoutingResult:
        embedding_text = ticket.to_embedding_text()
        similar = self.vector_store.search(embedding_text, top_k=self.top_k, source_filter="incident")

        context_lines = []
        for s in similar:
            context_lines.append(
                f"- Incident {s.number} (similarity={s.score:.3f}): \"{s.short_description}\" "
                f"-> resolved by group: {s.assignment_group}. Notes: {(s.close_notes or '')[:200]}"
            )
        context_block = "\n".join(context_lines) if context_lines else "No similar historical incidents found."

        user_prompt = f"""New incident to route:
Number: {ticket.number}
Short description: {ticket.short_description}
Description: {ticket.description}
Configuration Item: {ticket.cmdb_ci_name or "unknown"}

Similar historical incidents retrieved from the vector database:
{context_block}

Decide the correct assignment group per your instructions. Return JSON with: assignment_group \
(string or null), confidence (0.0-1.0), rationale, insufficient_information (boolean)."""

        result = self.llm_client.complete_json(SYSTEM_PROMPT, user_prompt, model=self.model)

        insufficient = bool(result.get("insufficient_information", False))
        assignment_group = result.get("assignment_group") or None
        if insufficient or not assignment_group:
            insufficient = True
            assignment_group = None
            confidence = 0.0
            rationale = result.get("rationale") or (
                "I do not have enough information to route this incident because no sufficiently "
                "similar or consistent historical incidents were found."
            )
        else:
            confidence = float(result.get("confidence", 0.0))
            rationale = result.get("rationale", "")

        routing_result = RoutingResult(
            ticket_number=ticket.number,
            assignment_group=assignment_group,
            confidence=confidence,
            rationale=rationale,
            insufficient_information=insufficient,
            similar_incidents=similar,
        )

        if insufficient:
            logger.warning("Insufficient information to route %s: %s", ticket.number, rationale)
        else:
            logger.info(
                "Routed %s -> %s (confidence=%.2f)",
                ticket.number, routing_result.assignment_group, routing_result.confidence,
            )
        return routing_result
