"""
Tests for GH-187: EEO field detection and HITL gating.

Covers:
  - src.apply.field_classifier: classify_field() returning FieldClassification
  - src.apply.hitl_gate: check_fields() returning HITLGateResult
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from src.apply.field_classifier import FieldClassification, classify_field
from src.apply.hitl_gate import HITLGateResult, check_fields


class TestFieldClassifier:
    """Unit tests for classify_field()."""

    def test_gender_field_detected(self) -> None:
        """Field with 'gender' in label is flagged as EEO."""
        result = classify_field("q_gender", "Gender")
        assert isinstance(result, FieldClassification)
        assert result.is_eeo is True
        assert result.matched_keyword is not None

    def test_race_ethnicity_field_detected(self) -> None:
        """Field with 'race' or 'ethnicity' in label is flagged as EEO."""
        result_race = classify_field("q_race", "Race")
        assert result_race.is_eeo is True
        result_eth = classify_field("q_eth", "Ethnicity")
        assert result_eth.is_eeo is True

    def test_disability_field_detected(self) -> None:
        """Field with 'disability' in label is flagged as EEO."""
        result = classify_field("q_dis", "Disability Status")
        assert result.is_eeo is True

    def test_veteran_field_detected(self) -> None:
        """Field with 'veteran' in label is flagged as EEO."""
        result = classify_field("q_vet", "Veteran Status")
        assert result.is_eeo is True

    def test_eeo_field_detected(self) -> None:
        """Field with 'eeo' or 'equal employment' in label is flagged as EEO."""
        result_eeo = classify_field("q_eeo", "EEO Questionnaire")
        assert result_eeo.is_eeo is True
        result_ee = classify_field("q_ee", "Equal Employment Opportunity")
        assert result_ee.is_eeo is True

    def test_normal_field_not_flagged(self) -> None:
        """Common personal info fields like 'First Name' and 'Email' are not EEO."""
        result_name = classify_field("first_name", "First Name")
        assert result_name.is_eeo is False
        result_email = classify_field("email", "Email Address")
        assert result_email.is_eeo is False
        result_phone = classify_field("phone", "Phone Number")
        assert result_phone.is_eeo is False

    def test_case_insensitive_matching(self) -> None:
        """EEO keyword matching is case-insensitive: 'GENDER', 'Gender', 'gender' all match."""
        result_upper = classify_field("q1", "GENDER")
        result_title = classify_field("q2", "Gender")
        result_lower = classify_field("q3", "gender")
        assert result_upper.is_eeo is True
        assert result_title.is_eeo is True
        assert result_lower.is_eeo is True

    def test_verbose_label_matching(self) -> None:
        """Verbose label about veteran status is flagged as EEO."""
        result = classify_field("q_vet_long", "Do you identify as a veteran of the U.S. Armed Forces?")
        assert result.is_eeo is True

    def test_self_identify_label(self) -> None:
        """Labels containing 'self-identify' or 'self-identification' are EEO."""
        result_identify = classify_field("q_si1", "Self-Identify")
        assert result_identify.is_eeo is True
        result_identification = classify_field("q_si2", "Please complete this self-identification form")
        assert result_identification.is_eeo is True

    def test_national_origin_detected(self) -> None:
        """Field with 'national origin' is flagged as EEO."""
        result = classify_field("q_nat", "National Origin")
        assert result.is_eeo is True

    def test_sex_field_detected(self) -> None:
        """Field with 'sex' as a whole word is flagged as EEO, but 'section' is not."""
        result_sex = classify_field("q_sex", "Sex")
        assert result_sex.is_eeo is True
        result_section = classify_field("q_section", "Section")
        assert result_section.is_eeo is False

    def test_work_experience_not_flagged(self) -> None:
        """Work-related fields are not EEO."""
        result_work = classify_field("work_exp", "Work Experience")
        assert result_work.is_eeo is False
        result_years = classify_field("years_exp", "Years of Experience")
        assert result_years.is_eeo is False

    def test_empty_label(self) -> None:
        """An empty string label is not flagged as EEO."""
        result = classify_field("q_empty", "")
        assert result.is_eeo is False

    def test_protected_class_detected(self) -> None:
        """Field with 'protected class' is flagged as EEO."""
        result = classify_field("q_pc", "Protected Class")
        assert result.is_eeo is True


class TestHITLGate:
    """Unit tests for check_fields() and related HITL gate logic."""

    def test_all_safe_fields_pass(self) -> None:
        """When all fields are normal (non-EEO), all_clear is True and no fields are blocked."""
        fields = [
            {"name": "first_name", "label": "First Name"},
            {"name": "last_name", "label": "Last Name"},
            {"name": "email", "label": "Email Address"},
        ]
        result = check_fields("job_001", fields)
        assert isinstance(result, HITLGateResult)
        assert result.all_clear is True
        assert len(result.blocked_fields) == 0
        assert len(result.safe_fields) == 3

    def test_eeo_fields_blocked(self) -> None:
        """Mix of normal and EEO fields: EEO fields go to blocked_fields, normals to safe_fields."""
        fields = [
            {"name": "first_name", "label": "First Name"},
            {"name": "q_gender", "label": "Gender"},
            {"name": "email", "label": "Email"},
            {"name": "q_race", "label": "Race/Ethnicity"},
        ]
        result = check_fields("job_002", fields)
        assert result.all_clear is False
        blocked_names = [f.field_name for f in result.blocked_fields]
        safe_names = [f["name"] for f in result.safe_fields]
        assert "q_gender" in blocked_names
        assert "q_race" in blocked_names
        assert "first_name" in safe_names
        assert "email" in safe_names

    def test_all_eeo_fields_blocked(self) -> None:
        """When every field is EEO, all_clear is False and all fields are in blocked_fields."""
        fields = [
            {"name": "q_gender", "label": "Gender"},
            {"name": "q_veteran", "label": "Veteran Status"},
            {"name": "q_dis", "label": "Disability"},
        ]
        result = check_fields("job_003", fields)
        assert result.all_clear is False
        assert len(result.blocked_fields) == 3
        assert len(result.safe_fields) == 0

    def test_hitl_review_request_format(self) -> None:
        """Each blocked field entry carries job_id, field_name, and requires_review=True."""
        fields = [
            {"name": "q_race", "label": "Race"},
        ]
        result = check_fields("job_042", fields)
        assert len(result.blocked_fields) == 1
        blocked = result.blocked_fields[0]
        assert blocked.job_id == "job_042"
        assert blocked.field_name == "q_race"
        assert blocked.requires_review is True

    def test_format_hitl_alert_contains_fields(self) -> None:
        """format_hitl_alert output contains the blocked field names."""
        from src.apply.hitl_gate import format_hitl_alert
        fields = [
            {"name": "first_name", "label": "First Name"},
            {"name": "q_gender", "label": "Gender"},
            {"name": "q_veteran", "label": "Veteran Status"},
        ]
        result = check_fields("job_099", fields)
        alert_text = format_hitl_alert(result)
        assert isinstance(alert_text, str)
        assert "q_gender" in alert_text
        assert "q_veteran" in alert_text

    def test_empty_fields_list(self) -> None:
        """An empty fields list results in all_clear=True with no blocked fields."""
        result = check_fields("job_empty", [])
        assert result.all_clear is True
        assert len(result.blocked_fields) == 0
        assert len(result.safe_fields) == 0

    def test_blocked_fields_not_in_safe(self) -> None:
        """No field appears in both safe_fields and blocked_fields simultaneously."""
        fields = [
            {"name": "first_name", "label": "First Name"},
            {"name": "last_name", "label": "Last Name"},
            {"name": "q_gender", "label": "Gender"},
            {"name": "q_race", "label": "Race/Ethnicity"},
            {"name": "email", "label": "Email"},
        ]
        result = check_fields("job_999", fields)
        blocked_names = {f.field_name for f in result.blocked_fields}
        safe_names = {f["name"] for f in result.safe_fields}
        all_input_names = {f["name"] for f in fields}
        # No overlap between blocked and safe
        assert blocked_names & safe_names == set()
        # Union covers all inputs
        assert blocked_names | safe_names == all_input_names
