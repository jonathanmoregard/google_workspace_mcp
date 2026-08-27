"""OAUTHLIB_INSECURE_TRANSPORT must only be relaxed for loopback redirect URIs.

The variable is process-wide: setting it makes oauthlib stop requiring HTTPS for
every OAuth exchange in the process. A public deployment (HTTPS redirect URI)
must therefore never have it auto-enabled, while local stdio development against
http://localhost still needs it.
"""

import os

import pytest
from google.oauth2.credentials import Credentials

from auth.google_auth import (
    _allow_insecure_transport_for_local_redirect,
    handle_auth_callback,
    start_auth_flow,
)

_ENV_VAR = "OAUTHLIB_INSECURE_TRANSPORT"

_LOCAL_REDIRECT_URIS = [
    "http://localhost:8000/oauth2callback",
    "http://127.0.0.1:8000/oauth2callback",
]
_PUBLIC_REDIRECT_URIS = [
    "https://mcp.example.com/oauth2callback",
    "https://workspace-mcp.fly.dev/oauth2callback",
    "http://mcp.example.com/oauth2callback",
]


class _DummyFlow:
    def __init__(self, credentials):
        self.credentials = credentials
        self.code_verifier = "verifier"

    def fetch_token(self, authorization_response):  # noqa: ARG002
        return None

    def authorization_url(self, **kwargs):  # noqa: ARG002
        return ("https://accounts.google.com/o/oauth2/auth?x=1", "state")


class _DummyOAuthStore:
    def __init__(self):
        self.store_calls = 0

    def validate_and_consume_oauth_state(self, state, session_id=None):  # noqa: ARG002
        return {
            "session_id": session_id,
            "code_verifier": "verifier",
            "expected_user_email": None,
            "enforce_user_email_match": False,
            "principal_source": None,
        }

    def store_session(self, **kwargs):  # noqa: ARG002
        self.store_calls += 1

    def store_oauth_state(self, state, **kwargs):  # noqa: ARG002
        return None


class _DummyCredentialStore:
    def get_credential(self, user_email):  # noqa: ARG002
        return None

    def store_credential(self, user_email, credentials):  # noqa: ARG002
        return True


@pytest.fixture(autouse=True)
def _clear_insecure_transport(monkeypatch):
    """Start every case from an unset variable and restore the caller's value."""
    monkeypatch.delenv(_ENV_VAR, raising=False)


@pytest.fixture
def patched_oauth(monkeypatch):
    credentials = Credentials(
        token="access-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=["scope.a"],
    )
    monkeypatch.setattr(
        "auth.google_auth.create_oauth_flow",
        lambda **kwargs: _DummyFlow(credentials),  # noqa: ARG005
    )
    monkeypatch.setattr(
        "auth.google_auth.get_oauth21_session_store", lambda: _DummyOAuthStore()
    )
    monkeypatch.setattr(
        "auth.google_auth.get_credential_store", lambda: _DummyCredentialStore()
    )
    monkeypatch.setattr(
        "auth.google_auth.get_user_info",
        lambda credentials: {"email": "user@example.com"},  # noqa: ARG005
    )
    monkeypatch.setattr(
        "auth.google_auth.save_credentials_to_session", lambda *args: None
    )
    monkeypatch.setattr("auth.google_auth.is_stateless_mode", lambda: False)
    monkeypatch.setattr("auth.google_auth.is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr("auth.google_auth.is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(
        "auth.google_auth.get_transport_mode", lambda: "streamable-http"
    )
    monkeypatch.setattr("auth.google_auth.get_current_scopes", lambda: ["scope.a"])


@pytest.mark.parametrize("redirect_uri", _LOCAL_REDIRECT_URIS)
def test_helper_relaxes_https_for_loopback(redirect_uri, monkeypatch):
    _allow_insecure_transport_for_local_redirect(redirect_uri)
    assert os.environ.get(_ENV_VAR) == "1"


@pytest.mark.parametrize("redirect_uri", _PUBLIC_REDIRECT_URIS)
def test_helper_keeps_https_required_for_public_redirect(redirect_uri):
    _allow_insecure_transport_for_local_redirect(redirect_uri)
    assert _ENV_VAR not in os.environ


def test_helper_does_not_override_explicit_operator_setting(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "0")
    _allow_insecure_transport_for_local_redirect("http://localhost:8000/oauth2callback")
    assert os.environ[_ENV_VAR] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_uri", _PUBLIC_REDIRECT_URIS)
async def test_callback_does_not_disable_https_requirement_publicly(
    redirect_uri, patched_oauth
):
    """Regression: the callback used to set the flag unconditionally."""
    email, _ = await handle_auth_callback(
        scopes=["scope.a"],
        authorization_response=f"{redirect_uri}?state=abc&code=code",
        redirect_uri=redirect_uri,
        session_id="session-1",
    )

    assert email == "user@example.com"
    assert _ENV_VAR not in os.environ


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_uri", _LOCAL_REDIRECT_URIS)
async def test_callback_still_allows_http_on_localhost(redirect_uri, patched_oauth):
    """The local stdio flow must keep working."""
    email, _ = await handle_auth_callback(
        scopes=["scope.a"],
        authorization_response=f"{redirect_uri}?state=abc&code=code",
        redirect_uri=redirect_uri,
        session_id="session-1",
    )

    assert email == "user@example.com"
    assert os.environ.get(_ENV_VAR) == "1"


@pytest.mark.asyncio
async def test_start_auth_flow_guard_unchanged_for_public_redirect(patched_oauth):
    await start_auth_flow(
        user_google_email="user@example.com",
        service_name="Test",
        redirect_uri="https://mcp.example.com/oauth2callback",
    )

    assert _ENV_VAR not in os.environ


@pytest.mark.asyncio
async def test_start_auth_flow_guard_unchanged_for_localhost(patched_oauth):
    await start_auth_flow(
        user_google_email="user@example.com",
        service_name="Test",
        redirect_uri="http://localhost:8000/oauth2callback",
    )

    assert os.environ.get(_ENV_VAR) == "1"
