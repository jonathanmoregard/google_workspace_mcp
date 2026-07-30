"""Drive headless Claude agents through the scenario corpus.

    uv run python -m llmux.runner.run --corpus llmux/runner/_fixtures \
        --models sonnet --limit 1

One run is: a fresh mock-backed MCP server seeded from the scenario, a
headless ``claude -p`` process whose only capability is that server, and --
when it stops -- the backend's end state read back out of the server's state
dump and handed to the scenario's ``grade(backend)``.

Cost control is first-class, because every run spends real tokens: default
batch is small, ``--dry-run`` proves the whole wiring for free, ``--limit`` /
``--models`` / ``--scenario`` narrow the matrix, concurrency is hard-capped,
and an estimate is printed (and, on a tty, confirmed) before anything runs.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from llmux.runner import session as session_mod
from llmux.runner.scenarios import (
    GradeResult,
    Scenario,
    ScenarioContractError,
    discover,
    grade_backend,
)
from llmux.runner.taxonomy import Finding, ScenarioFacts, classify
from llmux.runner.transcript import Transcript, parse_stream_json

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = REPO_ROOT / "llmux" / "scenarios" / "generated"
FIXTURE_CORPUS = REPO_ROOT / "llmux" / "runner" / "_fixtures"
REPORTS_DIR = REPO_ROOT / "llmux" / "runner" / "reports"

#: Never more than this many agents at once, whatever --concurrency says:
#: each one is a Claude session plus a Python MCP server, and the point of
#: the harness is measurement, not throughput.
MAX_CONCURRENCY = 3

#: Rough USD per run, measured on the fixture corpus (sonnet: ~$0.25 for a
#: 3-turn run). Only ever used for the pre-flight warning.
COST_PER_RUN_USD = {
    "haiku": 0.05,
    "sonnet": 0.30,
    "opus": 1.20,
    "fable": 1.20,
}
DEFAULT_COST_PER_RUN_USD = 0.50


@dataclass
class RunOptions:
    """Everything a single run needs that is not the scenario or the model."""

    workdir: Path
    timeout_s: float = 600.0
    max_budget_usd: Optional[float] = 1.0
    append_system_prompt: Optional[str] = None
    claude_bin: str = "claude"
    keep_going: bool = True


@dataclass
class RunResult:
    """One (scenario, model) run: what happened and how it was graded."""

    scenario_id: str
    model: str
    difficulty: str
    grade: GradeResult
    transcript: Transcript
    findings: list[Finding] = field(default_factory=list)
    wall_s: float = 0.0
    returncode: Optional[int] = None
    timed_out: bool = False
    artifacts: Optional[Path] = None
    harness_error: Optional[str] = None
    scenario_path: Optional[Path] = None

    @property
    def passed(self) -> bool:
        return self.grade.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "model": self.model,
            "difficulty": self.difficulty,
            "pass": self.grade.passed,
            "score": self.grade.score,
            "failures": list(self.grade.failures),
            "grader_error": self.grade.error,
            "wall_s": round(self.wall_s, 2),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "harness_error": self.harness_error,
            "artifacts": str(self.artifacts) if self.artifacts else None,
            # Recorded so a report can be rebuilt from artifacts alone after
            # the taxonomy rules change (see analyze.reanalyze).
            "scenario_path": str(self.scenario_path) if self.scenario_path else None,
            "findings": [f.as_dict() for f in self.findings],
            "transcript": self.transcript.as_dict(),
        }


def estimate_cost(scenario_count: int, models: Sequence[str]) -> float:
    return sum(
        COST_PER_RUN_USD.get(model, DEFAULT_COST_PER_RUN_USD) * scenario_count
        for model in models
    )


def prepare_run_dir(root: Path, scenario_id: str, model: str) -> Path:
    """Isolated working area for one run: agent cwd, creds, artifacts."""
    run_dir = root / f"{scenario_id}__{model}"
    (run_dir / "cwd").mkdir(parents=True, exist_ok=True)
    (run_dir / "creds").mkdir(parents=True, exist_ok=True)
    return run_dir


def execute_run(scenario: Scenario, model: str, options: RunOptions) -> RunResult:
    """Run one scenario against one model and grade the end state."""
    run_dir = prepare_run_dir(options.workdir, scenario.id, model)
    state_path = run_dir / "state.json"
    config = session_mod.build_mcp_config(
        scenario.seed_path.resolve(),
        state_path,
        credentials_dir=run_dir / "creds",
        me=scenario.me,
        extra_env=scenario.server_env,
    )
    config_path = session_mod.write_mcp_config(run_dir / "mcp-config.json", config)
    argv = session_mod.build_claude_argv(
        scenario.brief,
        model=model,
        mcp_config_path=config_path,
        max_budget_usd=options.max_budget_usd,
        append_system_prompt=options.append_system_prompt,
        claude_bin=options.claude_bin,
    )
    (run_dir / "argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")

    started = time.time()
    timed_out = False
    harness_error: Optional[str] = None
    stdout = ""
    stderr = ""
    returncode: Optional[int] = None

    try:
        process = subprocess.Popen(
            argv,
            cwd=str(run_dir / "cwd"),
            env=session_mod.build_agent_env(dict(os.environ)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        harness_error = f"cannot start the Claude CLI: {exc}"
        process = None
    if process is not None:
        try:
            stdout, stderr = process.communicate(timeout=options.timeout_s)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate()
            returncode = process.returncode
            harness_error = f"run exceeded the {options.timeout_s:g}s wall clock"
    wall = time.time() - started

    (run_dir / "transcript.jsonl").write_text(stdout, encoding="utf-8")
    if stderr:
        (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    transcript = parse_stream_json(stdout.splitlines())

    grade = _grade_from_state(scenario, state_path)
    if grade.error and harness_error is None and not state_path.exists():
        harness_error = grade.error

    findings = classify(
        ScenarioFacts.from_scenario(scenario),
        transcript,
        passed=grade.passed,
        failures=grade.failures,
        timed_out=timed_out,
    )
    result = RunResult(
        scenario_id=scenario.id,
        model=model,
        difficulty=scenario.difficulty,
        grade=grade,
        transcript=transcript,
        findings=findings,
        wall_s=wall,
        returncode=returncode,
        timed_out=timed_out,
        artifacts=run_dir,
        harness_error=harness_error,
        scenario_path=scenario.path,
    )
    (run_dir / "run.json").write_text(
        json.dumps(result.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def _grade_from_state(scenario: Scenario, state_path: Path) -> GradeResult:
    """Read the server's end-state dump and grade it."""
    from mockdocs.state import StateFormatError, read_state

    try:
        backend = read_state(state_path)
    except StateFormatError as exc:
        return GradeResult.crashed(f"no gradeable end state: {exc}")
    return grade_backend(scenario, backend)


