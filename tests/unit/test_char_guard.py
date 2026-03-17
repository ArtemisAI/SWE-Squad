"""Tests for src/apply/char_guard.py — ATS field truncation guard."""
import pytest
from src.apply.char_guard import (
    ATS_FIELD_LIMITS,
    WARNING_THRESHOLD_PCT,
    TruncationWarning,
    check_truncation,
    get_field_limit,
    measure_and_warn,
)
from src.apply.field_classifier import FieldClassification


# ---------------------------------------------------------------------------
# get_field_limit
# ---------------------------------------------------------------------------

class TestGetFieldLimit:
    def test_workday_default(self):
        assert get_field_limit("workday") == 32000

    def test_workday_cover_letter(self):
        assert get_field_limit("workday", "cover_letter") == 32000

    def test_workday_work_summary(self):
        assert get_field_limit("workday", "work_summary") == 4000

    def test_greenhouse_default(self):
        assert get_field_limit("greenhouse") == 5000

    def test_greenhouse_long_answer(self):
        assert get_field_limit("greenhouse", "long_answer") == 5000

    def test_greenhouse_short_answer(self):
        assert get_field_limit("greenhouse", "short_answer") == 500

    def test_lever_default(self):
        assert get_field_limit("lever") == 2000

    def test_ashby_default_is_none(self):
        assert get_field_limit("ashby") is None

    def test_unknown_platform_returns_none(self):
        assert get_field_limit("taleo") is None

    def test_unknown_platform_unknown_field_returns_none(self):
        assert get_field_limit("taleo", "cover_letter") is None

    def test_case_insensitive_platform(self):
        assert get_field_limit("Workday") == get_field_limit("workday")

    def test_greenhouse_unknown_field_falls_back_to_default(self):
        # An unknown field_type should fall back to the platform default
        assert get_field_limit("greenhouse", "unknown_type") == 5000


# ---------------------------------------------------------------------------
# check_truncation
# ---------------------------------------------------------------------------

class TestCheckTruncation:
    def test_value_under_limit_not_truncated(self):
        value = "a" * 100
        result = check_truncation("cover_letter", value, "lever")
        assert result.truncated is False
        assert result.intended_len == 100
        assert result.limit == 2000
        assert result.pct_of_limit == pytest.approx(0.05, rel=1e-3)

    def test_value_over_limit_truncated(self):
        value = "a" * 2500
        result = check_truncation("cover_letter", value, "lever")
        assert result.truncated is True
        assert result.intended_len == 2500
        assert result.limit == 2000
        assert result.pct_of_limit == pytest.approx(1.25, rel=1e-3)

    def test_value_at_exactly_limit_not_truncated(self):
        value = "a" * 2000
        result = check_truncation("cover_letter", value, "lever")
        assert result.truncated is False
        assert result.intended_len == 2000
        assert result.pct_of_limit == pytest.approx(1.0, rel=1e-3)

    def test_maxlength_override_takes_precedence(self):
        # lever default is 2000, but override to 500
        value = "a" * 600
        result = check_truncation("cover_letter", value, "lever", maxlength=500)
        assert result.truncated is True
        assert result.limit == 500

    def test_maxlength_override_under_limit(self):
        value = "a" * 100
        result = check_truncation("field", value, "lever", maxlength=500)
        assert result.truncated is False
        assert result.limit == 500

    def test_unknown_platform_no_limit(self):
        value = "a" * 9999
        result = check_truncation("answer", value, "taleo")
        assert result.limit is None
        assert result.truncated is False
        assert result.pct_of_limit is None

    def test_ashby_none_limit(self):
        value = "a" * 50000
        result = check_truncation("essay", value, "ashby")
        assert result.limit is None
        assert result.truncated is False
        assert result.pct_of_limit is None

    def test_field_name_stored_correctly(self):
        result = check_truncation("my_field", "hello", "lever")
        assert result.field_name == "my_field"

    def test_pct_of_limit_rounded_to_three_decimals(self):
        # 1 / 3000 = 0.000333...
        value = "a"
        result = check_truncation("f", value, "workday", field_type="work_summary", maxlength=3000)
        assert result.pct_of_limit == round(1 / 3000, 3)

    def test_greenhouse_short_answer_over_limit(self):
        value = "x" * 501
        result = check_truncation("q1", value, "greenhouse", field_type="short_answer")
        assert result.truncated is True
        assert result.limit == 500

    def test_greenhouse_short_answer_under_limit(self):
        value = "x" * 499
        result = check_truncation("q1", value, "greenhouse", field_type="short_answer")
        assert result.truncated is False


# ---------------------------------------------------------------------------
# measure_and_warn (convenience wrapper)
# ---------------------------------------------------------------------------

class TestMeasureAndWarn:
    def test_returns_truncation_warning_instance(self):
        result = measure_and_warn("field", "hello world", "greenhouse")
        assert isinstance(result, TruncationWarning)

    def test_same_result_as_check_truncation(self):
        value = "a" * 300
        r1 = check_truncation("f", value, "greenhouse", "short_answer")
        r2 = measure_and_warn("f", value, "greenhouse", "short_answer")
        assert r1 == r2

    def test_maxlength_forwarded(self):
        value = "a" * 100
        result = measure_and_warn("f", value, "lever", maxlength=50)
        assert result.truncated is True
        assert result.limit == 50

    def test_under_limit_not_truncated(self):
        result = measure_and_warn("cover_letter", "short text", "workday")
        assert result.truncated is False


# ---------------------------------------------------------------------------
# WARNING_THRESHOLD_PCT constant
# ---------------------------------------------------------------------------

class TestWarningThreshold:
    def test_warning_threshold_is_0_9(self):
        assert WARNING_THRESHOLD_PCT == 0.9


# ---------------------------------------------------------------------------
# TruncationWarning dataclass fields
# ---------------------------------------------------------------------------

class TestTruncationWarningDataclass:
    def test_all_expected_fields_present(self):
        w = TruncationWarning(
            field_name="test_field",
            intended_len=100,
            limit=500,
            truncated=False,
            pct_of_limit=0.2,
        )
        assert w.field_name == "test_field"
        assert w.intended_len == 100
        assert w.limit == 500
        assert w.truncated is False
        assert w.pct_of_limit == 0.2

    def test_none_limit_and_pct(self):
        w = TruncationWarning(
            field_name="f",
            intended_len=50,
            limit=None,
            truncated=False,
            pct_of_limit=None,
        )
        assert w.limit is None
        assert w.pct_of_limit is None


# ---------------------------------------------------------------------------
# FieldClassification.max_length (additive field in field_classifier.py)
# ---------------------------------------------------------------------------

class TestFieldClassificationMaxLength:
    def test_max_length_defaults_to_none(self):
        fc = FieldClassification(
            field_name="cover_letter",
            field_label="Cover Letter",
            is_eeo=False,
            matched_keyword=None,
            confidence=1.0,
        )
        assert fc.max_length is None

    def test_max_length_can_be_set(self):
        fc = FieldClassification(
            field_name="cover_letter",
            field_label="Cover Letter",
            is_eeo=False,
            matched_keyword=None,
            confidence=1.0,
            max_length=5000,
        )
        assert fc.max_length == 5000

    def test_max_length_field_exists_on_dataclass(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(FieldClassification)}
        assert "max_length" in fields
