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

from llmux.runner import interference as interference_mod
from llmux.runner import session as session_mod
from llmux.runner.interference import InterferenceReport
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


#: A run's verdict, and the ONLY definition of one -- ``grade.passed`` is an
#: input to it, never a substitute for it (``llmux.runner.analyze`` used to
#: read ``passed`` where this module read ``outcome``, which counted every
#: INCONCLUSIVE run as a pass).
#:
#: ``INCONCLUSIVE`` is the third state the batch needed: a run that was
#: throttled, killed by the wall clock, or handed a contaminated tool surface
#: did not measure the tools, and scoring it either way puts the harness into
#: the capability curve. It is excluded from every pass rate and every mean
#: score, and counted on its own, so a batch with three of them reads as a
#: batch with three fewer measurements rather than three more passes.
OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"


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
    #: Something went wrong with the RUN: the CLI would not start, the wall
    #: clock killed it, there was no gradeable end state, the interleaving
    #: broke a spec invariant. The run did not measure the tools, so it is
    #: INCONCLUSIVE.
    harness_error: Optional[str] = None
    #: Something went wrong AFTER the grade, classifying an already-graded
    #: run. The taxonomy is the most volatile code in the harness and it runs
    #: once the money is spent; a crash in it costs the mistake labels, not
    #: the measurement. Deliberately NOT a reason for INCONCLUSIVE -- doing
    #: that converts a genuine agent FAIL into "not a capability result",
    #: which is the same inflation from the other direction.
    analysis_error: Optional[str] = None
    scenario_path: Optional[Path] = None
    #: What the scripted second editor did, when the scenario declared one.
    interference: Optional[dict[str, Any]] = None
    #: Non-MCP tools this run was offered or reached for. Advertising one
    #: wastes the agent's turns; calling one means the run left the surface.
    contamination: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.grade.passed

    @property
    def inconclusive_reasons(self) -> list[str]:
        """Why this run's grade should not be read as a capability result.

        Empty for a clean run, which is the only kind whose pass/fail is
        quoted without a caveat. Note what is NOT here: a crash while
        classifying an already-graded run (:attr:`analysis_error`). The agent
        did the work and the grader read the end state; losing the mistake
        labels afterwards is a gap in the taxonomy, not a reason to throw
        away a measurement that was paid for.
        """
        reasons: list[str] = []
        if self.transcript.rate_limited:
            for info in self.transcript.throttling_events:
                reasons.append(
                    f"rate limited: status={info.get('status')!r} "
                    f"type={info.get('rateLimitType')!r} "
                    f"resets_at={info.get('resetsAt')!r}"
                )
            if not self.transcript.throttling_events:
                reasons.append(
                    "the CLI's terminal reason names a usage limit: "
                    f"{self.transcript.subtype!r}/{self.transcript.terminal_reason!r}"
                )
        if self.timed_out:
            reasons.append(
                "killed by the harness wall clock, so what it would have "
                "scored is unknown"
            )
        advertised = self.transcript.leaked_builtin_tools(session_mod.SERVER_NAME)
        if advertised:
            reasons.append(
                "the tool surface was contaminated: non-MCP tool(s) "
                + ", ".join(advertised)
                + " were ADVERTISED to the agent. A denied call is not a "
                "clean run -- in batch 20260730-224247 `Monitor` was "
                "advertised and then refused by --permission-mode dontAsk, "
                "and the agent spent its turns discovering that instead of "
                "reviewing, then ended by asking the absent operator a "
                "question; the batch graded it normally and exited 0. Add "
                "the names to llmux.runner.session.BUILTIN_TOOLS_DENIED and "
                "confirm with `uv run python -m llmux.runner.toolprobe`."
            )
        escaped = [
            call.name
            for call in self.transcript.tool_calls
            if call.server is None and not call.is_harness and call.answered
            and not call.failed
        ]
        if escaped:
            reasons.append(
                "succeeded in calling non-MCP tool(s) outside the measured "
                "surface: " + ", ".join(sorted(set(escaped)))
            )
        if self.harness_error and not reasons:
            reasons.append(self.harness_error)
        return reasons

    @property
    def outcome(self) -> str:
        if self.inconclusive_reasons:
            return OUTCOME_INCONCLUSIVE
        return OUTCOME_PASS if self.passed else OUTCOME_FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "model": self.model,
            "difficulty": self.difficulty,
            "pass": self.grade.passed,
            "outcome": self.outcome,
            "inconclusive_reasons": self.inconclusive_reasons,
            "score": self.grade.score,
            "failures": list(self.grade.failures),
            "grader_error": self.grade.error,
            "wall_s": round(self.wall_s, 2),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "harness_error": self.harness_error,
            "analysis_error": self.analysis_error,
            "contamination": list(self.contamination),
            "artifacts": str(self.artifacts) if self.artifacts else None,
            # Recorded so a report can be rebuilt from artifacts alone after
            # the taxonomy rules change (see analyze.reanalyze).
            "scenario_path": str(self.scenario_path) if self.scenario_path else None,
            "interference": self.interference,
            "findings": [f.as_dict() for f in self.findings],
            "transcript": self.transcript.as_dict(),
        }


