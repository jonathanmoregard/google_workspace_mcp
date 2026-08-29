"""The attachment URL reads WORKSPACE_EXTERNAL_URL too, and must trim it.

Instances 8 and 9 of "a value nobody typed read as a deliberate choice". The
redirect URI fix closed this only inside `auth.oauth_config`; the attachment
URL builder reads the same variable straight from the environment, so
`WORKSPACE_EXTERNAL_URL="   "` still produced `"   /attachments/<id>"`.
"""

import importlib

import pytest

from auth.oauth_config import set_transport_mode

BLANKS = ["   ", "\t", "\n", " \t "]


@pytest.fixture
def http_transport(monkeypatch):
    """streamable-http, so the URL builder does not bind a stdio listener."""
    for name in (
        "WORKSPACE_EXTERNAL_URL",
        "WORKSPACE_MCP_BASE_URI",
        "WORKSPACE_MCP_PORT",
        "WORKSPACE_MCP_RESOLVED_PORT",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    previous = importlib.import_module("auth.oauth_config").get_transport_mode()
    set_transport_mode("streamable-http")
    yield monkeypatch
    set_transport_mode(previous)


def _attachment_url(file_id: str) -> str:
    """Late-bound, so a reloaded core.config module is the one consulted."""
    return importlib.import_module("core.attachment_storage").get_attachment_url(
        file_id
    )


@pytest.mark.parametrize("blank", BLANKS)
def test_whitespace_external_url_does_not_reach_the_attachment_url(
    http_transport, blank
):
    http_transport.setenv("WORKSPACE_EXTERNAL_URL", blank)

    url = _attachment_url("abc123")

    assert url == "http://localhost:8000/attachments/abc123"
    assert not url.startswith(" ")


def test_a_real_external_url_is_still_honoured(http_transport):
    http_transport.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com")

    assert _attachment_url("abc123") == "https://mcp.example.com/attachments/abc123"


def test_surrounding_whitespace_is_trimmed_from_a_real_external_url(http_transport):
    http_transport.setenv("WORKSPACE_EXTERNAL_URL", "  https://mcp.example.com\n")

    assert _attachment_url("abc123") == "https://mcp.example.com/attachments/abc123"


def test_a_trailing_slash_still_does_not_double(http_transport):
    http_transport.setenv("WORKSPACE_EXTERNAL_URL", "https://mcp.example.com/")

    assert _attachment_url("abc123") == "https://mcp.example.com/attachments/abc123"


# --- gmail's trusted-origin set: what the strip does and does not move ------
#
# `gmail_tools` binds WORKSPACE_EXTERNAL_URL, WORKSPACE_MCP_BASE_URI and
# WORKSPACE_MCP_PORT from `core.config` AT IMPORT, so `monkeypatch.setenv`
# cannot move `_get_trusted_attachment_origins()` at all — an earlier version
# of these tests used setenv and therefore asserted nothing whatever the strip
# did. They now drive the module attributes the function actually reads, which
# is the only way to compare the pre-strip and post-strip bindings.


def _origins_with(monkeypatch, *, external, base_uri, port=8000):
    gmail_tools = importlib.import_module("gmail.gmail_tools")
    monkeypatch.setattr(gmail_tools, "WORKSPACE_EXTERNAL_URL", external)
    monkeypatch.setattr(gmail_tools, "WORKSPACE_MCP_BASE_URI", base_uri)
    monkeypatch.setattr(gmail_tools, "WORKSPACE_MCP_PORT", port)
    return gmail_tools._get_trusted_attachment_origins()


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_external_url_leaves_the_trusted_origins_unchanged(
    http_transport, blank
):
    """This is the case that genuinely does not move."""
    pre_strip = _origins_with(
        http_transport, external=blank, base_uri="http://localhost"
    )
    post_strip = _origins_with(
        http_transport, external=None, base_uri="http://localhost"
    )

    assert pre_strip == post_strip == {("http", "localhost:8000")}


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_base_uri_DOES_move_the_trusted_origins(http_transport, blank):
    """And this is the case that does — stated precisely, not waved away.

    Pre-strip the set was EMPTY: `":8000"` and `"   :8000"` have no scheme, so
    the only candidate was discarded and no origin was trusted at all.
    Post-strip the set holds this deployment's own origin.

    It is inert, for a reason worth naming rather than asserting on faith. The
    attachment URLs those same bindings produced pre-strip (`":8000/attach…"`)
    have no netloc, and `gmail_tools._try_read_local_attachment` only consults
    the trusted-origin set `if parsed.netloc` — so pre-strip the check was
    SKIPPED, not passed. Post-strip the URL carries a netloc and the check runs
    against an origin that is in the set. Neither state rejects what the other
    accepts; the widening replaces a bypass with an enforced check, and admits
    no third-party origin.
    """
    pre_strip = _origins_with(http_transport, external=None, base_uri=blank)
    post_strip = _origins_with(
        http_transport, external=None, base_uri="http://localhost"
    )

    assert pre_strip == set()
    assert post_strip == {("http", "localhost:8000")}
    assert post_strip - pre_strip == {("http", "localhost:8000")}


def test_the_pre_strip_attachment_url_skipped_the_origin_check_entirely(
    http_transport,
):
    """The reason the set moving is harmless, pinned rather than asserted."""
    from urllib.parse import urlparse

    assert urlparse(":8000/attachments/abc").netloc == ""
    assert urlparse("   :8000/attachments/abc").netloc == ""
    assert urlparse("http://localhost:8000/attachments/abc").netloc == "localhost:8000"


def test_no_third_party_origin_is_ever_trusted(http_transport):
    for blank in ("", "   ", "\t", "\n"):
        origins = _origins_with(
            http_transport, external=blank, base_uri="http://localhost"
        )
        assert origins == {("http", "localhost:8000")}