def dry_run(scenarios: Sequence[Scenario], models: Sequence[str]) -> int:
    """Validate the whole wiring without spending a token.

    Starts each scenario's server, lists its tools, and runs the scenario's
    grader against the *seed* state -- which exercises every moving part
    except the agent itself.
    """
    failures = 0
    print(f"dry run: {len(scenarios)} scenario(s) x {len(models)} model(s)")
    claude_path = session_mod.claude_available()
    print(f"  claude CLI: {claude_path or 'NOT FOUND (real runs would fail)'}")
    if not claude_path:
        failures += 1
    import tempfile

    with tempfile.TemporaryDirectory(prefix="llmux-dry-") as tmp:
        root = Path(tmp)
        for scenario in scenarios:
            run_dir = prepare_run_dir(root, scenario.id, "dry")
            state_path = run_dir / "state.json"
            config = session_mod.build_mcp_config(
                scenario.seed_path.resolve(),
                state_path,
                credentials_dir=run_dir / "creds",
                me=scenario.me,
            )
            session_mod.write_mcp_config(run_dir / "mcp-config.json", config)
            argv = session_mod.build_claude_argv(
                scenario.brief, model=models[0], mcp_config_path=run_dir / "mcp-config.json"
            )
            try:
                tools = session_mod.probe_server(config)
            except Exception as exc:  # pragma: no cover - surfaced to the user
                print(f"  [FAIL] {scenario.id}: server did not start: {exc}")
                failures += 1
                continue
            review_tools = [t for t in tools if "suggest" in t or "review" in t]
            grade = _grade_from_state(scenario, state_path)
            status = "ok" if not grade.error else "GRADER ERROR"
            print(
                f"  [{status}] {scenario.id} ({scenario.difficulty}): "
                f"{len(tools)} tools ({len(review_tools)} review), "
                f"seed grade pass={grade.passed} score={grade.score:.2f}, "
                f"argv={len(argv)} args"
            )
            if grade.error:
                failures += 1
                print(f"      {grade.error}")
    print(
        "dry run "
        + ("FAILED" if failures else "clean")
        + f" ({failures} problem(s)); no tokens spent"
    )
    return 1 if failures else 0


