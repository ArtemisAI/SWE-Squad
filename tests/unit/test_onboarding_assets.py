from pathlib import Path
import subprocess


def test_onboard_script_has_valid_bash_syntax() -> None:
    script = Path("scripts/ops/onboard_repo.sh")
    assert script.exists()

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_onboard_script_contains_required_labels_and_steps() -> None:
    content = Path("scripts/ops/onboard_repo.sh").read_text(encoding="utf-8")

    required_labels = [
        "swe-team",
        "auto-detected",
        "severity: critical",
        "severity: high",
        "severity: medium",
        "severity: low",
        "module:*",
    ]

    for label in required_labels:
        assert label in content

    assert "python3 scripts/ops/swe_team_runner.py --bootstrap" in content
    assert "send_test_telegram" in content


def test_onboarding_doc_covers_manual_guide_sections() -> None:
    doc = Path("ONBOARDING.md")
    assert doc.exists()

    content = doc.read_text(encoding="utf-8")
    for section in [
        "## Prerequisites",
        "## Bot account + PAT setup",
        "## Repository permission setup",
        "## `.env` configuration reference",
        "## VM sandbox deployment notes",
        "## Bootstrap + first-run verification",
        "## Troubleshooting",
    ]:
        assert section in content
