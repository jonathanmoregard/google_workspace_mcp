"""Trimming WORKSPACE_MCP_BASE_URI must not move the anti-rebinding allowlist.

`WORKSPACE_MCP_BASE_URI="   "` was truthy, so it became `base_url = "   :8000"`
and the redirect URI `"   :8000/oauth2callback"`. Stripping it makes whitespace
read as absent, like the empty string already did — but `base_url` also feeds
`core.server._configured_hostnames()`, the Host allowlist that guards against
DNS rebinding, and widening that by accident would be a security regression.

These tests pin that it does not widen: `"   :8000"` had no hostname to
contribute, and the stripped value's `localhost` is already in the loopback set,
so the allowlist is byte-identical before and after.
"""

import importlib
import os

import pytest

from core.server import (
    _configured_hostnames,
    _get_allowed_http_origins,
    _is_configured_host,
)


def reload_oauth_config():
    """Reload through `sys.modules`, never through a captured reference.

    Not a style preference — without it these tests pass alone and fail in the
    full suite. `tests/auth/test_port_resolver.py` deletes `auth.oauth_config`
    from `sys.modules` and re-imports it, so from that point on there are two
    distinct module objects, each with its own `_oauth_config` singleton. A
    name bound by `from auth.oauth_config import ...` at collection time keeps
    pointing at the OLD one, while `core.server._configured_hostnames()`
    imports inside the function body and therefore resolves the NEW one — so
    the reload updated a config nothing under test ever read, and the
    assertions silently measured a stale singleton.

    Binding late is exactly what the production code does, and it is what makes
    these assertions mean anything.
    """
    return importlib.import_module("auth.oauth_config").reload_oauth_config()


_INPUTS = (
    "WORKSPACE_MCP_BASE_URI",
    "WORKSPACE_EXTERNAL_URL",
    "WORKSPACE_MCP_PORT",
    "WORKSPACE_MCP_RESOLVED_PORT",
    "PORT",
    "OAUTH_ALLOWED_ORIGINS",
    "OAUTH_CUSTOM_REDIRECT_URIS",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "MCP_ENABLE_OAUTH21",
)

BLANKS = ["   ", "\t", "\n", " \t "]


@pytest.fixture
def clean_env(monkeypatch):
    """Clear the redirect inputs, and leave the config singleton pristine.

    The teardown restores the environment ITSELF before reloading, rather than
    leaving that to monkeypatch. Fixture teardown is LIFO: monkeypatch is set
    up first, so its undo runs LAST — after this block. Reloading here without
    restoring first would rebuild the process-wide singleton from the test's
    own variables and hand it to every test that follows, which is how a
    fixture written to prove isolation comes to break it.

    monkeypatch's later undo is then a no-op for these names, because they
    already hold their original values. Every variable the tests in this file
    touch is in `_INPUTS`, which is what makes that true.
    """
    snapshot = {name: os.environ.get(name) for name in _INPUTS}
    for name in _INPUTS:
        monkeypatch.delenv(name, raising=False)
    try:
        yield monkeypatch
    finally:
        for name, value in snapshot.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reload_oauth_config()


@pytest.mark.parametrize("blank", BLANKS)
def test_host_allowlist_is_identical_to_the_unset_case(clean_env, blank):
    """The condition this change had to satisfy before it could ship."""
    reload_oauth_config()
    baseline = _configured_hostnames()

    clean_env.setenv("WORKSPACE_MCP_BASE_URI", blank)
    reload_oauth_config()

    assert _configured_hostnames() == baseline


@pytest.mark.parametrize("blank", BLANKS)
def test_no_new_host_is_accepted(clean_env, blank):
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", blank)
    reload_oauth_config()

    assert _is_configured_host("localhost") is True
    assert _is_configured_host("evil.example.com") is False
    assert _is_configured_host(" ") is False


def test_a_real_base_uri_still_contributes_its_hostname(clean_env):
    """The strip must not stop a configured name reaching the allowlist."""
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "http://workspace-mcp.svc")
    reload_oauth_config()

    assert "workspace-mcp.svc" in _configured_hostnames()
    assert _is_configured_host("workspace-mcp.svc") is True


def test_a_stray_newline_no_longer_lands_inside_the_urls(clean_env):
    """urlparse tolerated it in the allowlist; the redirect URI did not."""
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "  http://workspace-mcp.svc\n")
    config = reload_oauth_config()

    assert config.base_url == "http://workspace-mcp.svc:8000"
    assert config.redirect_uri == "http://workspace-mcp.svc:8000/oauth2callback"
    assert "workspace-mcp.svc" in _configured_hostnames()


@pytest.mark.parametrize("blank", BLANKS)
def test_whitespace_now_behaves_exactly_like_empty(clean_env, blank):
    """Both mean "nobody configured this", so both must land on the default."""
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "")
    empty = reload_oauth_config()
    empty_state = (empty.base_url, empty.redirect_uri, _get_allowed_http_origins())

    clean_env.setenv("WORKSPACE_MCP_BASE_URI", blank)
    blank_config = reload_oauth_config()

    assert (
        blank_config.base_url,
        blank_config.redirect_uri,
        _get_allowed_http_origins(),
    ) == empty_state


@pytest.mark.parametrize("blank", BLANKS)
def test_the_deployments_own_origin_comes_back(clean_env, blank):
    """The garbage value normalised to nothing and dropped it silently."""
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", blank)
    reload_oauth_config()

    assert "http://localhost:8000" in _get_allowed_http_origins()


@pytest.mark.parametrize("blank", BLANKS)
def test_no_third_party_origin_is_introduced(clean_env, blank):
    """The only entry the strip adds is this deployment's own base URL."""
    reload_oauth_config()
    baseline = _get_allowed_http_origins()

    clean_env.setenv("WORKSPACE_MCP_BASE_URI", blank)
    reload_oauth_config()

    assert _get_allowed_http_origins() == baseline


# --- the fixture must not leak its own config, either --------------------


def test_leak_probe_sets_a_distinctive_base_uri(clean_env):
    """Paired with the test below; this one only has to run first."""
    clean_env.setenv("WORKSPACE_MCP_BASE_URI", "http://leak-probe.invalid")

    assert reload_oauth_config().base_url == "http://leak-probe.invalid:8000"


def test_the_fixture_leaves_the_singleton_pristine():
    """Deliberately takes no fixture: it inspects what the previous one left.

    Fixture teardown is LIFO, so a `clean_env` that reloads the config after
    its `yield` runs BEFORE monkeypatch restores the environment — rebuilding
    the process-wide singleton from the *test's* variables and leaving it for
    everything that follows. A fixture that claims to restore a singleton and
    leaks instead is worse than none, because it reads as covered.
    """
    config = importlib.import_module("auth.oauth_config").get_oauth_config()

    assert "leak-probe.invalid" not in config.base_url
