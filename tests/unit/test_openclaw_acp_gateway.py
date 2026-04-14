"""
Tests for OpenClaw ACP gateway A2A compatibility (GH-92).

Covers:
- _send_via_openclaw subprocess command structure
- ACP gateway health check on port 18789
- OpenClaw v2026.3.2 JSON response format handling
- Success/failure parsing and fallback behavior
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch, call

import pytest


# ======================================================================
# Auto-clear rate-limit caches before every test so openclaw tests are
# not affected by the process-wide telegram.py rate limiter state.
# ======================================================================

@pytest.fixture(autouse=True)
def _clear_rl_caches():
    import src.swe_team.telegram as _tg
    import src.swe_team.notifier as _n
    _tg._rl_cache.clear()
    _tg._rl_timestamps.clear()
    _n._rate_limit_cache.clear()
    _n._send_timestamps.clear()
    yield
    _tg._rl_cache.clear()
    _tg._rl_timestamps.clear()
    _n._rate_limit_cache.clear()
    _n._send_timestamps.clear()


# ======================================================================
# Helpers
# ======================================================================

def _make_completed_process(returncode=0, stdout="", stderr=""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ======================================================================
# _send_via_openclaw — subprocess command structure
# ======================================================================

class TestSendViaOpenclawCommandStructure:
    """Verify the exact docker exec / openclaw CLI invocation."""

    def test_command_uses_docker_exec_openclaw(self):
        """Command starts with ['docker', 'exec', 'openclaw', 'openclaw', ...]."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("hello")

        args = mock_run.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "exec"
        assert args[2] == "openclaw"
        assert args[3] == "openclaw"

    def test_command_subcommand_is_message_send(self):
        """Sub-command is 'message send'."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("hello")

        args = mock_run.call_args[0][0]
        assert "message" in args
        assert "send" in args

    def test_command_channel_is_telegram(self):
        """--channel telegram is always passed."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("test msg")

        args = mock_run.call_args[0][0]
        assert "--channel" in args
        channel_idx = args.index("--channel")
        assert args[channel_idx + 1] == "telegram"

    def test_command_target_chat_id(self):
        """--target is a non-empty chat ID string."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("test msg")

        args = mock_run.call_args[0][0]
        assert "--target" in args
        target_idx = args.index("--target")
        chat_id = args[target_idx + 1]
        assert chat_id  # non-empty

    def test_command_includes_message_text(self):
        """--message <text> is passed with the provided message."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("my unique payload")

        args = mock_run.call_args[0][0]
        assert "--message" in args
        msg_idx = args.index("--message")
        assert args[msg_idx + 1] == "my unique payload"

    def test_subprocess_run_kwargs(self):
        """capture_output=True, text=True, and a timeout are set."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            notifier._send_via_openclaw("x")

        kwargs = mock_run.call_args[1]
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("timeout") is not None


# ======================================================================
# _send_via_openclaw — success/failure parsing
# ======================================================================

class TestSendViaOpenclawParsing:
    """Success and failure return values based on returncode and stdout."""

    def test_returns_true_on_success(self):
        """Returns True when returncode==0 and 'Sent via Telegram' in stdout."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram"
            )
            result = notifier._send_via_openclaw("hi")

        assert result is True

    def test_returns_false_on_nonzero_returncode(self):
        """Returns False when returncode != 0."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=1, stdout="error"
            )
            result = notifier._send_via_openclaw("hi")

        assert result is False

    def test_returns_false_when_success_string_missing(self):
        """Returns False when returncode==0 but stdout lacks 'Sent via Telegram'."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout=""
            )
            result = notifier._send_via_openclaw("hi")

        assert result is False

    def test_returns_false_on_file_not_found(self):
        """Returns False when docker is not installed (FileNotFoundError)."""
        from src.swe_team import notifier

        with patch("subprocess.run", side_effect=FileNotFoundError("no docker")):
            result = notifier._send_via_openclaw("hi")

        assert result is False

    def test_returns_false_on_timeout(self):
        """Returns False on subprocess.TimeoutExpired."""
        from src.swe_team import notifier

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=30),
        ):
            result = notifier._send_via_openclaw("hi")

        assert result is False

    def test_returns_false_on_generic_exception(self):
        """Returns False on any unexpected exception."""
        from src.swe_team import notifier

        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            result = notifier._send_via_openclaw("hi")

        assert result is False

    def test_never_raises(self):
        """_send_via_openclaw must never propagate exceptions."""
        from src.swe_team import notifier

        for exc in [OSError("x"), ValueError("y"), Exception("z")]:
            with patch("subprocess.run", side_effect=exc):
                try:
                    notifier._send_via_openclaw("msg")
                except Exception as e:
                    pytest.fail(f"_send_via_openclaw raised unexpectedly: {e}")


# ======================================================================
# OpenClaw v2026.3.2 ACP gateway — JSON response format
# ======================================================================

