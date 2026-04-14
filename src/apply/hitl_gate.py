"""
src/apply/hitl_gate.py — Human-in-the-loop gate for EEO/demographic fields.

Before the application agent fills any form field, every field must pass
through this gate.  Fields classified as EEO-sensitive are quarantined in
a *blocked* list and must receive explicit human approval before proceeding.

No external dependencies — stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from src.apply.char_guard import measure_and_warn
from src.apply.field_classifier import FieldClassification, classify_field


@dataclass
class HITLReviewRequest:
    """A single field that requires human review before it may be filled.

    Attributes:
        job_id:          Identifier for the job application being processed.
        field_name:      Programmatic name/id of the blocked field.
        field_label:     Human-readable label of the blocked field.
        matched_keyword: The EEO keyword that triggered the block.
        requires_review: Always ``True`` for blocked fields; present for
                         downstream consumers that may serialise this object.
        human_response:  Set by the reviewing human to supply the approved
                         value, or ``None`` if review is still pending.
        reviewed:        ``True`` once a human has inspected this request
                         (even if they chose to leave *human_response* empty).
        char_count:      Optional character count of the field value, used for
                         ATS form truncation auditing (GH-188).
        truncation_risk: ``True`` when the field value is at risk of being
                         truncated by the ATS form submission layer.
    """

    job_id: str
    field_name: str
    field_label: str
    matched_keyword: str
    failure_reason: str = ""
    requires_review: bool = True
    human_response: Optional[str] = None
    reviewed: bool = False
    char_count: Optional[int] = None
    truncation_risk: bool = False

    def __post_init__(self) -> None:
        # Ensure failure_reason is always the matched keyword, never the
        # (potentially verbose) field_label which can contain recipe banner text.
        if not self.failure_reason:
            self.failure_reason = self.matched_keyword


@dataclass
class HITLGateResult:
    """Outcome of running the HITL gate over a set of form fields.

    Attributes:
        safe_fields:    Fields that were not classified as EEO-sensitive and
                        may be auto-filled.  Each element is the original
                        field dict passed to :func:`check_fields`.
        blocked_fields: Fields that were classified as EEO-sensitive and
                        must not be auto-filled without human review.
        all_clear:      ``True`` when *blocked_fields* is empty — i.e. the
                        agent may proceed to fill all fields automatically.
    """

    safe_fields: List[dict]
    blocked_fields: List[HITLReviewRequest]
    all_clear: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.all_clear = len(self.blocked_fields) == 0


def check_fields(
    job_id: str,
    fields: List[dict],
    classifier: Optional[Callable[[str, str], FieldClassification]] = None,
    ats_platform: Optional[str] = None,
) -> HITLGateResult:
    """Run EEO classification on every field and split into safe vs blocked.

    Args:
        job_id:     Identifier for the job application being processed.
                    Stored on each :class:`HITLReviewRequest` for traceability.
        fields:     List of form-field descriptor dicts.  Each dict must
                    contain at minimum ``"name"`` (str) and ``"label"`` (str)
                    keys.  All other keys are preserved unchanged in
                    ``HITLGateResult.safe_fields``.
        classifier: Optional replacement for :func:`classify_field`.  Useful
                    for testing or domain-specific overrides.  Must accept
                    ``(field_name: str, field_label: str)`` and return a
                    :class:`FieldClassification`.  Defaults to the standard
                    :func:`~src.apply.field_classifier.classify_field`.
        ats_platform: Optional ATS platform identifier (e.g. ``"workday"``,
                      ``"greenhouse"``).  When provided and a field dict
                      contains a ``"value"`` key, truncation risk is assessed
                      via :func:`~src.apply.char_guard.measure_and_warn` and
                      ``char_count`` / ``truncation_risk`` are populated on
                      blocked :class:`HITLReviewRequest` items.

    Returns:
        A :class:`HITLGateResult` with ``safe_fields``, ``blocked_fields``,
        and ``all_clear`` populated.

    Raises:
        KeyError: If a field dict is missing the required ``"name"`` or
                  ``"label"`` keys.
    """
    _classify = classifier if classifier is not None else classify_field

    safe: List[dict] = []
    blocked: List[HITLReviewRequest] = []

    for f in fields:
        name = f["name"]
        label = f["label"]
        value = f.get("value")

        classification = _classify(name, label)

        # Assess truncation risk when we have both a value and an ATS platform.
        char_count: Optional[int] = None
        truncation_risk = False
        if value is not None and ats_platform is not None:
            field_type = f.get("field_type", "default")
            maxlength = f.get("maxlength")
            warning = measure_and_warn(
                name, value, ats_platform, field_type, maxlength,
            )
            char_count = warning.intended_len
            truncation_risk = warning.truncated

        if classification.is_eeo:
            blocked.append(
                HITLReviewRequest(
                    job_id=job_id,
                    field_name=name,
                    field_label=label,
                    matched_keyword=classification.matched_keyword or "",
                    char_count=char_count,
                    truncation_risk=truncation_risk,
                )
            )
        else:
            safe.append(f)

    return HITLGateResult(safe_fields=safe, blocked_fields=blocked)


def format_hitl_alert(result: HITLGateResult) -> str:
    """Format a human-readable alert message for a non-clear HITL gate result.

    Suitable for display in a terminal, Telegram message, or log entry.
    When ``result.all_clear`` is ``True`` a short "all-clear" notice is
    returned instead.

    Args:
        result: The :class:`HITLGateResult` to format.

    Returns:
        A plain-text (no HTML) multi-line string.
    """
    if result.all_clear:
        return (
            f"[HITL Gate] All clear — no EEO/demographic fields detected. "
            f"{len(result.safe_fields)} field(s) approved for auto-fill."
        )

    lines = [
        "[HITL Gate] BLOCKED — EEO/demographic fields require human review.",
        "",
        f"  Safe fields (auto-fill approved): {len(result.safe_fields)}",
        f"  Blocked fields (human review required): {len(result.blocked_fields)}",
        "",
        "Blocked fields:",
    ]

    for i, req in enumerate(result.blocked_fields, start=1):
        lines.append(
            f"  {i}. [{req.field_name}] \"{req.field_label}\"  (matched: \"{req.matched_keyword}\")"
        )

    lines += [
        "",
        "Action required: a human must review and supply responses for all",
        "blocked fields before the application agent may proceed.",
    ]

    return "\n".join(lines)
