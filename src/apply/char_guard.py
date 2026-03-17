"""ATS form field character-count guard.

Detects and warns when field values approach or exceed known ATS platform
character limits, preventing silent truncation during form submission.
"""
from dataclasses import dataclass
from typing import Optional, Dict

# Known ATS platform field character limits (conservative estimates)
ATS_FIELD_LIMITS: Dict[str, Dict[str, Optional[int]]] = {
    "workday": {"cover_letter": 32000, "work_summary": 4000, "default": 32000},
    "greenhouse": {"long_answer": 5000, "short_answer": 500, "default": 5000},
    "lever": {"default": 2000},
    "ashby": {"default": None},  # employer-configured
}

WARNING_THRESHOLD_PCT = 0.9  # warn at 90% of limit


@dataclass
class TruncationWarning:
    field_name: str
    intended_len: int
    limit: Optional[int]
    truncated: bool
    pct_of_limit: Optional[float]


def get_field_limit(ats_platform: str, field_type: str = "default") -> Optional[int]:
    """Look up the character limit for a given ATS platform and field type."""
    platform_limits = ATS_FIELD_LIMITS.get(ats_platform.lower(), {})
    return platform_limits.get(field_type, platform_limits.get("default"))


def check_truncation(
    field_name: str,
    value: str,
    ats_platform: str,
    field_type: str = "default",
    maxlength: Optional[int] = None,
) -> TruncationWarning:
    """Check if a field value risks truncation on the given ATS platform.

    Args:
        field_name: Name of the form field
        value: The text value to be entered
        ats_platform: ATS platform identifier (workday, greenhouse, lever, ashby)
        field_type: Type of field for platform-specific limit lookup
        maxlength: Optional DOM maxlength attribute override

    Returns:
        TruncationWarning with truncation assessment
    """
    intended_len = len(value)
    limit = maxlength or get_field_limit(ats_platform, field_type)

    if limit is None:
        return TruncationWarning(
            field_name=field_name,
            intended_len=intended_len,
            limit=None,
            truncated=False,
            pct_of_limit=None,
        )

    pct = intended_len / limit if limit > 0 else None
    truncated = intended_len > limit

    return TruncationWarning(
        field_name=field_name,
        intended_len=intended_len,
        limit=limit,
        truncated=truncated,
        pct_of_limit=round(pct, 3) if pct is not None else None,
    )


def measure_and_warn(
    field_name: str,
    value: str,
    ats_platform: str,
    field_type: str = "default",
    maxlength: Optional[int] = None,
) -> TruncationWarning:
    """Convenience wrapper: check truncation and return warning with risk flag.

    Use this as the primary entry point for the truncation guard.
    """
    return check_truncation(field_name, value, ats_platform, field_type, maxlength)