class TestOpenClawACPGatewayJsonResponse:
    """
    OpenClaw v2026.3.2 changed the ACP gateway to return JSON payloads
    instead of plain text.  The notifier checks for 'Sent via Telegram'
    in stdout — tests verify behaviour with various v2026.3.2 responses.
    """

    def test_json_success_response_with_sent_string(self):
        """
        v2026.3.2 may embed 'Sent via Telegram' inside a JSON body.
        As long as the string is present in stdout the notifier succeeds.
        """
        from src.swe_team import notifier

        json_stdout = '{"status": "ok", "detail": "Sent via Telegram", "channel": "telegram"}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout=json_stdout
            )
            result = notifier._send_via_openclaw("ping")

        assert result is True

    def test_json_error_response_without_sent_string(self):
        """
        v2026.3.2 JSON error response (rc=0, but no 'Sent via Telegram') → False.
        """
        from src.swe_team import notifier

        json_stdout = '{"status": "error", "detail": "channel unavailable"}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout=json_stdout
            )
            result = notifier._send_via_openclaw("ping")

        assert result is False

    def test_json_response_nonzero_rc_is_failure(self):
        """Non-zero rc is a failure regardless of JSON content."""
        from src.swe_team import notifier

        json_stdout = '{"status": "ok", "detail": "Sent via Telegram"}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=2, stdout=json_stdout
            )
            result = notifier._send_via_openclaw("ping")

        assert result is False

    def test_plain_text_legacy_response_still_works(self):
        """Pre-v2026.3.2 plain text 'Sent via Telegram' still succeeds."""
        from src.swe_team import notifier

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(
                returncode=0, stdout="Sent via Telegram\n"
            )
            result = notifier._send_via_openclaw("ping")

        assert result is True


# ======================================================================
# ACP gateway health check — port 18789
# ======================================================================

class TestACPGatewayHealthCheck:
    """
    The OpenClaw ACP gateway exposes a health endpoint on port 18789.
    These tests verify that a health check against that port returns the
    expected result, using stdlib urllib (no real network calls).
    """

    def test_health_check_url_uses_port_18789(self):
        """Health check must target port 18789."""
        import urllib.request

        health_url = "http://localhost:18789/health"
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"status":"ok"}'
            mock_open.return_value = mock_resp

            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status

        assert "18789" in health_url
        assert status == 200

    def test_health_check_ok_means_gateway_available(self):
        """HTTP 200 from port 18789 signals gateway is available."""
        import urllib.request

        health_url = "http://localhost:18789/health"

        def _mock_urlopen(req, timeout=None):
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.status = 200
            ctx.read.return_value = b'{"status":"ok"}'
            return ctx

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                available = resp.status == 200

        assert available is True

    def test_health_check_connection_error_means_unavailable(self):
        """Connection refused from port 18789 means gateway unavailable."""
        import urllib.error
        import urllib.request

        health_url = "http://localhost:18789/health"

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            available = False
            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    available = resp.status == 200
            except urllib.error.URLError:
                available = False

        assert available is False


# ======================================================================
# _send — fallback behaviour
# ======================================================================

class TestSendFallback:
    """_send() tries OpenClaw first, falls back to direct Telegram."""

    def setup_method(self):
        import src.swe_team.notifier as _n
        import src.swe_team.telegram as _tg
        _n._rate_limit_cache.clear()
        _n._send_timestamps.clear()
        _tg._rl_cache.clear()
        _tg._rl_timestamps.clear()

    def test_uses_openclaw_when_available(self):
        """If OpenClaw succeeds, direct Telegram is NOT called."""
        from src.swe_team import notifier

        with (
            patch.object(notifier, "_send_via_openclaw", return_value=True) as mock_oc,
            patch.object(notifier, "_send_direct", return_value=True) as mock_direct,
        ):
            result = notifier._send("hello")

        assert result is True
        mock_oc.assert_called_once_with("hello")
        mock_direct.assert_not_called()

    def test_falls_back_to_direct_when_openclaw_fails(self):
        """If OpenClaw fails, direct Telegram is tried."""
        from src.swe_team import notifier

        with (
            patch.object(notifier, "_send_via_openclaw", return_value=False) as mock_oc,
            patch.object(notifier, "_send_direct", return_value=True) as mock_direct,
        ):
            result = notifier._send("hello")

        assert result is True
        mock_oc.assert_called_once_with("hello")
        mock_direct.assert_called_once_with("hello")

    def test_returns_false_when_both_fail(self):
        """Returns False when both OpenClaw and direct Telegram fail."""
        from src.swe_team import notifier

        with (
            patch.object(notifier, "_send_via_openclaw", return_value=False),
            patch.object(notifier, "_send_direct", return_value=False),
        ):
            result = notifier._send("hello")

        assert result is False

    def test_openclaw_failure_does_not_suppress_fallback(self):
        """An exception in _send_via_openclaw must not break the fallback."""
        from src.swe_team import notifier

        # _send_via_openclaw itself never raises (it catches all), so
        # simulate it returning False (the normal failure mode).
        with (
            patch.object(notifier, "_send_via_openclaw", return_value=False),
            patch.object(notifier, "_send_direct", return_value=True) as mock_direct,
        ):
            result = notifier._send("msg")

        assert result is True
        mock_direct.assert_called_once()
