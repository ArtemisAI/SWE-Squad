"""
src/apply/field_classifier.py — EEO/demographic field classification.

Identifies form fields that collect sensitive EEO or demographic information
so that the HITL gate can block automated filling of those fields.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


# ---------------------------------------------------------------------------
# Keyword corpus
# ---------------------------------------------------------------------------
# Each entry is a regex pattern string applied case-insensitively.
# Multi-word phrases use literal spaces (the label text is searched verbatim).
# Single short words that could create false positives (e.g. "sex" matching
# "section", "age" matching "message") use \b word-boundaries — note that
# regex \b does NOT fire between a letter and an underscore, so field_name
# matching uses a separate token-splitting path (see classify_field).

EEO_FIELD_KEYWORDS: List[str] = [
    # Gender / sex
    r"\bsex\b",           # word-boundary: avoids "section", "sexual" handled separately
    "gender",
    r"\bmale\b",
    r"\bfemale\b",
    "non-binary",
    "nonbinary",
    "transgender",
    "genderqueer",
    "gender identity",
    "gender expression",
    "pronouns",
    "he/him",
    "she/her",
    "they/them",
    # Race / ethnicity
    "race",               # no boundary needed — label context makes it specific
    "ethnicity",
    "ethnic",
    "racial",
    "hispanic",
    "latino",
    "latina",
    "latinx",
    r"\basian\b",
    r"\bblack\b",
    "african american",
    "african-american",
    r"\bwhite\b",
    "caucasian",
    "native american",
    "native hawaiian",
    "pacific islander",
    "alaska native",
    "multiracial",
    "mixed race",
    "two or more races",
    # National origin / citizenship
    "national origin",
    "nationality",
    "country of origin",
    "citizenship",
    "immigration status",
    "visa status",
    "work authorization",
    "authorized to work",
    # Disability
    "disability",
    "disabled",
    "impairment",
    r"\bada\b",
    "section 503",
    "section 504",
    "physical condition",
    "mental condition",
    "chronic illness",
    # Veteran / military
    "veteran",
    "military",
    "armed forces",
    "military service",
    "military status",
    "active duty",
    "service member",
    "protected veteran",
    "recently separated",
    "special disabled veteran",
    # Catch-all / regulatory phrases
    r"\beeo\b",
    "equal employment",
    "equal opportunity",
    "affirmative action",
    "demographic",
    "self-identify",
    "self identify",
    "self-identification",
    "self identification",
    "protected class",
    "protected characteristic",
    "protected status",
    "voluntary disclosure",
    "voluntary self",
    # Age (sometimes captured in EEO contexts)
    "date of birth",
    "birth date",
    # Religion
    "religion",
    "religious",
    # Sexual orientation
    "sexual orientation",
    "lgbtq",
    "lgbt",
    "bisexual",
    "homosexual",
    r"\bgay\b",
    "lesbian",
    # Pregnancy / family status
    "pregnancy",
    "pregnant",
    "parental status",
    "marital status",
    "family status",
]

# Pre-compiled pattern for label/verbose-text matching.
# Keywords that already contain regex meta-characters are used as-is;
# plain strings are re.escape()-d so literal punctuation is handled correctly.
def _build_label_pattern(keywords: List[str]) -> re.Pattern[str]:
    parts: List[str] = []
    for kw in keywords:
        has_meta = any(c in kw for c in r"\b()[]^$|.*+?{}")
        parts.append(kw if has_meta else re.escape(kw))
    return re.compile("|".join(parts), re.IGNORECASE)


_LABEL_PATTERN: re.Pattern[str] = _build_label_pattern(EEO_FIELD_KEYWORDS)

# For field_name token matching we split on underscores / hyphens / spaces,
# then run each token (and adjacent token pairs) through the same pattern.
# This means "q_race_field" → tokens ["q", "race", "field"] → "race" matches.
_TOKEN_SEP: re.Pattern[str] = re.compile(r"[_\-\s]+")


def _match_field_name(field_name: str) -> Optional[str]:
    """Return the first matching keyword found in *field_name* tokens, or None.

    Splitting on ``_``, ``-``, and whitespace lets us catch EEO keywords that
    are embedded in underscore_separated or kebab-case identifiers — for
    example ``q_race_field`` → ``race`` matches, while ``section_id`` → no
    match (``section`` is not a keyword; the guard lives in the corpus instead).
    """
    tokens = _TOKEN_SEP.split(field_name)
    # Check individual tokens
    for tok in tokens:
        m = _LABEL_PATTERN.fullmatch(tok)
        if m:
            return m.group(0).lower()
    # Check adjacent pairs (handles two-word phrases split across tokens)
    for i in range(len(tokens) - 1):
        pair = tokens[i] + " " + tokens[i + 1]
        m = _LABEL_PATTERN.search(pair)
        if m:
            return m.group(0).lower()
    # Final fallback: run the full pattern on the raw name string
    # (catches things like "eeorace" or names with no separator)
    m = _LABEL_PATTERN.search(field_name)
    if m:
        return m.group(0).lower()
    return None


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class FieldClassification:
    """Result of classifying a single form field."""

    field_name: str
    field_label: str
    is_eeo: bool
    matched_keyword: Optional[str]
    confidence: float  # 0.0–1.0; 1.0 when an exact keyword phrase matches
    max_length: Optional[int] = None  # DOM maxlength attribute, if known


# ---------------------------------------------------------------------------
# Core classification logic
# ---------------------------------------------------------------------------

def classify_field(field_name: str, field_label: str) -> FieldClassification:
    """Classify a single form field as EEO-sensitive or safe.

    Performs case-insensitive matching against *EEO_FIELD_KEYWORDS* on both
    *field_label* and *field_name*.  The label receives higher weight because
    it reflects the user-facing text that describes what is being collected;
    *field_name* (the HTML name/id attribute) is checked as a secondary signal
    using a token-splitting strategy so that underscore-separated identifiers
    like ``q_race_field`` are handled correctly.

    Confidence scoring:
      - 1.0  — keyword found in *field_label* (direct, explicit label match)
      - 0.85 — keyword found only in *field_name* (attribute-level hint)

    Args:
        field_name:  The programmatic name or id of the form field (e.g.
                     ``"eeo_race"`` or ``"vet_status"``).
        field_label: The human-readable label shown in the form (e.g.
                     ``"Do you identify as a veteran of the U.S. Armed Forces?"``).

    Returns:
        A :class:`FieldClassification` instance.
    """
    # 1. Check label first — highest-confidence signal.
    label_match = _LABEL_PATTERN.search(field_label)
    if label_match:
        return FieldClassification(
            field_name=field_name,
            field_label=field_label,
            is_eeo=True,
            matched_keyword=label_match.group(0).lower(),
            confidence=1.0,
        )

    # 2. Fall back to token-based field name inspection.
    name_keyword = _match_field_name(field_name)
    if name_keyword is not None:
        return FieldClassification(
            field_name=field_name,
            field_label=field_label,
            is_eeo=True,
            matched_keyword=name_keyword,
            confidence=0.85,
        )

    return FieldClassification(
        field_name=field_name,
        field_label=field_label,
        is_eeo=False,
        matched_keyword=None,
        confidence=1.0,
    )


def classify_fields(fields: List[dict]) -> List[FieldClassification]:
    """Classify a batch of form fields.

    Each element of *fields* must be a dict containing at minimum the keys
    ``"field_name"`` (str) and ``"label"`` (str).  Extra keys are ignored.

    Args:
        fields: List of field descriptor dicts.

    Returns:
        A list of :class:`FieldClassification` instances in the same order as
        the input.

    Raises:
        KeyError: If a field dict is missing the required ``"field_name"`` or
                  ``"label"`` keys.
    """
    return [
        classify_field(
            field_name=f["field_name"],
            field_label=f["label"],
        )
        for f in fields
    ]
