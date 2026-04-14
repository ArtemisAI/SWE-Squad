"""Tests for CircuitBreaker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.swe_team.circuit_breaker import CircuitBreaker


class TestCircuitBreakerDefaults:
    """Default state: not paused, 0 failure rate."""

    def test_initial_not_paused(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        assert cb.is_paused is False

    def test_initial_failure_rate_zero(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        assert cb.failure_rate == 0.0


class TestRecordSuccess:
    """Recording successes should never trip the breaker."""

    def test_all_successes_no_pause(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(20):
            cb.record_result(True)
        assert cb.is_paused is False
        assert cb.failure_rate == 0.0

    def test_five_successes_no_pause(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(5):
            cb.record_result(True)
        assert cb.is_paused is False


class TestFailuresBelowThreshold:
    """Failures below 80% should not trip the breaker."""

    def test_three_failures_out_of_five(self, tmp_path):
        """60% failure rate — below 80% threshold."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(2):
            cb.record_result(True)
        for _ in range(3):
            cb.record_result(False)
        assert cb.failure_rate == pytest.approx(0.6)
        assert cb.is_paused is False

    def test_seven_failures_out_of_ten(self, tmp_path):
        """70% failure rate — below threshold."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(3):
            cb.record_result(True)
        for _ in range(7):
            cb.record_result(False)
        assert cb.failure_rate == pytest.approx(0.7)
        assert cb.is_paused is False


class TestThresholdTrips:
    """Exactly 80% failure rate with >= 5 results trips the breaker."""

    def test_four_failures_out_of_five(self, tmp_path):
        """80% exactly — should trip."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        cb.record_result(True)
        for _ in range(4):
            cb.record_result(False)
        assert cb.failure_rate == pytest.approx(0.8)
        assert cb.is_paused is True

    def test_all_failures(self, tmp_path):
        """100% failure rate — should trip."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(5):
            cb.record_result(False)
        assert cb.failure_rate == 1.0
        assert cb.is_paused is True

    def test_eight_failures_out_of_ten(self, tmp_path):
        """80% with full window."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(2):
            cb.record_result(True)
        for _ in range(8):
            cb.record_result(False)
        assert cb.failure_rate == pytest.approx(0.8)
        assert cb.is_paused is True


class TestPauseDuration:
    """is_paused should reflect the time window."""

    def test_paused_within_window(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), pause_duration_minutes=30)
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

    def test_not_paused_after_window(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), pause_duration_minutes=30)
        for _ in range(5):
            cb.record_result(False)
        # Simulate time passing beyond the pause window
        future = datetime.now(timezone.utc) + timedelta(minutes=31)
        with patch("src.swe_team.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert cb.is_paused is False

    def test_paused_just_before_expiry(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), pause_duration_minutes=30)
        for _ in range(5):
            cb.record_result(False)
        almost = datetime.now(timezone.utc) + timedelta(minutes=29)
        with patch("src.swe_team.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = almost
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert cb.is_paused is True


class TestRollingWindow:
    """Results beyond window_size should be trimmed."""

    def test_window_truncation(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), window_size=5)
        # Fill with 5 failures
        for _ in range(5):
            cb.record_result(False)
        assert cb.failure_rate == 1.0
        # Now push 5 successes — old failures should be evicted
        for _ in range(5):
            cb.record_result(True)
        assert cb.failure_rate == 0.0
        assert len(cb._results) == 5

    def test_window_size_respected(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), window_size=3)
        for _ in range(10):
            cb.record_result(True)
        assert len(cb._results) == 3