def detect_contamination(transcript: Transcript) -> list[str]:
    """Anything in this transcript that is not the MCP surface under test.

    Read straight off the CLI's own ``system``/``init`` tool list and the
    agent's own calls, so it stays true even when the deny list in
    :mod:`llmux.runner.session` has fallen behind a CLI release -- which is
    exactly how batch ``20260730-224247`` shipped with five undenied
    built-ins nobody noticed until a run ended by asking a question.
    """
    notes: list[str] = []
    advertised = transcript.leaked_builtin_tools(session_mod.SERVER_NAME)
    if advertised:
        notes.append(
            f"{len(advertised)} non-MCP tool(s) advertised to the agent: "
            + ", ".join(advertised)
            + " (add them to llmux.runner.session.BUILTIN_TOOLS_DENIED)"
        )
    called = transcript.builtin_tools_called()
    if called:
        notes.append("agent called non-MCP tool(s): " + ", ".join(called))
    return notes


def preflight_toolprobe(
    *, model: str = "haiku", timeout_s: float = 180.0, claude_bin: str = "claude"
) -> tuple[bool, list[str], Optional[str]]:
    """Ask a real spawned agent what it can see, BEFORE the batch spends money.

    ``(clean, leaked, error)``. The deny list in :mod:`llmux.runner.session`
    is a promise, not a guarantee -- Claude Code ships built-ins between
    releases and each one is advertised until somebody adds its name -- and
    every run against a stale list is INCONCLUSIVE. Discovering that after
    $17 of agents is the expensive way to learn it; the probe costs one
    haiku turn.

    A probe that cannot run at all is NOT a reason to refuse the batch (the
    per-run ``system``/``init`` check still catches leakage after the fact),
    so its own failure comes back as ``error`` rather than as a verdict.
    """
    from llmux.runner.toolprobe import probe_advertised_tools

    try:
        result = probe_advertised_tools(
            model=model, timeout_s=timeout_s, claude_bin=claude_bin
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        return True, [], f"{type(exc).__name__}: {exc}"
    leaked = list(result.get("leaked") or [])
    return not leaked, leaked, None


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
    # The interference script is the runner's, not the scenario's env: a
    # scenario may ask for mock *modes* through meta.env, never for the paths
    # the harness owns. Merged last so it wins outright.
    interference_env = interference_mod.materialise(scenario, run_dir)
    config = session_mod.build_mcp_config(
        scenario.seed_path.resolve(),
        state_path,
        credentials_dir=run_dir / "creds",
        me=scenario.me,
        extra_env={**scenario.server_env, **interference_env},
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

    backend, grade = _load_and_grade(scenario, state_path)
    if grade.error and harness_error is None and not state_path.exists():
        harness_error = grade.error

    report = InterferenceReport.from_backend(backend) if backend is not None else None
    if report is not None and report.violations and harness_error is None:
        # A broken interleaving means the run measured nothing. Surface it
        # where a reader cannot miss it rather than letting it read as a
        # failed agent.
        harness_error = "interference broke a spec invariant: " + "; ".join(
            report.violations[:3]
        )

    contamination = detect_contamination(transcript)

    # Classification is the most volatile code in the harness -- the taxonomy
    # changes whenever we learn something -- and it runs AFTER the money has
    # been spent. It must never be the reason a paid-for run is lost, and it
    # is recorded apart from harness_error: folding it in there made a crash
    # in the labeller turn a genuine agent FAIL into INCONCLUSIVE, i.e. into
    # "not a capability result", which hides real failures.
    analysis_error: Optional[str] = None
    try:
        findings = classify(
            ScenarioFacts.from_scenario(scenario),
            transcript,
            passed=grade.passed,
            failures=grade.failures,
            timed_out=timed_out,
            interference=report,
        )
    except Exception as exc:  # noqa: BLE001 - see comment above
        findings = []
        analysis_error = (
            f"classification failed ({type(exc).__name__}: {exc}); the grade "
            "and the transcript are unaffected, only the mistake labels are "
            "missing"
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
        analysis_error=analysis_error,
        scenario_path=scenario.path,
        interference=report.as_dict() if report is not None else None,
        contamination=contamination,
    )
    _write_run_record(run_dir, result)
    return result


def _write_run_record(run_dir: Path, result: RunResult) -> None:
    """Persist one run the moment it finishes.

    This file is the batch's durable memory: ``analyze.reanalyze`` rebuilds a
    whole report from these plus the transcripts, so a crash in aggregation
    costs a command, not a batch.
    """
    try:
        payload = json.dumps(result.as_dict(), indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - a serialisable subset beats nothing
        payload = json.dumps(
            {
                "scenario_id": result.scenario_id,
                "model": result.model,
                "pass": bool(result.passed),
                "score": float(result.grade.score),
                # Keep the one field analyze.reanalyze cannot work without,
                # so even a half-written record stays rebuildable.
                "scenario_path": (
                    str(result.scenario_path) if result.scenario_path else None
                ),
                "timed_out": bool(result.timed_out),
                "harness_error": result.harness_error,
                "record_error": f"{type(exc).__name__}: {exc}",
            },
            indent=2,
        )
    (run_dir / "run.json").write_text(payload, encoding="utf-8")


def _load_and_grade(
    scenario: Scenario, state_path: Path
) -> tuple[Optional[Any], GradeResult]:
    """Read the server's end-state dump once, and grade it.

    The backend comes back too, because under interference the same snapshot
    also carries the interleaving (``backend.concurrency``) and re-reading
    the file to get at it would risk grading one state and classifying
    another.
    """
    from mockdocs.state import StateFormatError, read_state

    try:
        backend = read_state(state_path)
    except StateFormatError as exc:
        return None, GradeResult.crashed(f"no gradeable end state: {exc}")
    return backend, grade_backend(scenario, backend)


def _grade_from_state(scenario: Scenario, state_path: Path) -> GradeResult:
    """Read the server's end-state dump and grade it."""
    return _load_and_grade(scenario, state_path)[1]


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
    # Missing claude is reported, not counted as a dry-run failure: a dry
    # run's job is to validate scenarios, servers and graders without
    # spending tokens, and it must be runnable on machines without the CLI
    # installed (CI is the obvious case). A real run's `--models` path will
    # still refuse to spawn without it.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="llmux-dry-") as tmp:
        root = Path(tmp)
        for scenario in scenarios:
            run_dir = prepare_run_dir(root, scenario.id, "dry")
            state_path = run_dir / "state.json"
            # Validating the interference script here is the whole point of a
            # dry run: a malformed second editor should fail before tokens,
            # not halfway through a batch.
            try:
                interference_env = interference_mod.materialise(scenario, run_dir)
            except interference_mod.InterferenceError as exc:
                print(f"  [FAIL] {scenario.id}: bad interference script: {exc}")
                failures += 1
                continue
            config = session_mod.build_mcp_config(
                scenario.seed_path.resolve(),
                state_path,
                credentials_dir=run_dir / "creds",
                me=scenario.me,
                extra_env={**scenario.server_env, **interference_env},
            )
            session_mod.write_mcp_config(run_dir / "mcp-config.json", config)
            argv = session_mod.build_claude_argv(
                scenario.brief,
                model=models[0],
                mcp_config_path=run_dir / "mcp-config.json",
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
            declared = interference_mod.declared_interferences(scenario)
            suffix = (
                f", {len(declared)} interference(s): "
                + ", ".join(i.name for i in declared)
                if declared
                else ""
            )
            print(
                f"  [{status}] {scenario.id} ({scenario.difficulty}): "
                f"{len(tools)} tools ({len(review_tools)} review), "
                f"seed grade pass={grade.passed} score={grade.score:.2f}, "
                f"argv={len(argv)} args{suffix}"
            )
            if grade.error:
                failures += 1
                print(f"      {grade.error}")
    print(
        "dry run "
        + ("FAILED" if failures else "clean")
        + f" ({failures} problem(s)); no tokens spent"
    )
    print(
        "  note: the deny-list check is NOT part of this -- it has to spawn a "
        "real agent, which a dry run must not do. A real batch runs it "
        "automatically; run it alone with `uv run python -m "
        "llmux.runner.toolprobe`."
    )
    return 1 if failures else 0


def run_batch(
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    options: RunOptions,
    *,
    concurrency: int = 2,
) -> list[RunResult]:
    """Run the full matrix, at most ``concurrency`` agents at a time.

    A run that blows up inside the harness comes back as a recorded
    ``INCONCLUSIVE`` result rather than an exception: one bad run must not
    take down the fifteen that already cost money and finished.
    """
    jobs = [(scenario, model) for model in models for scenario in scenarios]
    workers = max(1, min(concurrency, MAX_CONCURRENCY))
    results: list[RunResult] = []

    def one(job: tuple[Scenario, str]) -> RunResult:
        scenario, model = job
        print(f"  -> {scenario.id} [{model}] starting", flush=True)
        try:
            result = execute_run(scenario, model, options)
        except Exception as exc:  # noqa: BLE001 - see docstring
            import traceback

            traceback.print_exc()
            return RunResult(
                scenario_id=scenario.id,
                model=model,
                difficulty=scenario.difficulty,
                grade=GradeResult.crashed(f"{type(exc).__name__}: {exc}"),
                transcript=Transcript(),
                harness_error=f"the harness raised: {type(exc).__name__}: {exc}",
                scenario_path=scenario.path,
            )
        for note in result.contamination:
            print(f"     ! {scenario.id} [{model}] {note}", flush=True)
        for reason in result.inconclusive_reasons:
            print(f"     ! {scenario.id} [{model}] INCONCLUSIVE: {reason}", flush=True)
        print(
            f"  <- {scenario.id} [{model}] "
            f"{result.outcome} "
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
    parser.add_argument(
        "--skip-toolprobe",
        action="store_true",
        help="do not check the deny list against a real agent before the "
        "batch (the check costs one haiku turn and refuses a batch whose "
        "runs would all be INCONCLUSIVE)",
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

    # Before the money: what does an agent under this exact isolation
    # actually see? Every run against a stale deny list is INCONCLUSIVE, so
    # a batch that starts against one buys nothing.
    if args.skip_toolprobe:
        print("tool probe: SKIPPED (--skip-toolprobe)")
    else:
        print("tool probe: asking one agent what tools it can see ...", flush=True)
        clean, leaked, probe_error = preflight_toolprobe()
        if probe_error:
            print(f"  ! the probe itself could not run ({probe_error}); "
                  "continuing -- every run's system/init is still checked")
        elif clean:
            print("  clean: the agent sees the mock MCP server and nothing else")
        else:
            print(
                f"  LEAKED {len(leaked)} non-MCP tool(s): {', '.join(leaked)}\n"
                "\nEvery run in this batch would be INCONCLUSIVE: a surface "
                "with built-ins on it is not the surface under measurement, "
                "whether or not the agent gets permission to call them. Add "
                "these names to llmux.runner.session.BUILTIN_TOOLS_DENIED and "
                "re-run, or pass --skip-toolprobe to spend the money anyway.",
                file=sys.stderr,
            )
            return 2

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

    # The runs are done and paid for. Everything below is analysis, and
    # analysis is the part that breaks -- an import error in a module a
    # concurrent edit was halfway through once destroyed a finished batch's
    # report. So: report failures are caught, the raw records are written
    # regardless, and the operator is told the one command that rebuilds
    # everything from the per-run artifacts already on disk.
    report_paths: dict[str, Path] = {}
    report_error: Optional[str] = None
    try:
        from llmux.runner.analyze import write_report

        report_paths = write_report(results, reports_dir, stamp=stamp, corpus=str(corpus))
    except Exception as exc:  # noqa: BLE001 - see comment above
        import traceback

        traceback.print_exc()
        report_error = f"{type(exc).__name__}: {exc}"
        report_paths = _write_raw_fallback(results, reports_dir, stamp)

    for label in ("markdown", "json", "raw"):
        if label in report_paths:
            print(f"{label + ':':9s} {report_paths[label]}")
    passed = sum(1 for r in results if r.outcome == OUTCOME_PASS)
    inconclusive = [r for r in results if r.outcome == OUTCOME_INCONCLUSIVE]
    conclusive = len(results) - len(inconclusive)
    print(f"{passed}/{conclusive} conclusive runs passed")
    if inconclusive:
        print(
            f"{len(inconclusive)} run(s) INCONCLUSIVE, excluded from that "
            "rate (not a capability result -- rate limit, wall clock, or a "
            "contaminated tool surface):"
        )
        for result in inconclusive:
            print(
                f"  - {result.scenario_id} [{result.model}]: "
                + "; ".join(result.inconclusive_reasons)
            )
    if report_error:
        print(
            f"\nthe report could not be built ({report_error}); the runs "
            "themselves are intact. Rebuild with:\n"
            f"  uv run python -m llmux.runner.analyze {workdir}",
            file=sys.stderr,
        )
        return 3
    return 0


def _write_raw_fallback(
    results: Sequence[RunResult], reports_dir: Path, stamp: str
) -> dict[str, Path]:
    """Dump whatever the runs produced when the report itself blew up."""
    raw_path = Path(reports_dir) / f"{stamp}-raw.json"
    records: list[dict[str, Any]] = []
    for result in results:
        try:
            records.append(result.as_dict())
        except Exception as exc:  # noqa: BLE001 - one bad run, not the batch
            records.append(
                {
                    "scenario_id": getattr(result, "scenario_id", "?"),
                    "model": getattr(result, "model", "?"),
                    "record_error": f"{type(exc).__name__}: {exc}",
                }
            )
    try:
        raw_path.write_text(
            json.dumps({"stamp": stamp, "runs": records}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - the per-run artifacts still exist
        return {}
    return {"raw": raw_path}


if __name__ == "__main__":
    raise SystemExit(main())
