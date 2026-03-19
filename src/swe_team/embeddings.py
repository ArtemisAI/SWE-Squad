"""
Embedding helper for SWE Squad semantic memory.

Uses the configured embedding model via the existing LLM proxy and returns
``None`` on failures so callers can treat semantic memory as best-effort.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from src.swe_team.models import SWETicket

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "bge-m3"
_EXTRACTION_ERROR_LOG_CHARS = 1200
_EXTRACTION_INVESTIGATION_CHARS = 2400
_EXTRACTION_FIX_CHARS = 1200


def _ticket_text(ticket: SWETicket) -> str:
    return (
        f"Title: {ticket.title}\n"
        f"Module: {ticket.source_module or 'unknown'}\n"
        f"Error: {(ticket.error_log or '')[:500]}\n"
        f"Investigation: {(ticket.investigation_report or '')[:1000]}"
    )


def embed_ticket(ticket: SWETicket) -> Optional[list[float]]:
    """Return an embedding for *ticket* or ``None`` when unavailable."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised via failure fallback
        logger.warning("openai package unavailable for embeddings (non-fatal): %s", exc)
        return None

    model = os.getenv("EMBEDDING_MODEL", _DEFAULT_MODEL)
    api_url = os.getenv("EMBEDDING_API_URL") or os.getenv("BASE_LLM_API_URL")
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("BASE_LLM_API_KEY", "")

    if not api_url or not api_key:
        logger.warning("Embedding API URL/key missing (non-fatal)")
        return None

    try:
        embedding_input = (
            extract_memory_facts(ticket)
            if ticket.investigation_report
            else _ticket_text(ticket)
        )
        # Short timeout — embeddings are best-effort; 504s should fail fast
        # not burn 60s per retry (3 retries × 60s = 3min blocked per cycle).
        client = OpenAI(base_url=api_url, api_key=api_key, timeout=10.0, max_retries=1)
        resp = client.embeddings.create(
            input=embedding_input,
            model=model,
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.warning("embed_ticket failed (non-fatal): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Memory extraction (mem0-style) — see issue #6
# ---------------------------------------------------------------------------
# NOTE on model selection:
#   BASE_LLM_API_URL  = external OpenAI-compatible proxy (your-llm-proxy.example.com)
#                       Use this for cheap text extraction calls.
#                       Available T1 models: gemini-3-flash, qwen3:8b
#   Claude Code CLI   = used by SWE Squad AGENTS (investigator, developer)
#                       via subprocess. NOT the same as BASE_LLM.
#                       Do NOT call claude CLI from within library code.

_DEFAULT_EXTRACTION_MODEL = "gemini-3-flash"


def extract_memory_facts(ticket: SWETicket) -> str:
    """Distil a resolved ticket into a compact normalised memory fact.

    Uses a cheap T1 model (gemini-3-flash) via the BASE_LLM proxy
    to strip noise from raw ticket text before embedding, following the
    mem0 pattern of structured fact extraction.

    Only called when ``ticket.investigation_report`` is non-empty (i.e. the
    ticket is resolved/investigation_complete). Falls back to ``_ticket_text``
    on any failure — non-fatal.

    Output format:
    - Root cause (1 sentence)
    - Fix applied (1-2 sentences)
    - Affected module
    - Tags: error type, fix pattern
    """
    if not ticket.investigation_report:
        return _ticket_text(ticket)

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised via failure fallback
        logger.warning("openai package unavailable for fact extraction (non-fatal): %s", exc)
        return _ticket_text(ticket)

    api_url = os.getenv("BASE_LLM_API_URL")
    api_key = os.getenv("BASE_LLM_API_KEY", "")
    model = os.getenv("EXTRACTION_MODEL", _DEFAULT_EXTRACTION_MODEL)
    if not api_url:
        logger.warning("BASE_LLM_API_URL missing for fact extraction (non-fatal)")
        return _ticket_text(ticket)

    prompt = (
        "You are extracting reusable software incident memory facts.\n"
        "Produce compact plain text with these exact headings:\n"
        "Root cause:\n"
        "Fix applied:\n"
        "Affected module:\n"
        "Tags:\n\n"
        "Constraints:\n"
        "- Root cause: exactly 1 sentence.\n"
        "- Fix applied: 1-2 sentences.\n"
        "- Affected module: short value.\n"
        "- Tags: comma-separated keywords for error type and fix pattern.\n"
        "- Do not include markdown fences or extra sections.\n\n"
        f"Title: {ticket.title}\n"
        f"Module: {ticket.source_module or 'unknown'}\n"
        f"Error log: {(ticket.error_log or '')[:_EXTRACTION_ERROR_LOG_CHARS]}\n"
        f"Investigation report: {(ticket.investigation_report or '')[:_EXTRACTION_INVESTIGATION_CHARS]}\n"
        f"Proposed fix: {(ticket.proposed_fix or '')[:_EXTRACTION_FIX_CHARS]}\n"
    )

    try:
        client = OpenAI(base_url=api_url, api_key=api_key, timeout=15.0, max_retries=1)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        # Cache facts in ticket metadata for edge extraction
        if content:
            ticket.metadata["memory_facts"] = content
        return content or _ticket_text(ticket)
    except Exception as exc:
        logger.warning("extract_memory_facts failed (non-fatal): %s", exc)
        return _ticket_text(ticket)


# ---------------------------------------------------------------------------
# Knowledge graph edge extraction — see knowledge_store.py
# ---------------------------------------------------------------------------


def extract_edges_from_ticket(
    ticket: SWETicket,
    similar_tickets: list[dict[str, Any]] | None = None,
    *,
    similarity_edge_threshold: float = 0.80,
) -> list["KnowledgeEdge"]:
    """Extract knowledge graph edges from a resolved ticket.

    Called after embedding + similarity search. Creates:
    - 'similar' edges for matches above threshold
    - 'touches_module' edge if source_module is known

    Returns edges (caller is responsible for persisting them).
    Best-effort — returns empty list on failure.
    """
    from src.swe_team.models import KnowledgeEdge, EdgeType

    edges: list[KnowledgeEdge] = []

    # Similar ticket edges
    if similar_tickets:
        for match in similar_tickets:
            raw_sim = float(match.get("raw_similarity", match.get("similarity", 0)))
            if raw_sim >= similarity_edge_threshold:
                target_id = str(match.get("ticket_id", ""))
                if target_id and target_id != ticket.ticket_id:
                    edges.append(KnowledgeEdge(
                        source_id=ticket.ticket_id,
                        target_id=target_id,
                        edge_type=EdgeType.SIMILAR,
                        confidence=raw_sim,
                        discovered_by="embedding",
                    ))

    # Module edge
    module = ticket.source_module
    if not module:
        # Try to extract from memory facts
        facts = ticket.metadata.get("memory_facts", "")
        if "Affected module:" in facts:
            for line in facts.split("\n"):
                if line.strip().startswith("Affected module:"):
                    module = line.split(":", 1)[1].strip()
                    break

    if module and module.lower() not in ("unknown", "none", ""):
        edges.append(KnowledgeEdge(
            source_id=ticket.ticket_id,
            target_id=module,
            edge_type=EdgeType.TOUCHES_MODULE,
            confidence=1.0,
            discovered_by="fact_extraction",
        ))

    return edges
