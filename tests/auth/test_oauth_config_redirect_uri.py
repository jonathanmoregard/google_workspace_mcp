"""The OAuth redirect URI must be the same self-identity every other URL uses.

``OAuthConfig.get_oauth_base_url()`` honours ``WORKSPACE_EXTERNAL_URL`` and
feeds FastMCP's ``base_url``, the RFC 8414 metadata endpoints and the startup
summary. ``_get_redirect_uri()`` used to build its own URL from ``base_url``
alone, so a deployment behind a reverse proxy reported one identity everywhere
and a different one in the single URL it hands to Google.
"""

import os

import pytest

from auth.oauth_config import OAuthConfig

# Spelled out rather than imported from the module under test: a test that
# asserts against the same constant the code uses cannot catch that constant
# changing, and importing it would stop this file collecting on any commit
# that predates it.
CALLBACK_PATH = "/oauth2callback"


# Everything OAuthConfig reads that can move the redirect URI. Cleared for
# every test so an ambient value in the developer's shell cannot decide the
# outcome.
_REDIRECT_INPUTS = (
    "GOOGLE_OAUTH_REDIRECT_URI",
    "WORKSPACE_EXTERNAL_URL",
    "WORKSPACE_MCP_BASE_URI",
    "WORKSPACE_MCP_PORT",
    "WORKSPACE_MCP_RESOLVED_PORT",
    "PORT",
    # get_redirect_uris() appends these, so an ambient value would fail the
    # list assertions for a reason that has nothing to do with the fix.
    "OAUTH_CUSTOM_REDIRECT_URIS",
    "OAUTH_ALLOWED_ORIGINS",
    # Keeps _apply_fastmcp_google_env from writing FASTMCP_* into os.environ.
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "MCP_ENABLE_OAUTH21",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _REDIRECT_INPUTS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_redirect_uri_honours_external_url_behind_an_ingress(clean_env):
    """The chart's ingress shape: base URI empty, external URL derived."""
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")
    clean_env.setenv("WORKSPACE_MCP_PORT", "8000")

    config = OAuthConfig()

    assert config.redirect_uri == "https://mcp.example.com/oauth2callback"


def test_redirect_uri_agrees_with_get_oauth_base_url(clean_env):
    """The redirect URI is the server's own identity, not a second opinion.

    This is the invariant the defect broke: every other URL the server reports
    about itself is built on ``get_oauth_base_url()``, including the one
    FastMCP composes for OAuth 2.1 as ``base_url + redirect_path``.
    """
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    config = OAuthConfig()

    assert config.redirect_uri.startswith(config.get_oauth_base_url())
    assert config.redirect_uri == (
        config.get_oauth_base_url().rstrip("/") + config.redirect_path
    )


def test_redirect_uri_matches_fastmcp_composition_with_a_path_prefix(clean_env):
    """A path-bearing external URL must not have its prefix applied twice.

    FastMCP builds its callback as ``str(base_url).rstrip("/") +
    redirect_path``. Deriving ``redirect_path`` by parsing the whole redirect
    URI would hand it ``/mcp/oauth2callback`` alongside a base of
    ``https://example.com/mcp``.
    """
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://example.com/mcp")

    config = OAuthConfig()

    assert config.redirect_path == CALLBACK_PATH
    assert config.redirect_uri == "https://example.com/mcp/oauth2callback"
    assert (
        config.get_oauth_base_url().rstrip("/") + config.redirect_path
    ) == config.redirect_uri


def test_trailing_slash_on_the_external_url_does_not_double(clean_env):
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com/")

    config = OAuthConfig()

    assert config.redirect_uri == "https://mcp.example.com/oauth2callback"


def test_external_url_is_carried_into_the_full_redirect_uri_list(clean_env):
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    config = OAuthConfig()

    assert config.get_redirect_uris() == ["https://mcp.example.com/oauth2callback"]
    assert config.validate_redirect_uri("https://mcp.example.com/oauth2callback")
    assert not config.validate_redirect_uri("http://localhost:8000/oauth2callback")


def test_environment_summary_reports_one_consistent_identity(clean_env):
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    summary = OAuthConfig().get_environment_summary()

    assert summary["effective_oauth_url"] == "https://mcp.example.com"
    assert summary["redirect_uri"] == "https://mcp.example.com/oauth2callback"


# --- R4: local development must behave exactly as it does today -------------


def test_loopback_dev_default_is_unchanged(clean_env):
    """Nothing configured: the historical `http://localhost:8000` callback."""
    config = OAuthConfig()

    assert config.base_url == "http://localhost:8000"
    assert config.get_oauth_base_url() == "http://localhost:8000"
    assert config.redirect_uri == "http://localhost:8000/oauth2callback"
    assert config.redirect_path == "/oauth2callback"


def test_loopback_dev_honours_an_explicit_port(clean_env):
    clean_env.setenv("WORKSPACE_MCP_PORT", "9000")

    config = OAuthConfig()

    assert config.redirect_uri == "http://localhost:9000/oauth2callback"


def test_empty_external_url_reads_as_absent_not_as_a_configured_url(clean_env):
    """The chart ships `WORKSPACE_EXTERNAL_URL: ""`; that is not a URL."""
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "")

    config = OAuthConfig()

    assert config.external_url is None
    assert config.redirect_uri == "http://localhost:8000/oauth2callback"