def run_batch(
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    options: RunOptions,
    *,
    concurrency: int = 2,
) -> list[RunResult]:
    """Run the full matrix, at most ``concurrency`` agents at a time."""
    jobs = [(scenario, model) for model in models for scenario in scenarios]
    workers = max(1, min(concurrency, MAX_CONCURRENCY))
    results: list[RunResult] = []

    def one(job: tuple[Scenario, str]) -> RunResult:
        scenario, model = job
        print(f"  -> {scenario.id} [{model}] starting", flush=True)
        result = execute_run(scenario, model, options)
        print(
            f"  <- {scenario.id} [{model}] "
            f"{'PASS' if result.passed else 'FAIL'} "
            f"score={result.grade.score:.2f} turns={result.transcript.num_turns} "
            f"${result.transcript.cost_usd:.3f} {result.wall_s:.1f}s",
            flush=True,
        )
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(one, jobs):
            results.append(result)
    return results


def _confirm(estimate: float, assume_yes: bool) -> bool:
    print(
        f"\n!! estimated cost: ~${estimate:.2f} of API usage. "
        "This spends real tokens.\n"
    )
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("   (non-interactive: proceeding; pass --dry-run to validate for free)")
        return True
    answer = input("   proceed? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmux.runner.run",
        description="Drive headless Claude agents through the review scenarios.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"scenario corpus directory (default: {DEFAULT_CORPUS})",
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help=f"shorthand for --corpus {FIXTURE_CORPUS}",
    )
    parser.add_argument(
        "--models",
        default="sonnet",
        help="comma-separated model aliases (default: sonnet)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="max scenarios to run, cheapest difficulty first (default: 3)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario_ids",
        help="run only this scenario id (repeatable)",
    )
    parser.add_argument(
        "--difficulty",
        action="append",
        dest="difficulties",
        help="only scenarios of this difficulty (repeatable)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help=f"parallel agents (default: 2, hard cap {MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="per-run wall clock in seconds (default: 600)",
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=1.0,
        help="per-run API budget handed to the CLI (default: 1.0)",
    )
    parser.add_argument(
        "--append-system-prompt",
        default=None,
        help="extra system prompt for the agent (default: none, so the "
        "measurement sees a stock client)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"where reports and run artifacts go (default: {REPORTS_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate scenarios, servers and graders without spending tokens",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the cost confirmation prompt",
    )
    parser.add_argument("--all", action="store_true", help="ignore --limit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    corpus = FIXTURE_CORPUS if args.fixtures else args.corpus
    models = [m.strip() for m in str(args.models).split(",") if m.strip()]
    if not models:
        print("no models requested", file=sys.stderr)
        return 2

    try:
        scenarios = discover(
            corpus,
            ids=args.scenario_ids,
            difficulties=args.difficulties,
            limit=None if args.all else args.limit,
        )
    except ScenarioContractError as exc:
        print(f"scenario corpus problem: {exc}", file=sys.stderr)
        return 2
    if not scenarios:
        print(f"no scenarios matched in {corpus}", file=sys.stderr)
        return 2

    print(
        f"corpus: {corpus} ({len(scenarios)} scenario(s) selected) "
        f"models: {', '.join(models)}"
    )
    if args.dry_run:
        return dry_run(scenarios, models)

    if not session_mod.claude_available():
        print(
            "the `claude` CLI is not on PATH; install it or use --dry-run",
            file=sys.stderr,
        )
        return 2
    if not _confirm(estimate_cost(len(scenarios), models), args.yes):
        print("aborted; nothing spent")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    reports_dir = Path(args.reports_dir)
    workdir = reports_dir / stamp / "runs"
    workdir.mkdir(parents=True, exist_ok=True)
    options = RunOptions(
        workdir=workdir,
        timeout_s=args.timeout,
        max_budget_usd=args.max_budget_usd,
        append_system_prompt=args.append_system_prompt,
    )
    results = run_batch(scenarios, models, options, concurrency=args.concurrency)

    from llmux.runner.analyze import write_report

    report_paths = write_report(results, reports_dir, stamp=stamp)
    print(f"\nreport: {report_paths['markdown']}")
    print(f"json:   {report_paths['json']}")
    failed = [r for r in results if not r.passed]
    print(f"{len(results) - len(failed)}/{len(results)} runs passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
