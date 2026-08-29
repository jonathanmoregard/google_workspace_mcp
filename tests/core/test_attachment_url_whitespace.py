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


def test_gmail_trusted_origins_gain_nothing_from_a_blank_value(http_transport):
    """This side already failed safe; the strip must not change that."""
    gmail_tools = importlib.import_module("gmail.gmail_tools")

    baseline = gmail_tools._get_trusted_attachment_origins()
    http_transport.setenv("WORKSPACE_EXTERNAL_URL", "   ")

    assert gmail_tools._get_trusted_attachment_origins() == baseline
