"""The runner's own orchestration: CLI parsing, dry run, and one fake run.

``execute_run`` is exercised against a stub ``claude`` (a shell script that
replays a canned stream-json transcript), so the whole pipeline -- config,
subprocess, transcript parse, state read-back, grade, classify, artifacts --
is covered without spending a token. The real CLI is covered by the smoke
test in ``test_smoke_e2e.py``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from llmux.runner import run as run_mod
from llmux.runner.run import RunOptions, build_parser, execute_run, prepare_run_dir
from llmux.runner.scenarios import load_scenario

FIXTURES = Path(__file__).resolve().parents[2] / "llmux" / "runner" / "_fixtures"


def test_parser_defaults_to_a_small_batch():
    args = build_parser().parse_args([])
    assert args.limit == 3, "a default batch must stay cheap"
    assert args.models == "sonnet"
    assert args.concurrency == 2
    assert args.dry_run is False
    assert args.corpus == run_mod.DEFAULT_CORPUS


def test_parser_flags_narrow_the_matrix():
    args = build_parser().parse_args(
        [
            "--fixtures",
            "--models",
            "sonnet,opus",
            "--limit",
            "1",
            "--scenario",
            "fx-accept-reject",
            "--concurrency",
            "9",
            "--dry-run",
        ]
    )
    assert args.fixtures is True
    assert args.models == "sonnet,opus"
    assert args.limit == 1
    assert args.scenario_ids == ["fx-accept-reject"]
    assert args.dry_run is True
    # The cap is applied at execution time, not by the parser.
    assert args.concurrency == 9
    assert run_mod.MAX_CONCURRENCY == 3


def test_dry_run_validates_every_fixture_without_spending_tokens(capsys):
    exit_code = run_mod.main(["--fixtures", "--all", "--dry-run"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "no tokens spent" in output
    assert "fx-accept-reject" in output
    assert "fx-suggest-utf16" in output
    # The dry run proves the server registers the review tools.
    assert "review)" in output


def test_missing_corpus_exits_with_a_usable_message(tmp_path, capsys):
    assert run_mod.main(["--corpus", str(tmp_path / "gone"), "--dry-run"]) == 2
    assert "scenario corpus problem" in capsys.readouterr().err


def _stub_claude(
    tmp_path: Path, transcript_lines: list[dict], state_writer: str
) -> Path:
    """A fake ``claude`` that prints a transcript and mutates the mock state.

    It receives the same argv the real CLI would, so the stub also proves the
    command line is well-formed enough to parse.
    """
    transcript = "\n".join(json.dumps(line) for line in transcript_lines)
    payload = tmp_path / "transcript.jsonl"
    payload.write_text(transcript, encoding="utf-8")
    helper = tmp_path / "mutate.py"
    helper.write_text(state_writer, encoding="utf-8")

    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'cat "{payload}"\n'
        # Find --mcp-config's value so the stub can mutate the same state dump.
        'cfg=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "--mcp-config" ]; then cfg="$2"; fi\n'
        "  shift\n"
        "done\n"
        f'"{os.environ.get("PYTHON", "python3")}" "{helper}" "$cfg"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


SOLVE_ANCHORED_COMMENT = """
import json, sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from mockdocs.state import read_state, write_state

config = json.loads(Path(sys.argv[1]).read_text())
env = config["mcpServers"]["gdocsmock"]["env"]
backend = read_state(env["MOCKDOCS_STATE_DUMP"])
backend.create_comment_thread("fx-doc-comment", "What is the source?", quote="40%")
write_state(backend, env["MOCKDOCS_STATE_DUMP"])
"""


def test_execute_run_grades_classifies_and_stores_artifacts(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[2]
    scenario = load_scenario(FIXTURES / "fx-anchored-comment")

    lines = [
        {
            "type": "system",
            "subtype": "init",
            "tools": ["mcp__gdocsmock__create_anchored_doc_comment"],
            "mcp_servers": [{"name": "gdocsmock", "status": "connected"}],
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__gdocsmock__create_anchored_doc_comment",
                        "input": {"document_id": "fx-doc-comment", "start_index": 17},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}
                ],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 3,
            "total_cost_usd": 0.24,
            "duration_ms": 9000,
            "result": "Comment added.",
        },
    ]
    monkeypatch.setenv("PYTHON", os.environ.get("PYTHON") or "python3")
    stub = _stub_claude(
        tmp_path, lines, SOLVE_ANCHORED_COMMENT.format(repo=str(repo_root))
    )

    # The state dump only exists once the server (or, here, the stub) writes
    # it, so seed it the way a live server would at startup.
    run_dir = prepare_run_dir(tmp_path / "runs", scenario.id, "sonnet")
    from mockdocs.fake_services import FakeBackend
    from mockdocs.state import write_state

    seeded = FakeBackend()
    seeded.seed(scenario.seed)
    write_state(seeded, run_dir / "state.json")

    result = execute_run(
        scenario,
        "sonnet",
        RunOptions(workdir=tmp_path / "runs", timeout_s=120, claude_bin=str(stub)),
    )

    assert result.harness_error is None
    assert result.passed is True, result.grade.failures
    assert result.grade.score == 1.0
    assert result.transcript.num_turns == 3
    assert result.transcript.cost_usd == 0.24
    assert [c.tool for c in result.transcript.tool_calls] == [
        "create_anchored_doc_comment"
    ]
    # A write with no read afterwards is a finding even on a passing run.
    assert "no_end_state_verification" in {f.code for f in result.findings}

    assert (run_dir / "transcript.jsonl").is_file()
    assert (run_dir / "mcp-config.json").is_file()
    assert json.loads((run_dir / "argv.json").read_text())[0] == str(stub)
    stored = json.loads((run_dir / "run.json").read_text())
    assert stored["pass"] is True
    assert stored["scenario_id"] == "fx-anchored-comment"
    assert stored["scenario_path"] == str(scenario.path)

    # The artifacts are self-sufficient: a report can be rebuilt from them
    # after a taxonomy rule changes, without rerunning any agent.
    from llmux.runner.analyze import reanalyze

    rebuilt = reanalyze(tmp_path / "runs")
    assert len(rebuilt) == 1
    assert rebuilt[0].scenario_id == "fx-anchored-comment"
    assert rebuilt[0].passed is True
    assert rebuilt[0].transcript.tool_sequence() == ["create_anchored_doc_comment"]


def test_execute_run_reports_a_missing_state_dump_as_a_harness_error(tmp_path):
    scenario = load_scenario(FIXTURES / "fx-anchored-comment")
    stub = _stub_claude(tmp_path, [{"type": "result", "subtype": "success"}], "pass\n")
    result = execute_run(
        scenario,
        "sonnet",
        RunOptions(workdir=tmp_path / "runs", timeout_s=120, claude_bin=str(stub)),
    )
    assert result.passed is False
    assert "no gradeable end state" in (result.harness_error or "")


def test_execute_run_reports_a_missing_cli_instead_of_crashing(tmp_path):
    scenario = load_scenario(FIXTURES / "fx-anchored-comment")
    result = execute_run(
        scenario,
        "sonnet",
        RunOptions(
            workdir=tmp_path / "runs",
            timeout_s=5,
            claude_bin=str(tmp_path / "definitely-not-installed"),
        ),
    )
    assert result.passed is False
    assert result.harness_error