class TestFailureRateEdgeCases:
    """failure_rate with empty, all success, all failure."""

    def test_empty_results(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        assert cb.failure_rate == 0.0

    def test_all_successes(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(10):
            cb.record_result(True)
        assert cb.failure_rate == 0.0

    def test_all_failures(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(10):
            cb.record_result(False)
        assert cb.failure_rate == 1.0


class TestClearPause:
    """clear_pause() resets the paused state."""

    def test_clear_pause_resets(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True
        cb.clear_pause()
        assert cb.is_paused is False

    def test_clear_pause_saves_to_disk(self, tmp_path):
        path = tmp_path / "cb.json"
        cb = CircuitBreaker(state_path=str(path))
        for _ in range(5):
            cb.record_result(False)
        cb.clear_pause()
        data = json.loads(path.read_text())
        assert data["paused_until"] is None

    def test_clear_pause_when_not_paused(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        cb.clear_pause()  # Should not raise
        assert cb.is_paused is False


class TestMinimumResults:
    """Threshold only checked when >= 5 results exist."""

    def test_four_failures_not_enough(self, tmp_path):
        """4 failures out of 4 = 100%, but only 4 results so no trip."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(4):
            cb.record_result(False)
        assert cb.failure_rate == 1.0
        assert cb.is_paused is False

    def test_three_failures_not_enough(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(3):
            cb.record_result(False)
        assert cb.is_paused is False

    def test_five_failures_trips(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True


class TestStatePersistence:
    """Save and load from JSON file."""

    def test_save_and_reload(self, tmp_path):
        path = tmp_path / "cb.json"
        cb1 = CircuitBreaker(state_path=str(path))
        cb1.record_result(True)
        cb1.record_result(False)
        cb1.record_result(True)

        # Reload from disk
        cb2 = CircuitBreaker(state_path=str(path))
        assert cb2._results == [True, False, True]
        assert cb2.failure_rate == pytest.approx(1 / 3)

    def test_paused_state_persists(self, tmp_path):
        path = tmp_path / "cb.json"
        cb1 = CircuitBreaker(state_path=str(path))
        for _ in range(5):
            cb1.record_result(False)
        assert cb1.is_paused is True

        cb2 = CircuitBreaker(state_path=str(path))
        assert cb2.is_paused is True

    def test_state_file_contents(self, tmp_path):
        path = tmp_path / "cb.json"
        cb = CircuitBreaker(state_path=str(path))
        cb.record_result(True)
        cb.record_result(False)

        data = json.loads(path.read_text())
        assert "results" in data
        assert "paused_until" in data
        assert "failure_rate" in data
        assert "is_paused" in data
        assert data["results"] == [True, False]

    def test_window_truncation_on_load(self, tmp_path):
        """State file with more results than window_size gets truncated on load."""
        path = tmp_path / "cb.json"
        path.write_text(json.dumps({"results": [True] * 20, "paused_until": None}))
        cb = CircuitBreaker(state_path=str(path), window_size=5)
        assert len(cb._results) == 5


class TestCorruptOrMissingStateFile:
    """Handle corrupt or missing state files gracefully."""

    def test_missing_state_file(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "nonexistent.json"))
        assert cb.failure_rate == 0.0
        assert cb.is_paused is False

    def test_corrupt_json(self, tmp_path):
        path = tmp_path / "cb.json"
        path.write_text("not valid json {{{")
        cb = CircuitBreaker(state_path=str(path))
        assert cb.failure_rate == 0.0
        assert cb.is_paused is False
        assert cb._results == []

    def test_empty_file(self, tmp_path):
        path = tmp_path / "cb.json"
        path.write_text("")
        cb = CircuitBreaker(state_path=str(path))
        assert cb.failure_rate == 0.0
        assert cb._results == []

    def test_valid_json_missing_keys(self, tmp_path):
        path = tmp_path / "cb.json"
        path.write_text(json.dumps({"unrelated": "data"}))
        cb = CircuitBreaker(state_path=str(path))
        assert cb._results == []
        assert cb._paused_until is None

    def test_corrupt_paused_until(self, tmp_path):
        path = tmp_path / "cb.json"
        path.write_text(json.dumps({"results": [], "paused_until": "not-a-date"}))
        cb = CircuitBreaker(state_path=str(path))
        # Should handle gracefully — is_paused catches exceptions
        assert cb.is_paused is False


class TestStateFileParentDirectory:
    """State file parent directory is created on save."""

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "cb.json"
        cb = CircuitBreaker(state_path=str(path))
        cb.record_result(True)
        assert path.exists()

    def test_existing_parent_dir(self, tmp_path):
        path = tmp_path / "cb.json"
        cb = CircuitBreaker(state_path=str(path))
        cb.record_result(True)
        assert path.exists()


class TestRecordAfterPause:
    """Recording after pause has expired should work correctly."""

    def test_record_after_pause_expired(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"), pause_duration_minutes=30)
        # Trip the breaker
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

        # Simulate time passing beyond pause window
        future = datetime.now(timezone.utc) + timedelta(minutes=31)
        with patch("src.swe_team.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = future
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert cb.is_paused is False

        # Record a success — results still accumulate
        cb.record_result(True)
        # Window has 5 False + 1 True = 6, oldest popped = 5 False + 1 True in window of 10
        assert len(cb._results) == 6

    def test_clear_then_record(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True
        cb.clear_pause()
        assert cb.is_paused is False
        # Old failures still in window (5 False). Adding successes dilutes the rate.
        # After adding 5 True: window has [F,F,F,F,F,T,T,T,T,T] = 50% failures.
        # But note: each record_result checks threshold. The 6th record gives
        # [F,F,F,F,F,T] = 83% which re-trips. So we need enough successes to
        # dilute below 80% before the check fires.
        # With window_size=10 (default), we need at least 6 True to get to
        # [F,F,F,F,T,T,T,T,T,T] after old ones slide out... but the breaker
        # re-trips on each record while still above threshold.
        # Instead, test that clear_pause works and results persist.
        assert cb._results == [False, False, False, False, False]
        assert cb._paused_until is None


class TestRecordSkip:
    """record_skip() must never affect failure rate or trip the breaker."""

    def test_skip_does_not_increment_results(self, tmp_path):
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        cb.record_skip()
        assert cb._results == []
        assert cb.failure_rate == 0.0

    def test_skip_does_not_trip_breaker(self, tmp_path):
        """Even 100 skips should not trip the circuit breaker."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(100):
            cb.record_skip()
        assert cb.is_paused is False
        assert cb.failure_rate == 0.0

    def test_skip_interleaved_with_successes_does_not_affect_rate(self, tmp_path):
        """Skips between real successes should leave failure_rate at 0."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(3):
            cb.record_result(True)
            cb.record_skip()
        assert cb.failure_rate == 0.0
        assert cb.is_paused is False

    def test_skip_does_not_clear_existing_failures(self, tmp_path):
        """Skips should neither add failures nor wash out existing ones."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        cb.record_result(True)
        cb.record_result(False)
        cb.record_result(False)
        rate_before = cb.failure_rate
        cb.record_skip()
        assert cb.failure_rate == pytest.approx(rate_before)
        assert len(cb._results) == 3  # unchanged

    def test_skip_does_not_trip_already_near_threshold(self, tmp_path):
        """4 failures + many skips must not cross the 5-result minimum."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        for _ in range(4):
            cb.record_result(False)
        # 4 results — below the minimum-5 guard, should not be paused yet
        assert cb.is_paused is False
        for _ in range(20):
            cb.record_skip()
        # Still should not be paused — skips didn't push results to 5
        assert cb.is_paused is False

    def test_skip_method_exists(self, tmp_path):
        """record_skip is a public method on CircuitBreaker."""
        cb = CircuitBreaker(state_path=str(tmp_path / "cb.json"))
        assert callable(getattr(cb, "record_skip", None))


class TestCustomParameters:
    """Custom window_size, threshold, and pause_duration."""

    def test_custom_threshold(self, tmp_path):
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb.json"),
            failure_threshold=0.5,
        )
        # 3 failures out of 5 = 60% > 50% threshold
        cb.record_result(True)
        cb.record_result(True)
        for _ in range(3):
            cb.record_result(False)
        assert cb.is_paused is True

    def test_custom_window_size(self, tmp_path):
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb.json"),
            window_size=5,
        )
        # 4 failures out of 5 = 80%
        cb.record_result(True)
        for _ in range(4):
            cb.record_result(False)
        assert cb.is_paused is True
        # Add a success — window now [F, F, F, F, T] = 80%
        # Wait: window_size=5, so oldest (True) is popped and we get [F, F, F, F, T]
        # Actually: [T, F, F, F, F] after 5 records; adding T makes [F, F, F, F, T] = 80%
        cb.clear_pause()
        cb.record_result(True)
        # Window: [F, F, F, F, T] = 80% — trips again
        assert cb.failure_rate == pytest.approx(0.8)

    def test_custom_pause_duration(self, tmp_path):
        cb = CircuitBreaker(
            state_path=str(tmp_path / "cb.json"),
            pause_duration_minutes=5,
        )
        for _ in range(5):
            cb.record_result(False)
        assert cb.is_paused is True

        just_after = datetime.now(timezone.utc) + timedelta(minutes=6)
        with patch("src.swe_team.circuit_breaker.datetime") as mock_dt:
            mock_dt.now.return_value = just_after
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            assert cb.is_paused is False
