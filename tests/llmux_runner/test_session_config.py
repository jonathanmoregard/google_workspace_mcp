"""The generated MCP config and ``claude`` command line.

These are the pieces that decide whether a run measures the tool surface or
measures the operator's laptop, so the isolation flags are asserted
individually and by name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from llmux.runner import session


def _config(tmp_path: Path) -> dict:
    return session.build_mcp_config(
        tmp_path / "seed.json",
        tmp_path / "state.json",
        credentials_dir=tmp_path / "creds",
        me="alice",
    )


def test_mcp_config_points_at_the_mock_server(tmp_path):
    config = _config(tmp_path)
    (name, spec), *rest = config["mcpServers"].items()
    assert not rest, "exactly one server: the agent gets no other capability"
    assert name == session.SERVER_NAME
    assert spec["command"] == sys.executable
    assert spec["args"][0].endswith("mockdocs/serve.py")
    assert spec["args"][1:] == [
        "--transport",
        "stdio",
        "--single-user",
        "--tools",
        "docs",
        "docs_preview",
    ]


def test_mcp_config_wires_seed_state_and_identity(tmp_path):
    spec = _config(tmp_path)["mcpServers"][session.SERVER_NAME]
    env = spec["env"]
    assert env["MOCKDOCS_SEED"] == str(tmp_path / "seed.json")
    assert env["MOCKDOCS_STATE_DUMP"] == str(tmp_path / "state.json")
    assert env["MOCKDOCS_ME"] == "alice"
    assert env["USER_GOOGLE_EMAIL"] == "alice@example.com"
    assert env["WORKSPACE_MCP_CREDENTIALS_DIR"] == str(tmp_path / "creds")
    # Mode-flipping vars are pinned rather than trusted: MCP config env is
    # merged into the parent environment, so an operator's shell must not be
    # able to put the server into OAuth or read-only mode.
    assert env["MCP_ENABLE_OAUTH21"] == "false"
    assert env["WORKSPACE_MCP_READ_ONLY"] == "false"


def test_mcp_config_is_written_as_valid_json(tmp_path):
    path = session.write_mcp_config(tmp_path / "nested" / "mcp.json", _config(tmp_path))
    assert json.loads(path.read_text())["mcpServers"][session.SERVER_NAME]


def test_allowed_tool_patterns_cover_only_the_mock_server():
    patterns = session.allowed_tool_patterns("gdocsmock")
    assert patterns == ["mcp__gdocsmock", "mcp__gdocsmock__*"]
    assert all(p.startswith("mcp__gdocsmock") for p in patterns)


def test_claude_argv_isolates_the_agent(tmp_path):
    argv = session.build_claude_argv(
        "do the thing", model="sonnet", mcp_config_path=tmp_path / "mcp.json"
    )
    assert argv[0] == "claude"
    assert argv[1:3] == ["-p", "do the thing"]
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    # No settings sources -> no CLAUDE.md, no user permissions leaking in.
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--verbose" in argv


def test_claude_argv_denies_every_builtin_tool(tmp_path):
    argv = session.build_claude_argv(
        "x", model="opus", mcp_config_path=tmp_path / "mcp.json"
    )
    denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--permission-mode")]
    assert set(denied) == set(session.BUILTIN_TOOLS_DENIED)
    for tool in ("Bash", "Read", "Write", "Task", "WebFetch", "ToolSearch", "Skill"):
        assert tool in denied
    # --tools "" would empty the model's tool list entirely, MCP included.
    assert "--tools" not in argv


def test_claude_argv_carries_the_budget_guard(tmp_path):
    argv = session.build_claude_argv(
        "x", model="sonnet", mcp_config_path=tmp_path / "m.json", max_budget_usd=0.25
    )
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    # There is no --max-turns in Claude Code 2.1.x; budget + wall clock are
    # the runaway guards.
    assert "--max-turns" not in argv

    unbounded = session.build_claude_argv(
        "x", model="sonnet", mcp_config_path=tmp_path / "m.json", max_budget_usd=None
    )
    assert "--max-budget-usd" not in unbounded


def test_agent_env_disables_tool_search():
    env = session.build_agent_env({"PATH": "/usr/bin"})
    assert env["PATH"] == "/usr/bin"
    # Without this the CLI hides MCP tools behind ToolSearch, which the deny
    # list removes -- leaving the agent with no tools at all.
    assert env["ENABLE_TOOL_SEARCH"] == "false"
