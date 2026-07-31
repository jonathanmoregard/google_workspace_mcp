"""Ask a real spawned agent what tools it can actually see.

    uv run python -m llmux.runner.toolprobe
    uv run python -m llmux.runner.toolprobe --json

The runner's isolation story (see :mod:`llmux.runner.session`) rests on a
*deny list* of built-in tool names, and a deny list is only ever as good as
its last update. Claude Code ships new built-ins between releases; each one
is advertised to the agent until somebody adds its name here. That is not a
theoretical hazard -- batch ``20260730-224247`` measured five built-ins the
list did not know about (``AskUserQuestion``, ``EnterPlanMode``, ``Monitor``,
``PushNotification``, ``RemoteTrigger``), and two runs burned turns calling
``Monitor`` and then ended by asking the (absent) user a question.

So the deny list gets an empirical check rather than a promise: this module
spawns one real headless agent with exactly the runner's own command line,
reads the ``system``/``init`` event, and reports every advertised tool whose
name is not ``mcp__<server>__*``. Any output but "clean" means the deny list
has fallen behind the CLI and :data:`llmux.runner.session.BUILTIN_TOOLS_DENIED`
needs the reported names.

It costs one trivial turn (the prompt asks for a two-character reply), which
is the cheapest honest way to answer "what does the agent actually see?".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Sequence

from llmux.runner import session as session_mod
from llmux.runner.transcript import parse_stream_json

#: A prompt that ends the run in one turn without touching a tool.
PROBE_PROMPT = "Reply with exactly: OK. Do not call any tool."


def probe_advertised_tools(
    *,
    model: str = "haiku",
    claude_bin: str = "claude",
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Spawn one agent under the runner's isolation and list what it sees.

    Returns ``{"tools": [...], "leaked": [...], "mcp_tools": [...],
    "returncode": int, "stderr": str}``. ``leaked`` is the finding: tool
    names advertised to the agent that are not on the mock MCP server.
    """
    with tempfile.TemporaryDirectory(prefix="llmux-toolprobe-") as tmp:
        root = Path(tmp)
        (root / "cwd").mkdir()
        seed = root / "seed.json"
        # The probe never calls a tool, so the server only has to start; an
        # empty seed is a valid mockdocs seed and keeps this free of corpus
        # coupling.
        seed.write_text(json.dumps({"documents": []}), encoding="utf-8")
        config = session_mod.build_mcp_config(
            seed, root / "state.json", credentials_dir=root / "creds"
        )
        config_path = session_mod.write_mcp_config(root / "mcp-config.json", config)
        argv = session_mod.build_claude_argv(
            PROBE_PROMPT,
            model=model,
            mcp_config_path=config_path,
            max_budget_usd=0.5,
            claude_bin=claude_bin,
        )
        process = subprocess.run(
            argv,
            cwd=str(root / "cwd"),
            env=session_mod.build_agent_env(dict(os.environ)),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    transcript = parse_stream_json(process.stdout.splitlines())
    tools = list(transcript.available_tools)
    mcp_tools = [t for t in tools if t.startswith(f"mcp__{session_mod.SERVER_NAME}__")]
    leaked = sorted(set(tools) - set(mcp_tools))
    return {
        "tools": sorted(tools),
        "mcp_tools": sorted(mcp_tools),
        "leaked": leaked,
        "returncode": process.returncode,
        "stderr": process.stderr[-2000:],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="llmux.runner.toolprobe", description=__doc__)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    if not session_mod.claude_available():
        print("the `claude` CLI is not on PATH", file=sys.stderr)
        return 2
    result = probe_advertised_tools(model=args.model, timeout_s=args.timeout)
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{len(result['mcp_tools'])} MCP tool(s) advertised")
        if result["leaked"]:
            print(f"LEAKED {len(result['leaked'])} non-MCP tool(s):")
            for name in result["leaked"]:
                print(f"  - {name}")
            print(
                "\nAdd these to llmux.runner.session.BUILTIN_TOOLS_DENIED "
                "and re-run; a run that can see them is not measuring the "
                "MCP surface alone."
            )
        else:
            print("clean: the agent sees the mock MCP server and nothing else")
    return 1 if result["leaked"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
