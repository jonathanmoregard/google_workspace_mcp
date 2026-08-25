"""Tests for the ``list_google_accounts`` tool registered on the core server."""

import json
from types import SimpleNamespace

import pytest

import core.account_directory as account_directory
import core.server as server_module
from core.server import SecureFastMCP, list_google_accounts
from core.tool_registry import get_tool_components
from core.tool_tier_loader import ToolTierLoader

TOOL_NAME = "list_google_accounts"


def _component():
    return get_tool_components(server_module.server)[TOOL_NAME]


def _stub_directory(monkeypatch, emails):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(
        account_directory,
        "peek_credential_store",
        lambda: SimpleNamespace(list_users=lambda: list(emails)),
    )


def test_tool_is_registered_on_the_core_server():
    assert TOOL_NAME in get_tool_components(server_module.server)


def test_tool_is_annotated_read_only_and_closed_world():
    annotations = _component().annotations

    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is False


def test_tool_exposes_no_account_parameter():
    """The agent must not be offered an account to choose — in any mode."""
    properties = (_component().parameters or {}).get("properties", {})

    assert properties == {}


@pytest.mark.asyncio
async def test_tool_returns_a_json_report(monkeypatch):
    _stub_directory(monkeypatch, ["solo@example.com", "work@example.com"])
    monkeypatch.setattr(server_module, "USER_GOOGLE_EMAIL", "solo@example.com")

    payload = json.loads(await list_google_accounts())

    assert payload["default_account"] == "solo@example.com"
    assert [a["email"] for a in payload["accounts"]] == [
        "solo@example.com",
        "work@example.com",
    ]
    assert payload["probed"] is False


@pytest.mark.asyncio
async def test_tool_makes_no_network_calls(monkeypatch):
    """The tool answers from the credential store and cached state only."""

    def fail_if_called(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("list_google_accounts must not build a Google service")

    _stub_directory(monkeypatch, ["solo@example.com"])
    monkeypatch.setattr(server_module, "USER_GOOGLE_EMAIL", "solo@example.com")
    monkeypatch.setattr("auth.service_decorator.build", fail_if_called)

    assert json.loads(await list_google_accounts())["probed"] is False


@pytest.mark.asyncio
async def test_call_tool_does_not_inject_email_into_a_tool_without_the_parameter(
    monkeypatch,
):
    """``list_google_accounts`` takes no arguments; the single-user default
    injection in SecureFastMCP.call_tool must not break such a tool."""
    monkeypatch.setattr(server_module, "USER_GOOGLE_EMAIL", "configured@example.com")
    monkeypatch.setattr(server_module, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(server_module, "is_trust_gateway_identity", lambda: False)

    server = SecureFastMCP(name="test_server")

    def no_arguments() -> str:
        return "ok"

    server.tool()(no_arguments)

    result = await server.call_tool("no_arguments", None)

    assert result.content[0].text == "ok"


@pytest.mark.asyncio
async def test_call_tool_still_injects_the_default_for_tools_that_take_it(monkeypatch):
    monkeypatch.setattr(server_module, "USER_GOOGLE_EMAIL", "configured@example.com")
    monkeypatch.setattr(server_module, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(server_module, "is_trust_gateway_identity", lambda: False)

    server = SecureFastMCP(name="test_server")

    def echo_email(user_google_email: str) -> str:
        return user_google_email

    server.tool()(echo_email)

    result = await server.call_tool("echo_email", None)

    assert result.content[0].text == "configured@example.com"


def test_tool_survives_tier_filtering_at_every_tier():
    """Without a tool_tiers.yaml entry the tool is silently dropped for every
    ``--tool-tier`` user while default runs keep it."""
    loader = ToolTierLoader()

    for tier in ("core", "extended", "complete"):
        assert TOOL_NAME in loader.get_tools_up_to_tier(tier)
        assert TOOL_NAME in loader.get_tools_up_to_tier(tier, ["docs"])