def test_base_uri_without_an_external_url_still_wins(clean_env):
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "http://workspace-mcp.svc")
    clean_env.setenv("WORKSPACE_MCP_PORT", "8000")

    config = OAuthConfig()

    assert config.redirect_uri == "http://workspace-mcp.svc:8000/oauth2callback"


# --- R3: the operator's override stays on top -------------------------------


def test_explicit_redirect_uri_beats_the_external_url(clean_env):
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")
    clean_env.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://callback.example.com/custom/cb"
    )

    config = OAuthConfig()

    assert config.redirect_uri == "https://callback.example.com/custom/cb"
    assert config.redirect_path == "/custom/cb"


def test_explicit_redirect_uri_beats_the_base_uri(clean_env):
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "http://workspace-mcp.svc")
    clean_env.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://callback.example.com/oauth2callback"
    )

    config = OAuthConfig()

    assert config.redirect_uri == "https://callback.example.com/oauth2callback"


def test_empty_explicit_redirect_uri_is_not_an_override(clean_env):
    clean_env.setenv("GOOGLE_OAUTH_REDIRECT_URI", "")
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    config = OAuthConfig()

    assert config.redirect_uri == "https://mcp.example.com/oauth2callback"


# --- whitespace is the absence of a value, not a choice ---------------------


@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t "])
def test_whitespace_external_url_reads_as_absent(clean_env, blank):
    """`WORKSPACE_EXTERNAL_URL="   "` used to yield `'   /oauth2callback'`."""
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", blank)

    config = OAuthConfig()

    assert config.external_url is None
    assert config.redirect_uri == "http://localhost:8000/oauth2callback"


@pytest.mark.parametrize("blank", ["   ", "\t", "\n"])
def test_whitespace_explicit_redirect_uri_is_not_an_override(clean_env, blank):
    """It used to become the redirect URI verbatim, path `'/   '` and all."""
    clean_env.setenv("GOOGLE_OAUTH_REDIRECT_URI", blank)
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    config = OAuthConfig()

    assert config.redirect_uri == "https://mcp.example.com/oauth2callback"
    assert config.redirect_path == CALLBACK_PATH


def test_surrounding_whitespace_is_trimmed_from_a_real_value(clean_env):
    """A stray newline from a Kubernetes secret or a .env file is not part of
    the URL the operator meant."""
    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "  https://mcp.example.com\n")

    assert OAuthConfig().redirect_uri == "https://mcp.example.com/oauth2callback"


# --- the security claim, pinned ---------------------------------------------


@pytest.fixture
def insecure_transport_isolated():
    """Undo anything the loopback grant writes, even when an assert fails.

    ``_allow_insecure_transport_for_local_redirect`` writes
    ``os.environ`` directly, and monkeypatch cannot restore a variable it never
    saw. Without this teardown a failing assertion below would leave
    ``OAUTHLIB_INSECURE_TRANSPORT=1`` set for the rest of the session and
    silently disarm every later test of the same guard.
    """
    from core.env_flags import (
        INSECURE_TRANSPORT_ENV_VAR,
        reset_insecure_transport_decision,
    )

    previous = os.environ.get(INSECURE_TRANSPORT_ENV_VAR)
    os.environ.pop(INSECURE_TRANSPORT_ENV_VAR, None)
    reset_insecure_transport_decision()
    try:
        yield
    finally:
        os.environ.pop(INSECURE_TRANSPORT_ENV_VAR, None)
        if previous is not None:
            os.environ[INSECURE_TRANSPORT_ENV_VAR] = previous
        reset_insecure_transport_decision()


def test_external_url_deployment_no_longer_trips_the_loopback_grant(
    clean_env, insecure_transport_isolated
):
    """Why this is a security fix and not only a correctness one.

    ``auth.google_auth`` lifts oauthlib's process-wide HTTPS requirement when
    the redirect URI looks like loopback, and that grant is still on by default
    — the operator veto added alongside it has to be typed to take effect. A
    public deployment whose redirect URI wrongly read
    ``http://localhost:8000/oauth2callback`` therefore ran with the requirement
    lifted. Deriving the redirect from the external URL closes that route
    structurally: the grant never fires because the URI is not loopback.
    """
    from auth.google_auth import _allow_insecure_transport_for_local_redirect
    from core.env_flags import insecure_transport_bypass_active

    clean_env.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    config = OAuthConfig()
    _allow_insecure_transport_for_local_redirect(config.redirect_uri)

    assert insecure_transport_bypass_active() is False
