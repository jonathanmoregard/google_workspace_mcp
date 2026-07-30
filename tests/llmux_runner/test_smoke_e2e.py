"""ONE real headless run: `claude` -> real MCP server -> mock -> grade.

This is the only test in the suite that spends money, so it is opt-in:

    LLMUX_SMOKE=1 uv run pytest tests/llmux_runner/test_smoke_e2e.py

Skipped (never failed) when the CLI is absent, unauthenticated, or the opt-in
is not set -- a laptop without a Claude subscription must still get a green
suite.

What it asserts is the *harness contract*, not the model's competence: the
MCP server connects, the agent's tools are the mock's tools and nothing else,
the end state comes back out of the state dump, and a report is written.
Whether the agent got the task right is the measurement, not the test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llmux.runner import run as run_mod
from llmux.runner import session

pytestmark = pytest.mark.llmux_smoke

AUTH_HINTS = ("not authenticated", "please run", "login", "credentials", "api key")


def session_tool_allowlist() -> frozenset[str]:
    """Client-internal tools that are allowed to appear alongside the mock."""
    from llmux.runner.transcript import HARNESS_TOOL_NAMES

    return HARNESS_TOOL_NAMES


def _skip_unless_enabled() -> None:
    if not os.getenv("LLMUX_SMOKE"):
        pytest.skip(
            "real Claude CLI run: costs tokens. Set LLMUX_SMOKE=1 to enable."
        )
    if not session.claude_available():
        pytest.skip("the `claude` CLI is not on PATH")


def test_one_real_run_against_a_fixture_scenario(tmp_path, capsys):
    _skip_unless_enabled()

    exit_code = run_mod.main(
        [
            "--fixtures",
            "--scenario",
            "fx-anchored-comment",
            "--models",
            "sonnet",
            "--limit",
            "1",
            "--concurrency",
            "1",
            "--timeout",
            "600",
            "--max-budget-usd",
            "1.5",
            "--yes",
            "--reports-dir",
            str(tmp_path),
        ]
    )
    printed = capsys.readouterr().out

    reports = sorted(tmp_path.glob("*.json"))
    if not reports:
        pytest.fail(f"no report was written; runner said:\n{printed}")
    payload = json.loads(reports[0].read_text())
    run = payload["runs"][0]
    transcript = run["transcript"]

    if run["harness_error"] and any(
        hint in run["harness_error"].lower() for hint in AUTH_HINTS
    ):
        pytest.skip(f"Claude CLI is not authenticated: {run['harness_error']}")

    assert transcript["mcp_connected"], (
        "the mock MCP server never became reachable; the run measured nothing. "
        f"servers={transcript['mcp_servers']}"
    )
    tools = [
        t
        for t in transcript["available_tools"]
        if t not in session_tool_allowlist()
    ]
    assert all(t.startswith("mcp__gdocsmock__") for t in tools), (
        "the agent had built-in tools available: "
        f"{sorted(t for t in tools if not t.startswith('mcp__'))}"
    )
    # init can fire before the server finishes connecting, so an empty MCP
    # list here is not a failure -- the calls below are the real proof.
    for required in (
        "mcp__gdocsmock__create_anchored_doc_comment",
        "mcp__gdocsmock__list_document_suggestions",
    ):
        assert required in tools or not tools

    calls = transcript["tool_calls"]
    assert calls, "the agent never called a tool"
    assert any(c["ok"] for c in calls), "no tool call succeeded"

    # The end state came back out of the server process and was graded.
    assert run["grader_error"] is None
    assert isinstance(run["pass"], bool)
    assert 0.0 <= run["score"] <= 1.0
    assert exit_code == 0

    markdown = (Path(reports[0]).with_suffix(".md")).read_text()
    assert "## Mistake taxonomy" in markdown
    assert "fx-anchored-comment" in markdown
