"""Spawn the real MCP server as a stdio subprocess and drive it as a client.

TRUE blackbox: the server is ``python main.py --transport stdio
--single-user --tools docs docs_preview``. The GA ``docs`` service
provides scratch-doc creation (``create_doc``), text modification
(``modify_doc_text``) and the Drive-backed comment factory tools
(``list_document_comments`` / ``manage_document_comment``); the
``docs_preview`` service registers the 7 hand-written review tools. We
talk MCP protocol through the fastmcp client (already a repo
dependency, fastmcp>=3.4.4).

The fastmcp client is async; tests are plain sync functions. A dedicated
background event loop per session keeps pytest free of event-loop-scope
concerns.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StdioTransport

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "e2e" / "_artifacts"

#: Host env vars that would change the server's mode if inherited.
_CONFLICTING_ENV_VARS = (
    "WORKSPACE_MCP_TOOLS",
    "WORKSPACE_MCP_TOOL_TIER",
    "WORKSPACE_MCP_READ_ONLY",
    "WORKSPACE_MCP_PERMISSIONS",
    "WORKSPACE_MCP_TRANSPORT",
    "WORKSPACE_MCP_CREDENTIALS_DIR",
    "GOOGLE_MCP_CREDENTIALS_DIR",
    "MCP_SINGLE_USER_MODE",
    "MCP_ENABLE_OAUTH21",
    "USER_GOOGLE_EMAIL",
)


def build_server_args(
    tools: tuple[str, ...] = ("docs", "docs_preview"),
) -> list[str]:
    """CLI args for the server subprocess (pure; unit-tested)."""
    return ["main.py", "--transport", "stdio", "--single-user", "--tools", *tools]


def build_server_env(
    credentials_dir: str,
    user_email: str,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Subprocess env: host env minus mode-flipping vars, plus our pins.

    (The mcp SDK replaces - not merges - the child env, so we must pass a
    complete environment including PATH/HOME.)
    """
    env = dict(os.environ if base_env is None else base_env)
    for var in _CONFLICTING_ENV_VARS:
        env.pop(var, None)
    env["WORKSPACE_MCP_CREDENTIALS_DIR"] = credentials_dir
    env["USER_GOOGLE_EMAIL"] = user_email
    return env


def tool_text(result: CallToolResult) -> str:
    """Concatenated text blocks of a tool result."""
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def tool_json(result: CallToolResult) -> Any:
    """Parse the tool result text as JSON.

    The docs_preview review tools emit JSON strings; the GA docs-service
    tools return human-readable confirmations - read those with
    :func:`tool_text` instead.
    """
    text = tool_text(result)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"Tool did not return JSON: {text[:400]!r}") from exc


class ServerSession:
    """Owns one server subprocess + fastmcp client on a background loop."""

    def __init__(
        self,
        credentials_dir: str,
        user_email: str,
        tools: tuple[str, ...] = ("docs", "docs_preview"),
    ) -> None:
        self.credentials_dir = credentials_dir
        self.user_email = user_email
        self.tools = tools
        self.log_path = ARTIFACTS_DIR / f"server-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Client | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self, timeout: float = 90.0) -> None:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="e2e-mcp-loop", daemon=True
        )
        self._thread.start()
        transport = StdioTransport(
            command=sys.executable,
            args=build_server_args(self.tools),
            env=build_server_env(self.credentials_dir, self.user_email),
            cwd=str(REPO_ROOT),
            log_file=self.log_path,
        )
        self._client = Client(transport, init_timeout=timeout)
        try:
            self._run(self._client.__aenter__(), timeout=timeout)
        except Exception as exc:
            raise RuntimeError(
                f"MCP server failed to start (stderr log: {self.log_path}): {exc}"
            ) from exc

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._run(self._client.__aexit__(None, None, None), timeout=30)
            finally:
                self._client = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self, coro: Any, timeout: float) -> Any:
        assert self._loop is not None, "session not started"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    # -- MCP operations --------------------------------------------------
    def list_tool_names(self) -> list[str]:
        tools = self._run(self._client.list_tools(), timeout=30)
        return sorted(t.name for t in tools)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> CallToolResult:
        """Call a tool; raises fastmcp ToolError if the tool errored."""
        return self._run(
            self._client.call_tool(name, arguments, timeout=timeout),
            timeout=timeout + 30,
        )

    def call_tool_raw(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> CallToolResult:
        """Call a tool without raising on error (for sad-path assertions)."""
        return self._run(
            self._client.call_tool(
                name, arguments, timeout=timeout, raise_on_error=False
            ),
            timeout=timeout + 30,
        )

    def expect_tool_error(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> str:
        """Call a tool that MUST fail; returns the error text."""
        result = self.call_tool_raw(name, arguments, timeout=timeout)
        text = tool_text(result)
        assert result.is_error, (
            f"{name} unexpectedly succeeded; response: {text[:400]!r}"
        )
        return text
