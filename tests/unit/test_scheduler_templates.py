"""
Tests for scheduler templates API — built-in automation recipes.

Covers:
  - SCHEDULER_TEMPLATES constant contains expected built-in templates
  - _get_scheduler_template lookup by ID
  - Template apply creates a ScheduledJob with correct fields
  - Unknown template ID returns None
  - Template validation (required fields present)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ops.dashboard_server import (
    SCHEDULER_TEMPLATES,
    _get_scheduler_template,
)


# ══════════════════════════════════════════════════════════════════════════════
# Template registry tests
# ══════════════════════════════════════════════════════════════════════════════


class TestSchedulerTemplatesList:
    """Tests for the built-in SCHEDULER_TEMPLATES constant."""

    def test_templates_is_nonempty_list(self):
        assert isinstance(SCHEDULER_TEMPLATES, list)
        assert len(SCHEDULER_TEMPLATES) >= 5

    def test_all_templates_have_required_fields(self):
        required = {"id", "name", "description", "cron", "action", "category"}
        for tpl in SCHEDULER_TEMPLATES:
            missing = required - set(tpl.keys())
            assert not missing, f"Template {tpl.get('id', '?')} missing fields: {missing}"

    def test_all_template_ids_are_unique(self):
        ids = [t["id"] for t in SCHEDULER_TEMPLATES]
        assert len(ids) == len(set(ids)), "Duplicate template IDs found"

    def test_daily_triage_template_exists(self):
        tpl = _get_scheduler_template("daily-triage")
        assert tpl is not None
        assert tpl["name"] == "Daily Triage Run"
        assert tpl["cron"] == "0 9 * * *"
        assert tpl["action"] == "pipeline_trigger"
        assert tpl["category"] == "maintenance"

    def test_nightly_health_check_template(self):
        tpl = _get_scheduler_template("nightly-health-check")
        assert tpl is not None
        assert tpl["cron"] == "0 2 * * *"
        assert tpl["category"] == "monitoring"

    def test_weekly_cost_report_template(self):
        tpl = _get_scheduler_template("weekly-cost-report")
        assert tpl is not None
        assert tpl["cron"] == "0 10 * * 1"
        assert tpl["category"] == "reporting"

    def test_hourly_queue_check_template(self):
        tpl = _get_scheduler_template("hourly-queue-check")
        assert tpl is not None
        assert tpl["cron"] == "0 * * * *"

    def test_monthly_cleanup_template(self):
        tpl = _get_scheduler_template("monthly-cleanup")
        assert tpl is not None
        assert tpl["cron"] == "0 3 1 * *"


class TestGetSchedulerTemplate:
    """Tests for the _get_scheduler_template lookup helper."""

    def test_known_id_returns_dict(self):
        tpl = _get_scheduler_template("daily-triage")
        assert isinstance(tpl, dict)
        assert tpl["id"] == "daily-triage"

    def test_unknown_id_returns_none(self):
        assert _get_scheduler_template("nonexistent-template") is None

    def test_empty_id_returns_none(self):
        assert _get_scheduler_template("") is None


class TestApplyTemplate:
    """Tests for creating a ScheduledJob from a template."""

    def test_apply_template_creates_job(self):
        from src.swe_team.scheduler import ScheduledJob

        tpl = _get_scheduler_template("daily-triage")
        assert tpl is not None

        job_data = {
            "name": tpl["name"],
            "description": tpl["description"],
            "cron_expression": tpl["cron"],
            "schedule_type": "cron",
            "metadata": {
                "from_template": tpl["id"],
                "category": tpl["category"],
                "action": tpl["action"],
            },
        }
        job = ScheduledJob.from_dict(job_data)

        assert job.name == "Daily Triage Run"
        assert job.cron_expression == "0 9 * * *"
        assert job.description == tpl["description"]
        assert job.metadata["from_template"] == "daily-triage"
        assert job.metadata["category"] == "maintenance"
        assert job.metadata["action"] == "pipeline_trigger"

    def test_apply_template_allows_name_override(self):
        from src.swe_team.scheduler import ScheduledJob

        tpl = _get_scheduler_template("nightly-health-check")
        assert tpl is not None

        custom_name = "My Custom Health Check"
        job_data = {
            "name": custom_name,
            "description": tpl["description"],
            "cron_expression": tpl["cron"],
            "schedule_type": "cron",
            "metadata": {"from_template": tpl["id"]},
        }
        job = ScheduledJob.from_dict(job_data)
        assert job.name == custom_name

    def test_apply_template_job_is_enabled_by_default(self):
        from src.swe_team.scheduler import ScheduledJob

        tpl = _get_scheduler_template("hourly-queue-check")
        assert tpl is not None

        job = ScheduledJob.from_dict({
            "name": tpl["name"],
            "cron_expression": tpl["cron"],
            "schedule_type": "cron",
        })
        assert job.enabled is True

    def test_all_templates_produce_valid_jobs(self):
        """Every built-in template should produce a valid ScheduledJob without errors."""
        from src.swe_team.scheduler import ScheduledJob

        for tpl in SCHEDULER_TEMPLATES:
            job = ScheduledJob.from_dict({
                "name": tpl["name"],
                "description": tpl["description"],
                "cron_expression": tpl["cron"],
                "schedule_type": "cron",
                "metadata": {
                    "from_template": tpl["id"],
                    "category": tpl["category"],
                    "action": tpl["action"],
                },
            })
            assert job.name, f"Template {tpl['id']} produced a job with empty name"
            assert job.cron_expression, f"Template {tpl['id']} produced a job with empty cron"


class TestTemplateCronValidation:
    """Verify that all template cron expressions are well-formed 5-field expressions."""

    def test_all_crons_are_five_fields(self):
        for tpl in SCHEDULER_TEMPLATES:
            parts = tpl["cron"].split()
            assert len(parts) == 5, (
                f"Template {tpl['id']} cron '{tpl['cron']}' does not have 5 fields"
            )

    def test_all_crons_parseable(self):
        """Verify cron fields contain only valid characters."""
        import re
        valid_cron_char = re.compile(r"^[\d\*,/\-]+$")
        for tpl in SCHEDULER_TEMPLATES:
            for i, field in enumerate(tpl["cron"].split()):
                assert valid_cron_char.match(field), (
                    f"Template {tpl['id']} cron field {i} '{field}' contains invalid characters"
                )
