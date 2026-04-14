"""Tests for GitHubOAuthProvider — no network calls, no external deps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from src.swe_team.providers.auth.github_oauth import GitHubOAuthProvider


def _make_provider(
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",
    allowed_orgs: list | None = None,
    cookie_secret: str = "super-secret-key",
    session_expiry_hours: int = 24,
) -> GitHubOAuthProvider:
    return GitHubOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        allowed_orgs=allowed_orgs if allowed_orgs is not None else ["your-org"],
        cookie_secret=cookie_secret,
        session_expiry_hours=session_expiry_hours,
    )


class TestAuthorizeUrl(unittest.TestCase):
    def test_authorize_url_contains_client_id(self):
        provider = _make_provider()
        url = provider.get_authorize_url("state123")
        self.assertIn("client_id=test-client-id", url)

    def test_authorize_url_contains_state(self):
        provider = _make_provider()
        url = provider.get_authorize_url("mystate")
        self.assertIn("state=mystate", url)

    def test_authorize_url_contains_scope(self):
        provider = _make_provider()
        url = provider.get_authorize_url("s")
        self.assertIn("scope=read%3Auser+read%3Aorg+repo", url)

    def test_authorize_url_starts_with_github(self):
        provider = _make_provider()
        url = provider.get_authorize_url("s")
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize"))

    def test_different_states_produce_different_urls(self):
        provider = _make_provider()
        url1 = provider.get_authorize_url("state-A")
        url2 = provider.get_authorize_url("state-B")
        self.assertNotEqual(url1, url2)


class TestCookieCreationAndValidation(unittest.TestCase):
    def setUp(self):
        self.provider = _make_provider()
        self.user_info = {
            "login": "octocat",
            "name": "The Octocat",
            "orgs": ["your-org"],
        }

    def test_roundtrip_returns_same_user(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        result = self.provider.validate_session(cookie)
        self.assertIsNotNone(result)
        self.assertEqual(result["login"], "octocat")

    def test_roundtrip_preserves_orgs(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        result = self.provider.validate_session(cookie)
        self.assertIn("your-org", result["orgs"])

    def test_roundtrip_preserves_name(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        result = self.provider.validate_session(cookie)
        self.assertEqual(result["name"], "The Octocat")

    def test_empty_cookie_returns_none(self):
        self.assertIsNone(self.provider.validate_session(""))

    def test_none_like_empty_string(self):
        self.assertIsNone(self.provider.validate_session(""))

    def test_cookie_without_dot_returns_none(self):
        self.assertIsNone(self.provider.validate_session("nodothere"))

    def test_cookie_with_wrong_sig_returns_none(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        parts = cookie.rsplit(".", 1)
        tampered = parts[0] + ".deadbeef" + parts[1][8:]
        self.assertIsNone(self.provider.validate_session(tampered))

    def test_different_secret_rejects_cookie(self):
        other_provider = _make_provider(cookie_secret="different-secret")
        cookie = self.provider.create_session_cookie(self.user_info)
        self.assertIsNone(other_provider.validate_session(cookie))

    def test_invalid_base64_returns_none(self):
        # Craft a cookie with a valid signature but bad base64 payload
        provider = _make_provider()
        bad_payload = "!!!invalid!!!"
        sig = hmac.new(
            provider._cookie_secret,
            bad_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertIsNone(provider.validate_session(f"{bad_payload}.{sig}"))


class TestCookieExpiry(unittest.TestCase):
    def test_expired_cookie_returns_none(self):
        provider = _make_provider(session_expiry_hours=0)
        user_info = {"login": "octocat", "name": "Cat", "orgs": ["your-org"]}
        # Manually craft an already-expired cookie
        payload_data = {
            "login": "octocat",
            "name": "Cat",
            "orgs": ["your-org"],
            "exp": int(time.time()) - 1,  # expired 1 second ago
        }
        payload_json = json.dumps(payload_data, separators=(",", ":"), sort_keys=True)
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
        sig = hmac.new(
            provider._cookie_secret,
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()
        expired_cookie = f"{payload_b64}.{sig}"
        self.assertIsNone(provider.validate_session(expired_cookie))

    def test_future_expiry_is_valid(self):
        provider = _make_provider(session_expiry_hours=1)
        user_info = {"login": "user1", "name": "User One", "orgs": ["MyOrg"]}
        cookie = provider.create_session_cookie(user_info)
        result = provider.validate_session(cookie)
        self.assertIsNotNone(result)
        self.assertEqual(result["login"], "user1")

    def test_newly_created_cookie_has_future_expiry(self):
        provider = _make_provider(session_expiry_hours=24)
        user_info = {"login": "user", "name": "U", "orgs": []}
        cookie = provider.create_session_cookie(user_info)
        # Decode and inspect expiry
        payload_b64 = cookie.rsplit(".", 1)[0]
        payload_json = base64.urlsafe_b64decode(payload_b64 + "==").decode()
        data = json.loads(payload_json)
        self.assertGreater(data["exp"], time.time() + 3600)


class TestOrgAllowlist(unittest.TestCase):
    def test_org_member_is_authorized(self):
        provider = _make_provider(allowed_orgs=["your-org"])
        user = {"login": "u", "orgs": ["your-org", "another-org"]}
        self.assertTrue(provider.is_authorized(user))

    def test_non_member_is_not_authorized(self):
        provider = _make_provider(allowed_orgs=["your-org"])
        user = {"login": "u", "orgs": ["some-other-org"]}
        self.assertFalse(provider.is_authorized(user))

    def test_empty_orgs_user_is_not_authorized(self):
        provider = _make_provider(allowed_orgs=["your-org"])
        user = {"login": "u", "orgs": []}
        self.assertFalse(provider.is_authorized(user))

    def test_empty_allowed_orgs_permits_everyone(self):
        provider = _make_provider(allowed_orgs=[])
        user = {"login": "u", "orgs": []}
        self.assertTrue(provider.is_authorized(user))

    def test_multiple_allowed_orgs_any_match_is_enough(self):
        provider = _make_provider(allowed_orgs=["OrgA", "OrgB"])
        self.assertTrue(provider.is_authorized({"login": "u", "orgs": ["OrgB"]}))

    def test_case_sensitive_org_matching(self):
        provider = _make_provider(allowed_orgs=["your-org"])
        user = {"login": "u", "orgs": ["Your-Org"]}  # wrong case
        self.assertFalse(provider.is_authorized(user))


class TestCookieTamperingDetection(unittest.TestCase):
    def setUp(self):
        self.provider = _make_provider()
        self.user_info = {"login": "alice", "name": "Alice", "orgs": ["your-org"]}

    def test_payload_modification_detected(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        payload_b64, sig = cookie.rsplit(".", 1)
        # Decode, modify, re-encode
        payload_json = base64.urlsafe_b64decode(payload_b64 + "==").decode()
        data = json.loads(payload_json)
        data["login"] = "evil"
        new_payload_json = json.dumps(data, separators=(",", ":"), sort_keys=True)
        new_b64 = base64.urlsafe_b64encode(new_payload_json.encode()).decode()
        # Keep the old signature — should be rejected
        tampered = f"{new_b64}.{sig}"
        self.assertIsNone(self.provider.validate_session(tampered))

    def test_signature_truncation_detected(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        payload_b64, sig = cookie.rsplit(".", 1)
        truncated = f"{payload_b64}.{sig[:10]}"
        self.assertIsNone(self.provider.validate_session(truncated))

    def test_extra_dot_segment_rejected(self):
        cookie = self.provider.create_session_cookie(self.user_info)
        malformed = cookie + ".extra"
        # rsplit(".", 1) gives us: (everything_before_last_dot, "extra")
        # The recalculated sig won't match, so it should be None
        self.assertIsNone(self.provider.validate_session(malformed))


class TestExchangeCode(unittest.TestCase):
    """exchange_code mocks all network calls."""

    def _mock_urlopen(self, token_response: dict, user_response: dict, orgs_response: list):
        """Return a context manager sequence for the three GitHub API calls."""
        call_count = {"n": 0}
        responses = [
            json.dumps(token_response).encode(),
            json.dumps(user_response).encode(),
            json.dumps(orgs_response).encode(),
        ]

        class _FakeResp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _urlopen(req, timeout=15):
            idx = call_count["n"]
            call_count["n"] += 1
            return _FakeResp(responses[idx])

        return _urlopen

    def test_exchange_code_returns_user_with_orgs(self):
        provider = _make_provider()
        token_resp = {"access_token": "ghs_fake_token"}
        user_resp = {"login": "bob", "name": "Bob Smith", "email": "bob@example.com", "avatar_url": ""}
        orgs_resp = [{"login": "your-org"}, {"login": "other-org"}]

        with patch("urllib.request.urlopen", side_effect=self._mock_urlopen(token_resp, user_resp, orgs_resp)):
            result = provider.exchange_code("code123")

        self.assertEqual(result["login"], "bob")
        self.assertIn("your-org", result["orgs"])

    def test_exchange_code_raises_on_token_error(self):
        provider = _make_provider()
        token_resp = {"error": "bad_verification_code", "error_description": "The code has expired."}

        class _FakeResp:
            def read(self):
                return json.dumps(token_resp).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            with self.assertRaises(RuntimeError):
                provider.exchange_code("bad-code")

    def test_exchange_code_handles_empty_orgs(self):
        provider = _make_provider()
        token_resp = {"access_token": "tok"}
        user_resp = {"login": "carol", "name": "Carol", "email": "", "avatar_url": ""}
        orgs_resp: list = []

        with patch("urllib.request.urlopen", side_effect=self._mock_urlopen(token_resp, user_resp, orgs_resp)):
            result = provider.exchange_code("code-abc")

        self.assertEqual(result["orgs"], [])


if __name__ == "__main__":
    unittest.main()
