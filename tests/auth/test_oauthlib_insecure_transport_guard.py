"""OAUTHLIB_INSECURE_TRANSPORT must only be relaxed for loopback redirect URIs,
and only when its value actually says so.

The variable is process-wide: setting it makes oauthlib stop requiring HTTPS for
every OAuth exchange in the process. A public deployment (HTTPS redirect URI)
must therefore never have it auto-enabled, while local stdio development against
http://localhost still needs it.

oauthlib reads the variable with ``os.environ.get`` and tests the truthiness of
the resulting *string* — see ``is_secure_transport`` in
``oauthlib/oauth2/rfc6749/utils.py`` — so ``"0"``, ``"false"``, ``"no"`` and
``"off"`` each disabled the HTTPS requirement while reading as "off" to every
human and to the startup banner. These tests pin the library's rule against the
installed library rather than restating it, then pin our own handling to it.
"""

import json
import os
from pathlib import Path

import pytest
import yaml
from google.oauth2.credentials import Credentials
from oauthlib.oauth2.rfc6749.utils import is_secure_transport

from auth.google_auth import (
    _allow_insecure_transport_for_local_redirect,
    handle_auth_callback,
    start_auth_flow,
)
from core.env_flags import (
    INSECURE_TRANSPORT_ENV_VAR,
    insecure_transport_bypass_active,
    normalize_insecure_transport_env,
)

_ENV_VAR = "OAUTHLIB_INSECURE_TRANSPORT"
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: A plain-HTTP URL oauthlib would reject while its HTTPS requirement stands.
_HTTP_TOKEN_URL = "http://token.example/token"

#: Values an operator writes meaning "off". Every one of them used to turn the
#: bypass on, because each is a non-empty string.
_FALSEY_VALUES = ["", "0", "false", "FALSE", "no", "off"]
_TRUTHY_VALUES = ["1", "true", "TRUE", "yes", "on"]


def _https_enforced() -> bool:
    """Ask the installed oauthlib whether it still requires HTTPS."""
    return not is_secure_transport(_HTTP_TOKEN_URL)


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


@pytest.mark.parametrize("value", _TRUTHY_VALUES)
def test_helper_does_not_override_explicit_operator_setting(value, monkeypatch):
    """An operator who turned the bypass on keeps it on."""
    monkeypatch.setenv(_ENV_VAR, value)
    _allow_insecure_transport_for_local_redirect("http://localhost:8000/oauth2callback")
    assert insecure_transport_bypass_active()
    assert not _https_enforced()


@pytest.mark.parametrize("value", _FALSEY_VALUES)
def test_helper_honours_an_explicit_off_even_on_loopback(value, monkeypatch):
    """A value that reads as off must switch the bypass off, not on.

    This is the defect: the helper returned early on *presence* of the variable
    and left the operator's ``"0"`` in place, where oauthlib read it as a
    non-empty string and stopped requiring HTTPS. Setting it explicitly is now
    the only way to decline the loopback auto-grant, so it has to be honoured
    here rather than overwritten.
    """
    monkeypatch.setenv(_ENV_VAR, value)
    _allow_insecure_transport_for_local_redirect("http://localhost:8000/oauth2callback")
    assert not insecure_transport_bypass_active()
    assert _https_enforced()


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


# --------------------------------------------------------------------------
# oauthlib's own rule, pinned against the installed library
# --------------------------------------------------------------------------


def test_env_var_name_matches_the_one_oauthlib_reads():
    assert INSECURE_TRANSPORT_ENV_VAR == _ENV_VAR


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "1", "true", "anything"])
def test_oauthlib_lifts_https_for_any_non_empty_value(value, monkeypatch):
    """Characterisation: oauthlib tests the truthiness of the raw string.

    Not asserted from memory — this runs against whichever oauthlib the lock
    file resolves, so it fails if the library ever starts parsing the value.
    """
    monkeypatch.setenv(_ENV_VAR, value)
    assert not _https_enforced()


