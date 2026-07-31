"""Everything needed to start one isolated agent run.

Two pure builders (unit-tested without spending a token):

- :func:`build_mcp_config` -- the ``--mcp-config`` JSON that points the agent
  at *this repo's real MCP server* running against the mockdocs backend, one
  fresh subprocess per run, seeded from the scenario and dumping its end
  state to a file the harness reads back.
- :func:`build_claude_argv` -- the ``claude`` command line.

Isolation rules encoded here, each one load-bearing and each one verified
against Claude Code 2.1.220 rather than assumed:

``cwd`` is a scratch directory
    The agent never sees this repository, so it cannot read the tool source
    (or the scenario's ``expected.json``) instead of using the tools.
``--disallowedTools <every built-in>``
    The only capability left is the mock MCP server -- the surface under
    measurement. Note that ``--tools ""`` does **not** work for this: it
    empties the model's tool list entirely, MCP tools included, and the agent
    then narrates tool calls as prose instead of making them. The list is a
    superset by design and is still not sufficient on its own -- see
    :data:`BUILTIN_TOOLS_DENIED` and :mod:`llmux.runner.toolprobe`.
``ENABLE_TOOL_SEARCH=false``
    Without it this CLI hides MCP tools behind the ``ToolSearch`` deferral
    (``system/init`` reports ``mcp_servers: pending`` and no MCP tools), so
    denying ``ToolSearch`` would leave the agent with nothing to call. With
    it, ``init`` reports ``status: connected`` and all mock tools up front --
    which is also what a normal user's client shows.
``--strict-mcp-config``
    The developer's own ``.mcp.json`` / user-level servers are ignored.
``--setting-sources ""``
    No user/project/local settings, and no ``CLAUDE.md`` memory: the
    operator's personal instructions must not leak into a measurement of tool
    ergonomics.
``--no-session-persistence``
    Runs leave nothing in ``~/.claude/projects``; the transcript we capture
    from stdout is the record.

Note on turn caps: Claude Code 2.1.x has no ``--max-turns`` flag (verified
against ``claude --help``; it exists only in the SDK). The runaway guard is
therefore ``--max-budget-usd`` plus a wall-clock timeout enforced by the
harness, which bounds a confused agent in dollars and in seconds.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]

#: MCP server name the agent sees. Tool names become ``mcp__<name>__<tool>``.
SERVER_NAME = "gdocsmock"

#: Tool groups the mock server registers (``main.py --tools ...``).
SERVER_TOOL_GROUPS = ("docs", "docs_preview")

#: Env vars that would flip the server into a different mode if inherited
#: from the operator's shell. MCP config env is merged into the parent
#: environment rather than replacing it, so these are pinned, not popped.
SERVER_ENV_PINS = {
    "MCP_ENABLE_OAUTH21": "false",
    "MCP_SINGLE_USER_MODE": "1",
    "WORKSPACE_MCP_READ_ONLY": "false",
}

#: Every built-in tool Claude Code 2.1.x can expose, denied so the agent is
#: left with the mock MCP server and nothing else. Names it does not know are
#: ignored, so the list is deliberately a superset (it must stay one as the
#: CLI grows built-ins).
#:
#: **A deny list is a promise, not a guarantee.** Batch ``20260730-224247``
#: proved it: five built-ins the list did not know about were advertised to
#: the agent (``AskUserQuestion``, ``EnterPlanMode``, ``Monitor``,
#: ``PushNotification``, ``RemoteTrigger``), and two runs spent turns calling
#: ``Monitor`` -- which the ``dontAsk`` permission mode then denied at call
#: time -- before ending by asking the absent operator to paste in a file.
#: The names below have been added, but the structural fix is elsewhere:
#: :mod:`llmux.runner.toolprobe` asks a real spawned agent what it can see,
#: and every real run's ``system``/``init`` event is checked for non-MCP
#: names so leakage is REPORTED even when this tuple has fallen behind.
BUILTIN_TOOLS_DENIED = (
    "Agent",
    "Artifact",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "ListMcpResourcesTool",
    "Monitor",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "PushNotification",
    "Read",
    "ReadMcpResourceDirTool",
    "ReadMcpResourceTool",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "SendUserMessage",
    "Skill",
    "SlashCommand",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
)

#: Env overrides for the ``claude`` process itself (not the MCP server).
AGENT_ENV_PINS = {
    # Present MCP tools directly instead of behind the ToolSearch deferral.
    "ENABLE_TOOL_SEARCH": "false",
    # Keep the runs off the operator's telemetry/update paths.
    "DISABLE_AUTOUPDATER": "1",
}


def build_agent_env(base_env: dict[str, str]) -> dict[str, str]:
    """Environment for the ``claude`` subprocess."""
    return {**base_env, **AGENT_ENV_PINS}


def build_mcp_config(
    seed_path: Path,
    state_dump_path: Path,
    *,
    credentials_dir: Path,
    me: str = "mockuser",
    server_name: str = SERVER_NAME,
    tool_groups: tuple[str, ...] = SERVER_TOOL_GROUPS,
    python: Optional[str] = None,
    repo_root: Path = REPO_ROOT,
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """The ``mcpServers`` document for one run."""
    env = {
        "MOCKDOCS_SEED": str(seed_path),
        "MOCKDOCS_STATE_DUMP": str(state_dump_path),
        "MOCKDOCS_ME": me,
        "USER_GOOGLE_EMAIL": f"{me}@example.com",
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(credentials_dir),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        **SERVER_ENV_PINS,
    }
    env.update(extra_env or {})
    return {
        "mcpServers": {
            server_name: {
                "command": python or sys.executable,
                "args": [
                    str(repo_root / "mockdocs" / "serve.py"),
                    "--transport",
                    "stdio",
                    "--single-user",
                    "--tools",
                    *tool_groups,
                ],
                "env": env,
            }
        }
    }


def write_mcp_config(path: Path, config: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def allowed_tool_patterns(server_name: str = SERVER_NAME) -> list[str]:
    """Permission patterns granting exactly the mock server's tools.

    Both spellings are passed: ``mcp__<server>`` (every tool on that server)
    and ``mcp__<server>__*`` (the glob form). Whichever the running CLI
    understands, the grant is the same set and nothing wider.
    """
    return [f"mcp__{server_name}", f"mcp__{server_name}__*"]


def build_claude_argv(
    prompt: str,
    *,
    model: str,
    mcp_config_path: Path,
    server_name: str = SERVER_NAME,
    max_budget_usd: Optional[float] = 1.0,
    append_system_prompt: Optional[str] = None,
    claude_bin: str = "claude",
) -> list[str]:
    """The exact command line for one headless run."""
    argv = [
        claude_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools",
        *allowed_tool_patterns(server_name),
        "--disallowedTools",
        *BUILTIN_TOOLS_DENIED,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max_budget_usd:g}"]
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    return argv


def claude_available(claude_bin: str = "claude") -> Optional[str]:
    """Absolute path of the CLI, or ``None`` when it is not installed."""
    return shutil.which(claude_bin)


def probe_server(config: dict[str, Any], *, timeout: float = 60.0) -> list[str]:
    """Start the configured mock server and return its tool names.

    A raw stdio JSON-RPC handshake (initialize -> initialized -> tools/list),
    deliberately not the fastmcp client: this is what ``--dry-run`` uses to
    prove the wiring works, so it should share as little machinery with the
    tests as possible and cost nothing.
    """
    import os
    import subprocess

    (name, spec), *_ = config["mcpServers"].items()
    env = {**os.environ, **(spec.get("env") or {})}
    process = subprocess.Popen(
        [spec["command"], *spec.get("args", [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    try:

        def send(payload: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def recv() -> dict[str, Any]:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline()
                if not line:
                    stderr = (process.stderr.read() if process.stderr else "") or ""
                    raise RuntimeError(
                        f"mock MCP server {name!r} exited before replying: "
                        f"{stderr[-800:]}"
                    )
                line = line.strip()
                if not line:
                    continue
                message = json.loads(line)
                if "id" in message:
                    return message

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "llmux-dry-run", "version": "1"},
                },
            }
        )
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = recv()
        tools = ((listed.get("result") or {}).get("tools")) or []
        return sorted(str(t.get("name")) for t in tools)
    finally:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
