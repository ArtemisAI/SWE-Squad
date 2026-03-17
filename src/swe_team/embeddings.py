"""
Embedding helper for SWE Squad semantic memory.

Uses the configured embedding model via the existing LLM proxy and returns
``None`` on failures so callers can treat semantic memory as best-effort.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from src.swe_team.models import SWETicket

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "bge-m3"


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
        client = OpenAI(base_url=api_url, api_key=api_key)
        resp = client.embeddings.create(
            input=_ticket_text(ticket),
            model=model,
        )
        return resp.data[0].embedding
    except Exception as exc:
        logger.warning("embed_ticket failed (non-fatal): %s", exc)
        return None