@pytest.mark.parametrize("value", [None, ""])
def test_oauthlib_requires_https_when_absent_or_empty(value, monkeypatch):
    if value is None:
        monkeypatch.delenv(_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(_ENV_VAR, value)
    assert _https_enforced()


def test_bypass_predicate_matches_oauthlib_exactly(monkeypatch):
    """Our predicate must be oauthlib's rule, not the shared boolean parser."""
    for value in _FALSEY_VALUES + _TRUTHY_VALUES + ["treu", None]:
        if value is None:
            monkeypatch.delenv(_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(_ENV_VAR, value)
        assert insecure_transport_bypass_active() is (not _https_enforced()), value


# --------------------------------------------------------------------------
# Normalisation: a value that looks off has to be off
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", _FALSEY_VALUES)
def test_normalise_turns_a_falsey_value_into_a_real_off(value, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, value)

    assert normalize_insecure_transport_env() is False
    assert _https_enforced()
    # Kept present-but-empty rather than deleted, so the loopback guard can
    # still tell "operator declined" from "operator said nothing".
    assert os.environ[_ENV_VAR] == ""


@pytest.mark.parametrize("value", _TRUTHY_VALUES)
def test_normalise_canonicalises_a_truthy_value(value, monkeypatch):
    monkeypatch.setenv(_ENV_VAR, value)

    assert normalize_insecure_transport_env() is True
    assert not _https_enforced()
    assert os.environ[_ENV_VAR] == "1"


def test_normalise_leaves_an_unset_variable_unset(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)

    assert normalize_insecure_transport_env() is False
    assert _ENV_VAR not in os.environ
    assert _https_enforced()


def test_normalise_fails_closed_and_loudly_on_an_unrecognised_value(
    monkeypatch, caplog
):
    """A typo must not be a bypass. ``parse_bool_env`` raises; we log and refuse."""
    monkeypatch.setenv(_ENV_VAR, "treu")

    with caplog.at_level("ERROR"):
        assert normalize_insecure_transport_env() is False

    assert _https_enforced()
    assert os.environ[_ENV_VAR] == ""
    assert any("treu" in record.getMessage() for record in caplog.records)


def test_normalise_is_idempotent(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "0")
    normalize_insecure_transport_env()
    first = os.environ[_ENV_VAR]

    assert normalize_insecure_transport_env() is False
    assert os.environ[_ENV_VAR] == first


# --------------------------------------------------------------------------
# The startup banner has to agree with oauthlib
# --------------------------------------------------------------------------


def _banner_row(name: str) -> tuple[str, str, str]:
    # Imported lazily: main's module body loads .env and settles OAuth 2.1 mode
    # at decoration time, which must not happen merely by collecting this file.
    import main

    rows = {row[0]: row for row in main.describe_mode_config()}
    return rows[name]


@pytest.mark.parametrize("value", ["", "0", "1", "false", "no", "on", "treu"])
def test_banner_state_agrees_with_oauthlib(value, monkeypatch):
    """The banner said "off" for ``"0"`` while the HTTPS requirement was lifted."""
    monkeypatch.setenv(_ENV_VAR, value)

    _, _, state = _banner_row(_ENV_VAR)
    bypassed = not _https_enforced()

    assert (state == "warn") is bypassed, (value, state)


def test_banner_reports_an_unset_variable_as_off(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)

    _, value, state = _banner_row(_ENV_VAR)

    assert state == "off"
    assert value == "not set"


def test_banner_names_the_consequence_when_the_bypass_is_on(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "0")

    _, value, _ = _banner_row(_ENV_VAR)

    assert "0" in value
    assert "HTTPS" in value


# --------------------------------------------------------------------------
# Shipped config must not enable the bypass for every install
# --------------------------------------------------------------------------


def _deployment_env_of(relative_path: str, *keys: str) -> dict:
    data = json.loads((_REPO_ROOT / relative_path).read_text())
    for key in keys:
        data = data[key]
    return data


def test_fastmcp_manifest_does_not_enable_the_bypass():
    env = _deployment_env_of("fastmcp.json", "deployment", "env")
    assert _ENV_VAR not in env


def test_claude_plugin_manifest_does_not_enable_the_bypass():
    env = _deployment_env_of(
        ".claude-plugin/plugin.json", "mcpServers", "google-workspace", "env"
    )
    assert _ENV_VAR not in env


def test_mcpb_manifest_does_not_default_the_bypass_on():
    manifest = json.loads((_REPO_ROOT / "manifest.json").read_text())
    assert manifest["user_config"][_ENV_VAR]["default"] is False


def test_helm_values_ship_no_value_for_the_bypass():
    """``"0"`` is a non-empty string, so the chart shipped the bypass enabled."""
    values = yaml.safe_load(
        (_REPO_ROOT / "helm-chart" / "workspace-mcp" / "values.yaml").read_text()
    )
    assert _ENV_VAR not in values["env"]
