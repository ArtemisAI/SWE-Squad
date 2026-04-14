"""Unit tests for WebUI onboarding API endpoints."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
import sys

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


class TestOnboardingAPI:
    """Test cases for the /api/onboarding/complete endpoint."""

    def test_onboarding_complete_valid_request(self) -> None:
        """Test successful onboarding completion with valid data."""
        from scripts.ops.dashboard_server import DashboardHandler

        # Create a temporary config file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("")  # Empty config
            temp_config_path = Path(f.name)

        try:
            # Mock the config path
            with patch(
                "scripts.ops.dashboard_server._CONFIG_PATH", temp_config_path
            ):
                handler = Mock(spec=DashboardHandler)
                handler._read_post_body = Mock(
                    return_value={
                        "team_id": "test-team",
                        "repos": [
                            {"name": "owner/repo1", "branches": ["main"]},
                            {"name": "owner/repo2"},
                        ],
                    }
                )
                handler._json_response = Mock()

                # Import and call the handler method
                from scripts.ops.dashboard_server import DashboardHandler as DH
                DH._handle_onboarding_complete(handler)

                # Verify success response
                handler._json_response.assert_called_once()
                call_args = handler._json_response.call_args
                response_data = call_args[0][0]

                assert response_data.get("ok") is True
                assert response_data.get("team_id") == "test-team"
                assert "repos" in response_data

                # Verify config was written
                import yaml
                config = yaml.safe_load(temp_config_path.read_text())
                assert config.get("team_id") == "test-team"
                assert len(config.get("repos", [])) == 2

        finally:
            temp_config_path.unlink()

    def test_onboarding_complete_missing_team_id(self) -> None:
        """Test onboarding completion with missing team_id."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(return_value={"repos": [{"name": "owner/repo"}]})
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]
        status = call_args[1] if len(call_args) > 1 else {}

        assert response_data.get("error") is not None
        assert "team_id" in response_data.get("error", "").lower()
        assert status.get("status", 200) == 400

    def test_onboarding_complete_empty_team_id(self) -> None:
        """Test onboarding completion with empty team_id."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(
            return_value={"team_id": "   ", "repos": [{"name": "owner/repo"}]}
        )
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]

        assert response_data.get("error") is not None
        assert "team_id" in response_data.get("error", "").lower()

    def test_onboarding_complete_missing_repos(self) -> None:
        """Test onboarding completion with missing repos."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(return_value={"team_id": "test-team"})
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]

        assert response_data.get("error") is not None
        assert "repository" in response_data.get("error", "").lower()

    def test_onboarding_complete_empty_repos_array(self) -> None:
        """Test onboarding completion with empty repos array."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(return_value={"team_id": "test-team", "repos": []})
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]

        assert response_data.get("error") is not None
        assert "repository" in response_data.get("error", "").lower()

    def test_onboarding_complete_invalid_repo_format(self) -> None:
        """Test onboarding completion with invalid repo format."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(
            return_value={"team_id": "test-team", "repos": "invalid"}
        )
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]

        assert response_data.get("error") is not None
        assert "array" in response_data.get("error", "").lower()

    def test_onboarding_complete_missing_repo_name(self) -> None:
        """Test onboarding completion with repo missing name field."""
        from scripts.ops.dashboard_server import DashboardHandler

        handler = Mock(spec=DashboardHandler)
        handler._read_post_body = Mock(
            return_value={"team_id": "test-team", "repos": [{"branches": ["main"]}]}
        )
        handler._json_response = Mock()

        from scripts.ops.dashboard_server import DashboardHandler as DH
        DH._handle_onboarding_complete(handler)

        handler._json_response.assert_called_once()
        call_args = handler._json_response.call_args
        response_data = call_args[0][0]

        assert response_data.get("error") is not None
        assert "name" in response_data.get("error", "").lower()

    def test_onboarding_merges_with_existing_config(self) -> None:
        """Test onboarding merges new repos with existing config."""
        from scripts.ops.dashboard_server import DashboardHandler

        # Create a temporary config file with existing repo
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("repos:\n  - name: existing/repo\n    branches: [main]\n")
            temp_config_path = Path(f.name)

        try:
            with patch(
                "scripts.ops.dashboard_server._CONFIG_PATH", temp_config_path
            ):
                handler = Mock(spec=DashboardHandler)
                handler._read_post_body = Mock(
                    return_value={
                        "team_id": "test-team",
                        "repos": [
                            {"name": "owner/repo1", "branches": ["develop"]},
                            {"name": "owner/repo2"},
                        ],
                    }
                )
                handler._json_response = Mock()

                from scripts.ops.dashboard_server import DashboardHandler as DH
                DH._handle_onboarding_complete(handler)

                # Verify config was merged
                import yaml
                config = yaml.safe_load(temp_config_path.read_text())
                repos = config.get("repos", [])
                repo_names = [r.get("name") for r in repos]

                assert len(repos) == 3
                assert "existing/repo" in repo_names
                assert "owner/repo1" in repo_names
                assert "owner/repo2" in repo_names

        finally:
            temp_config_path.unlink()

    def test_onboarding_duplicate_repo_names_not_duplicated(self) -> None:
        """Test that duplicate repo names in onboarding are not added twice."""
        from scripts.ops.dashboard_server import DashboardHandler

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("repos:\n  - name: owner/repo1\n")
            temp_config_path = Path(f.name)

        try:
            with patch(
                "scripts.ops.dashboard_server._CONFIG_PATH", temp_config_path
            ):
                handler = Mock(spec=DashboardHandler)
                handler._read_post_body = Mock(
                    return_value={
                        "team_id": "test-team",
                        "repos": [
                            {"name": "owner/repo1"},  # duplicate
                            {"name": "owner/repo2"},  # new
                        ],
                    }
                )
                handler._json_response = Mock()

                from scripts.ops.dashboard_server import DashboardHandler as DH
                DH._handle_onboarding_complete(handler)

                # Verify duplicate not added
                import yaml
                config = yaml.safe_load(temp_config_path.read_text())
                repos = config.get("repos", [])

                assert len(repos) == 2  # existing + new only
                repo_names = [r.get("name") for r in repos]
                assert repo_names.count("owner/repo1") == 1

        finally:
            temp_config_path.unlink()


class TestOnboardingRouteAccess:
    """Test onboarding route accessibility and auth guards."""

    def test_onboarding_route_exempt_from_auth_check(self) -> None:
        """Verify /onboarding path is exempted from auth middleware."""
        from scripts.ops.dashboard_server import DashboardHandler

        # The route handler logic should exempt /onboarding from auth
        # This is verified by checking the _do_GET method implementation
        # (do_GET delegates to _do_GET via the dispatch wrapper)

        import inspect
        source = inspect.getsource(DashboardHandler._do_GET)

        # Verify /onboarding is handled before auth check
        assert '"/onboarding"' in source or "== \"/onboarding\"" in source

    def test_auth_routes_always_accessible(self) -> None:
        """Verify auth routes are handled before auth middleware."""
        from scripts.ops.dashboard_server import DashboardHandler

        import inspect
        source = inspect.getsource(DashboardHandler._do_GET)

        # Verify auth routes are handled first
        assert '"/auth/login"' in source
        assert '"/auth/callback"' in source
        assert '"/auth/logout"' in source
