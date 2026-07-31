"""Aggregate runs into a pass-rate table and a mistake taxonomy.

Two outputs from the same data: ``reports/<timestamp>.md`` for a human
deciding what to change about the tool surface, and ``reports/<timestamp>.json``
for anything that wants to diff two batches (did renaming that tool actually
move the wrong-tool-for-intent rate?).

The report leads with the taxonomy rather than the pass rate on purpose: a
pass rate says how the models did, the taxonomy says what the tools did to
them.

**One definition of a pass, used everywhere.** A run's verdict is its
``outcome`` (``llmux.runner.run``), never ``grade.passed`` on its own. The
two are not the same: a run that was throttled, killed by the wall clock, or
handed a contaminated tool surface is ``INCONCLUSIVE``, and its
``grade.passed`` is whatever the grader made of a document the agent may
never have finished working on. This module used ``result.passed`` while
``run.py`` used ``outcome``, so an INCONCLUSIVE run counted as a PASS in the
headline and in every pass-rate cell -- inflating exactly the number the
INCONCLUSIVE state was introduced to protect. Pass rate is therefore
``passes / (passes + fails)``, INCONCLUSIVE runs are excluded from it and
from ``mean_score``, and they are carried as their own count so nothing
about them is invisible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from llmux.runner.run import OUTCOME_FAIL, OUTCOME_INCONCLUSIVE, OUTCOME_PASS
from llmux.runner.taxonomy import (
    CLASSES,
    POSITIVE_CLASSES,
    Finding,
    repeat_report,
)

DIFFICULTY_ORDER = ("easy", "medium", "hard", "unknown")


@dataclass
class Cell:
    """Pass rate for one bucket of runs.

    ``runs`` is every run that landed in the bucket; ``passes``/``fails`` are
    the ones that produced a capability result. The rate is over the latter,
    because a batch where three runs were throttled is a batch with three
    fewer measurements, not three more passes.
    """

    runs: int = 0
    passes: int = 0
    fails: int = 0
    inconclusive: int = 0
    score_total: float = 0.0

    def add(self, outcome: str, score: float) -> None:
        self.runs += 1
        if outcome == OUTCOME_INCONCLUSIVE:
            self.inconclusive += 1
            return
        self.score_total += score
        if outcome == OUTCOME_PASS:
            self.passes += 1
        else:
            self.fails += 1

    @property
    def conclusive(self) -> int:
        return self.passes + self.fails

    @property
    def pass_rate(self) -> float:
        return self.passes / self.conclusive if self.conclusive else 0.0

    @property
    def mean_score(self) -> float:
        return self.score_total / self.conclusive if self.conclusive else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "passes": self.passes,
            "fails": self.fails,
            "inconclusive": self.inconclusive,
            "conclusive": self.conclusive,
            "pass_rate": round(self.pass_rate, 3),
            "mean_score": round(self.mean_score, 3),
        }


@dataclass
class Aggregate:
    """Everything the report renders."""

    total_runs: int = 0
    total_passes: int = 0
    total_fails: int = 0
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0
    total_turns: int = 0
    by_model: dict[str, Cell] = field(default_factory=dict)
    by_difficulty: dict[str, Cell] = field(default_factory=dict)
    by_model_difficulty: dict[str, dict[str, Cell]] = field(default_factory=dict)
    by_scenario: dict[str, dict[str, Any]] = field(default_factory=dict)
    taxonomy: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    #: The RUN went wrong: no gradeable end state, a dead CLI, a broken
    #: interleaving. These runs are INCONCLUSIVE.
    harness_errors: list[str] = field(default_factory=list)
    #: Something after the grade went wrong -- classification, or a
    #: transcript that could not be re-read. The grade stands; only the
    #: mistake labels are missing, so these runs stay in the pass rate.
    analysis_errors: list[str] = field(default_factory=list)
    #: One entry per run of a scenario that declared a concurrent editor.
    interference: list[dict[str, Any]] = field(default_factory=list)
    #: Runs whose grade is not a capability result: throttled, killed by the
    #: wall clock, or handed a contaminated tool surface. EXCLUDED from every
    #: pass rate and every mean score, and carried here so the exclusion is
    #: visible rather than silent. Counting them as passes -- which this
    #: module did, by reading ``result.passed`` where ``run.py`` read
    #: ``outcome`` -- inflates the exact number they exist to protect.
    inconclusive: list[dict[str, Any]] = field(default_factory=list)
    #: Non-MCP tools any run was offered or reached for.
    contamination: list[str] = field(default_factory=list)
    #: Problems hit while folding a run in, so a broken record costs one row
    #: rather than the whole report.
    aggregation_errors: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Passes over CONCLUSIVE runs -- see the module docstring."""
        return self.total_passes / self.conclusive_runs if self.conclusive_runs else 0.0

    @property
    def conclusive_runs(self) -> int:
        return self.total_passes + self.total_fails

    def as_dict(self) -> dict[str, Any]:
        return {
            "totals": {
                "runs": self.total_runs,
                "passes": self.total_passes,
                "fails": self.total_fails,
                "pass_rate": round(self.pass_rate, 3),
                "pass_rate_denominator": "conclusive runs (PASS + FAIL)",
                "inconclusive": len(self.inconclusive),
                "conclusive_runs": self.conclusive_runs,
                "cost_usd": round(self.total_cost_usd, 4),
                "wall_s": round(self.total_wall_s, 1),
                "turns": self.total_turns,
            },
            "inconclusive": self.inconclusive,
            "contamination": self.contamination,
            "aggregation_errors": self.aggregation_errors,
            "by_model": {k: v.as_dict() for k, v in self.by_model.items()},
            "by_difficulty": {k: v.as_dict() for k, v in self.by_difficulty.items()},
            "by_model_difficulty": {
                model: {d: c.as_dict() for d, c in cells.items()}
                for model, cells in self.by_model_difficulty.items()
            },
            "by_scenario": self.by_scenario,
            "taxonomy": self.taxonomy,
            "tool_usage": self.tool_usage,
            "harness_errors": self.harness_errors,
            "analysis_errors": self.analysis_errors,
            "interference": self.interference,
        }


def aggregate(results: Sequence[Any]) -> Aggregate:
    """Fold run results into the report model.

    Accepts anything with the ``RunResult`` shape, so the aggregation can be
    unit-tested with stubs instead of live runs.
    """
    agg = Aggregate()
    per_run_findings: list[Sequence[Finding]] = []

    for result in results:
        try:
            _fold(agg, result, per_run_findings)
        except Exception as exc:  # noqa: BLE001 - one bad row, not the report
            agg.aggregation_errors.append(
                f"{getattr(result, 'scenario_id', '?')} "
                f"[{getattr(result, 'model', '?')}]: "
                f"{type(exc).__name__}: {exc}"
            )

    try:
        agg.taxonomy = repeat_report(per_run_findings)
    except Exception as exc:  # noqa: BLE001 - the pass table still stands
        agg.aggregation_errors.append(f"taxonomy: {type(exc).__name__}: {exc}")
    return agg


def _fold(
    agg: Aggregate, result: Any, per_run_findings: list[Sequence[Finding]]
) -> None:
    """Add one run to the report model."""
    reasons = list(getattr(result, "inconclusive_reasons", ()) or ())
    outcome = getattr(result, "outcome", OUTCOME_PASS if result.passed else "FAIL")

    agg.total_runs += 1
    if outcome == OUTCOME_PASS:
        agg.total_passes += 1
    elif outcome != OUTCOME_INCONCLUSIVE:
        agg.total_fails += 1
    agg.total_cost_usd += float(result.transcript.cost_usd or 0.0)
    agg.total_wall_s += float(result.wall_s or 0.0)
    agg.total_turns += int(result.transcript.num_turns or 0)

    score = float(result.grade.score)
    agg.by_model.setdefault(result.model, Cell()).add(outcome, score)
    agg.by_difficulty.setdefault(result.difficulty, Cell()).add(outcome, score)
    agg.by_model_difficulty.setdefault(result.model, {}).setdefault(
        result.difficulty, Cell()
    ).add(outcome, score)

    entry = agg.by_scenario.setdefault(
        result.scenario_id,
        {"difficulty": result.difficulty, "runs": {}},
    )
    agent_calls = result.transcript.agent_tool_calls
    entry["runs"][result.model] = {
        "pass": bool(result.passed),
        "outcome": outcome,
        "inconclusive_reasons": reasons,
        "score": round(score, 3),
        "turns": result.transcript.num_turns,
        "cost_usd": round(float(result.transcript.cost_usd or 0.0), 4),
        "wall_s": round(float(result.wall_s or 0.0), 1),
        "tool_calls": len(agent_calls),
        "failed_tool_calls": sum(1 for c in agent_calls if c.failed),
        "failures": list(result.grade.failures),
        "codes": sorted({f.code for f in result.findings}),
        "tool_sequence": result.transcript.tool_sequence(),
    }

    for call in agent_calls:
        usage = agg.tool_usage.setdefault(
            call.tool or call.name, {"calls": 0, "errors": 0}
        )
        usage["calls"] += 1
        usage["errors"] += int(bool(call.failed))

    if reasons:
        agg.inconclusive.append(
            {
                "scenario_id": result.scenario_id,
                "model": result.model,
                "score": round(score, 3),
                "reasons": reasons,
            }
        )
    for note in getattr(result, "contamination", ()) or ():
        agg.contamination.append(f"{result.scenario_id} [{result.model}]: {note}")
    if result.harness_error:
        agg.harness_errors.append(
            f"{result.scenario_id} [{result.model}]: {result.harness_error}"
        )
    analysis_error = getattr(result, "analysis_error", None)
    if analysis_error:
        agg.analysis_errors.append(
            f"{result.scenario_id} [{result.model}]: {analysis_error}"
        )
    interference = getattr(result, "interference", None)
    if interference:
        agg.interference.append(
            {
                "scenario_id": result.scenario_id,
                "model": result.model,
                "pass": bool(result.passed),
                "outcome": outcome,
                **interference,
            }
        )
    per_run_findings.append(result.findings)


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _sorted_difficulties(names: Iterable[str]) -> list[str]:
    known = [d for d in DIFFICULTY_ORDER if d in set(names)]
    extra = sorted(set(names) - set(DIFFICULTY_ORDER))
    return known + extra


def render_markdown(agg: Aggregate, *, stamp: str, corpus: Optional[str] = None) -> str:
    """The human-facing report."""
    lines: list[str] = []
    lines.append(f"# LLM UX run report -- {stamp}")
    lines.append("")
    if corpus:
        lines.append(f"Corpus: `{corpus}`")
    lines.append(
        f"{agg.total_runs} run(s): {agg.total_passes} PASS, "
        f"{agg.total_fails} FAIL, {len(agg.inconclusive)} INCONCLUSIVE. "
        f"Pass rate {_pct(agg.pass_rate)} "
        f"({agg.total_passes}/{agg.conclusive_runs} conclusive runs; "
        "INCONCLUSIVE runs are excluded, never counted as passes). "
        f"{agg.total_turns} turns, ${agg.total_cost_usd:.2f}, "
        f"{agg.total_wall_s / 60:.1f} min wall."
    )
    lines.append("")

    if agg.inconclusive:
        lines.append("## Inconclusive runs (read these before the pass rate)")
        lines.append("")
        lines.append(
            f"{len(agg.inconclusive)} of {agg.total_runs} run(s) did not "
            "produce a capability result: the account was throttled, the "
            "harness wall clock killed the run, or the tool surface under "
            "measurement was contaminated. They are EXCLUDED from every pass "
            "rate and every mean score below -- a run that did not measure "
            "the tools cannot be evidence about the tools, in either "
            "direction -- and counted in their own column so the exclusion "
            "is never invisible. A batch with many of these has a smaller "
            "sample, not a better score."
        )
        lines.append("")
        lines.append("| scenario | model | score | why |")
        lines.append("| --- | --- | ---: | --- |")
        for entry in agg.inconclusive:
            lines.append(
                f"| {entry['scenario_id']} | {entry['model']} | "
                f"{entry['score']:.2f} | " + "; ".join(entry["reasons"]) + " |"
            )
        lines.append("")

    if agg.contamination:
        lines.append("## Tool-surface contamination")
        lines.append("")
        lines.append(
            "The agent is supposed to see the mock MCP server and nothing "
            "else. Anything listed here means a built-in leaked past "
            "`--disallowedTools` -- add the name to "
            "`llmux.runner.session.BUILTIN_TOOLS_DENIED` and confirm with "
            "`uv run python -m llmux.runner.toolprobe`."
        )
        lines.append("")
        for note in agg.contamination:
            lines.append(f"- {note}")
        lines.append("")

    if agg.aggregation_errors:
        lines.append("## Report-building problems")
        lines.append("")
        lines.append(
            "These runs finished but could not be folded into the tables "
            "below. The runs themselves are intact on disk; rebuild with "
            "`uv run python -m llmux.runner.analyze <reports>/<stamp>/runs`."
        )
        lines.append("")
        for problem in agg.aggregation_errors:
            lines.append(f"- {problem}")
        lines.append("")

    if agg.harness_errors:
        lines.append("## Harness problems (read these first)")
        lines.append("")
        lines.append(
            "These runs did not measure the tool surface -- something in the "
            "harness went wrong while the run was happening."
        )
        lines.append("")
        for problem in agg.harness_errors:
            lines.append(f"- {problem}")
        lines.append("")

    if agg.analysis_errors:
        lines.append("## Classification problems (the grades still stand)")
        lines.append("")
        lines.append(
            "Something went wrong AFTER these runs were graded -- the "
            "taxonomy, or re-reading a transcript. The agent did the work "
            "and the grader read the end state, so these runs are still in "
            "the pass rate; what is missing is their mistake labels. "
            "Treating this as 'not a capability result' would turn a real "
            "FAIL into a shrug."
        )
        lines.append("")
        for problem in agg.analysis_errors:
            lines.append(f"- {problem}")
        lines.append("")

    lines.append("## Mistake taxonomy")
    lines.append("")
    if not agg.taxonomy:
        lines.append("No findings: every run was clean.")
    else:
        lines.append(
            "| class | runs | share | occurrences | repeated in-run | systemic |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | :---: |")
        for code, entry in agg.taxonomy.items():
            lines.append(
                f"| `{code}` | {entry['runs']} | {_pct(entry['run_share'])} | "
                f"{entry['occurrences']} | {entry['repeated_within_run']} | "
                f"{'YES' if entry['systemic'] else ''} |"
            )
        lines.append("")
        lines.append(
            "*Systemic* = the class repeated inside a single run, or appeared "
            "in at least 30% of runs."
        )
        positives = sorted(POSITIVE_CLASSES & set(agg.taxonomy))
        if positives:
            lines.append("")
            lines.append(
                "Not every row here is a mistake: "
                + ", ".join(f"`{c}`" for c in positives)
                + " is a POSITIVE signal, counted so good behaviour under "
                "concurrent change is visible rather than merely absent."
            )
        lines.append("")
        for code, entry in agg.taxonomy.items():
            lines.append(f"### `{code}`")
            lines.append("")
            lines.append(CLASSES.get(code, entry.get("description") or ""))
            lines.append("")
            for example in entry.get("examples", []):
                lines.append(f"- {example}")
            lines.append("")

    if agg.interference:
        lines.append("## Concurrent-editor runs")
        lines.append("")
        lines.append(
            "A second editor worked in the document while the agent reviewed "
            "it, firing at fixed points in the agent's own call sequence. "
            "`re-read` is the mechanical test of whether the agent looked "
            "again after the change; `stale writes` are writes that SUCCEEDED "
            "carrying pre-change indexes, which is the silent case."
        )
        lines.append("")
        lines.append(
            "| scenario | model | outcome | fired (at agent call) | re-read | "
            "blind retries | stale writes |"
        )
        lines.append("| --- | --- | :---: | --- | :---: | ---: | ---: |")
        for entry in agg.interference:
            fired = ", ".join(
                f"{f['name']}@{f['at_call']}" for f in entry.get("fired") or []
            )
            outcome = entry.get("outcome") or (
                OUTCOME_PASS if entry["pass"] else OUTCOME_FAIL
            )
            lines.append(
                f"| {entry['scenario_id']} | {entry['model']} | "
                f"{outcome} | {fired or '(none)'} | "
                f"{'yes' if entry.get('reread_after_change') else 'NO'} | "
                f"{entry.get('blind_retries', 0)} | "
                f"{entry.get('stale_index_writes', 0)} |"
            )
        lines.append("")
        broken = [e for e in agg.interference if e.get("violations")]
        if broken:
            lines.append(
                "**Harness fault**: the interleaving itself broke a spec "
                "invariant in these runs, so their grades measure nothing:"
            )
            lines.append("")
            for entry in broken:
                for violation in entry["violations"]:
                    lines.append(
                        f"- {entry['scenario_id']} [{entry['model']}]: {violation}"
                    )
            lines.append("")

    lines.append("## Pass rate by model x difficulty")
    lines.append("")
    lines.append(
        "Every cell is `passes/conclusive` -- INCONCLUSIVE runs are not in "
        "the numerator or the denominator. The last column counts them, so a "
        "cell computed from two runs instead of four is visible rather than "
        "inferred."
    )
    lines.append("")
    difficulties = _sorted_difficulties(agg.by_difficulty)
    header = "| model | " + " | ".join(difficulties) + " | overall | inconclusive |"
    lines.append(header)
    lines.append(
        "| --- | " + " | ".join("---:" for _ in difficulties) + " | ---: | ---: |"
    )
    for model in sorted(agg.by_model):
        cells = agg.by_model_difficulty.get(model, {})
        row = [f"| {model} "]
        for difficulty in difficulties:
            cell = cells.get(difficulty)
            row.append(
                f"| {_pct(cell.pass_rate)} ({cell.passes}/{cell.conclusive}) "
                if cell
                else "| - "
            )
        overall = agg.by_model[model]
        row.append(
            f"| {_pct(overall.pass_rate)} ({overall.passes}/{overall.conclusive}) "
            f"| {overall.inconclusive} |"
        )
        lines.append("".join(row))
    lines.append("")

    lines.append("## Per-scenario scores")
    lines.append("")
    lines.append(
        "| scenario | difficulty | model | outcome | score | turns | calls (err) | $ |"
    )
    lines.append("| --- | --- | --- | :---: | ---: | ---: | ---: | ---: |")
    for scenario_id in sorted(agg.by_scenario):
        entry = agg.by_scenario[scenario_id]
        for model in sorted(entry["runs"]):
            run = entry["runs"][model]
            outcome = run.get("outcome") or ("PASS" if run["pass"] else "FAIL")
            lines.append(
                f"| {scenario_id} | {entry['difficulty']} | {model} | "
                f"{outcome} | {run['score']:.2f} | "
                f"{run['turns']} | {run['tool_calls']} ({run['failed_tool_calls']}) | "
                f"{run['cost_usd']:.3f} |"
            )
    lines.append("")

    failing = [
        (sid, model, run)
        for sid, entry in sorted(agg.by_scenario.items())
        for model, run in sorted(entry["runs"].items())
        if run.get("outcome") == OUTCOME_FAIL
    ]
    if failing:
        lines.append("## Failures in detail")
        lines.append("")
        lines.append(
            "Runs that measured the tools and failed. INCONCLUSIVE runs are "
            "in their own section above; a grader verdict on a run that was "
            "throttled or contaminated is not a failure of the tools."
        )
        lines.append("")
        for scenario_id, model, run in failing:
            lines.append(f"### {scenario_id} [{model}]")
            lines.append("")
            lines.append(
                "Tool sequence: "
                + (" -> ".join(run["tool_sequence"]) or "(no tool calls)")
            )
            lines.append("")
            for failure in run["failures"]:
                lines.append(f"- {failure}")
            if run["codes"]:
                lines.append("")
                lines.append("Classes: " + ", ".join(f"`{c}`" for c in run["codes"]))
            lines.append("")

    lines.append("## Tool usage")
    lines.append("")
    lines.append("| tool | calls | errors | error rate |")
    lines.append("| --- | ---: | ---: | ---: |")
    for tool, usage in sorted(
        agg.tool_usage.items(), key=lambda kv: (-kv[1]["calls"], kv[0])
    ):
        rate = usage["errors"] / usage["calls"] if usage["calls"] else 0.0
        lines.append(
            f"| `{tool}` | {usage['calls']} | {usage['errors']} | {_pct(rate)} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


def reanalyze(runs_dir: Path) -> list[Any]:
    """Rebuild run results from stored artifacts -- no tokens, no agents.

    Every run directory keeps its transcript, its end-state dump and the
    scenario it came from, which is enough to re-grade and re-classify it.
    That is how a taxonomy rule change gets validated against real runs
    instead of against synthetic ones.

    **This is the recovery path, so it must not be the thing that breaks.**
    It is reached precisely when something else already went wrong -- a
    half-written ``run.json``, a scenario directory that moved, a taxonomy
    rule mid-edit -- and the point of it is that a finished, paid-for batch
    survives that. Every step that touches a single run's artifacts is
    therefore guarded on its own: one unrebuildable run costs a warning, an
    unclassifiable one keeps its grade and loses only its labels, and neither
    takes the other fifteen with it.
    """
    from llmux.runner.interference import InterferenceReport
    from llmux.runner.run import RunResult, detect_contamination
    from llmux.runner.scenarios import GradeResult, load_scenario
    from llmux.runner.scenarios import grade_backend
    from llmux.runner.taxonomy import ScenarioFacts, classify
    from llmux.runner.transcript import Transcript, parse_transcript_file
    from mockdocs.state import StateFormatError, read_state

    runs_dir = Path(runs_dir)
    results: list[Any] = []
    skipped: list[str] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        record_path = run_dir / "run.json"
        if not record_path.is_file():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped.append(
                f"{record_path}: the run record is unreadable "
                f"({type(exc).__name__}: {exc}); a batch killed mid-write "
                "leaves one of these"
            )
            continue
        if not isinstance(record, dict):
            skipped.append(f"{record_path}: the run record is not an object")
            continue
        scenario_path = record.get("scenario_path")
        if not scenario_path:
            skipped.append(
                f"{record_path}: no scenario_path (predates its recording, or "
                "the record was written by the crash fallback)"
            )
            continue
        try:
            scenario = load_scenario(Path(scenario_path))
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            skipped.append(
                f"{record_path}: cannot load the scenario it names "
                f"({scenario_path}): {type(exc).__name__}: {exc}"
            )
            continue

        analysis_error: Optional[str] = record.get("analysis_error")
        try:
            transcript = parse_transcript_file(run_dir / "transcript.jsonl")
        except (OSError, ValueError) as exc:  # noqa: PERF203 - per-run guard
            transcript = Transcript()
            analysis_error = _join_notes(
                analysis_error,
                f"the transcript could not be read ({type(exc).__name__}: "
                f"{exc}); turns, cost and tool calls are missing from this row",
            )

        report = None
        try:
            backend = read_state(run_dir / "state.json")
            grade = grade_backend(scenario, backend)
            report = InterferenceReport.from_backend(backend)
        except StateFormatError as exc:
            grade = GradeResult.crashed(f"no gradeable end state: {exc}")
        except Exception as exc:  # noqa: BLE001 - a grader may raise anything
            grade = GradeResult.crashed(
                f"the grader raised: {type(exc).__name__}: {exc}"
            )

        try:
            findings = classify(
                ScenarioFacts.from_scenario(scenario),
                transcript,
                passed=grade.passed,
                failures=grade.failures,
                timed_out=bool(record.get("timed_out")),
                interference=report,
            )
        except Exception as exc:  # noqa: BLE001 - the grade still stands
            findings = []
            analysis_error = _join_notes(
                analysis_error,
                f"classification failed ({type(exc).__name__}: {exc}); the "
                "grade and the transcript are unaffected, only the mistake "
                "labels are missing",
            )

        try:
            results.append(
                RunResult(
                    scenario_id=scenario.id,
                    model=str(record.get("model") or "unknown"),
                    difficulty=scenario.difficulty,
                    grade=grade,
                    transcript=transcript,
                    findings=findings,
                    wall_s=float(record.get("wall_s") or 0.0),
                    returncode=record.get("returncode"),
                    timed_out=bool(record.get("timed_out")),
                    artifacts=run_dir,
                    harness_error=record.get("harness_error"),
                    analysis_error=analysis_error,
                    scenario_path=scenario.path,
                    interference=report.as_dict() if report is not None else None,
                    contamination=detect_contamination(transcript),
                )
            )
        except Exception as exc:  # noqa: BLE001 - one run, not the batch
            skipped.append(
                f"{record_path}: could not rebuild the run "
                f"({type(exc).__name__}: {exc})"
            )
    for problem in skipped:
        print(f"skipped {problem}")
    return results


def _join_notes(existing: Optional[str], note: str) -> str:
    return f"{existing}; {note}" if existing else note


def write_report(
    results: Sequence[Any],
    reports_dir: Path,
    *,
    stamp: str,
    corpus: Optional[str] = None,
) -> dict[str, Path]:
    """Write ``<stamp>.md`` and ``<stamp>.json``; return the paths written.

    Ordered so that the expensive thing survives the cheap thing going wrong.
    The per-run records are serialized and written FIRST, because they are
    what the batch paid for; the markdown -- which reads a taxonomy whose
    rules change weekly -- is rendered last and its failure costs a file, not
    a batch. Every step is independently guarded, and a step that fails
    leaves its reason in the JSON rather than raising.
    """
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    problems: list[str] = []

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
            problems.append(
                f"{getattr(result, 'scenario_id', '?')}: could not serialize "
                f"the run record ({type(exc).__name__}: {exc})"
            )

    try:
        agg = aggregate(results)
        summary = agg.as_dict()
    except Exception as exc:  # noqa: BLE001 - the runs still get written
        agg = None
        summary = {"aggregate_error": f"{type(exc).__name__}: {exc}"}
        problems.append(f"aggregation: {type(exc).__name__}: {exc}")

    json_path = reports_dir / f"{stamp}.json"
    json_path.write_text(
        json.dumps(
            {
                "stamp": stamp,
                "corpus": corpus,
                "summary": summary,
                "report_problems": problems,
                "runs": records,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    written["json"] = json_path

    if agg is not None:
        markdown_path = reports_dir / f"{stamp}.md"
        try:
            text = render_markdown(agg, stamp=stamp, corpus=corpus)
        except Exception as exc:  # noqa: BLE001 - the JSON is already safe
            text = (
                f"# LLM UX run report -- {stamp}\n\n"
                f"Rendering this report failed: {type(exc).__name__}: {exc}\n\n"
                f"The runs are intact in `{json_path.name}` and in each run's "
                "own `run.json`. Rebuild with `uv run python -m "
                "llmux.runner.analyze <reports>/<stamp>/runs`.\n"
            )
        markdown_path.write_text(text, encoding="utf-8")
        written["markdown"] = markdown_path
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Rebuild a report from a finished batch's run artifacts.

    uv run python -m llmux.runner.analyze llmux/runner/reports/<stamp>/runs
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="llmux.runner.analyze",
        description="Re-grade and re-classify a finished batch from its artifacts.",
    )
    parser.add_argument("runs_dir", type=Path, help="<reports>/<stamp>/runs")
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="where to write the report (default: the batch's own directory)",
    )
    parser.add_argument(
        "--stamp",
        default=None,
        help="report name (default: the batch stamp, suffixed -reanalyzed)",
    )
    args = parser.parse_args(argv)

    results = reanalyze(args.runs_dir)
    if not results:
        print(f"no run artifacts under {args.runs_dir}")
        return 2
    batch_dir = Path(args.runs_dir).parent
    reports_dir = args.reports_dir or batch_dir.parent
    stamp = args.stamp or f"{batch_dir.name}-reanalyzed"
    paths = write_report(results, reports_dir, stamp=stamp)
    # The same definition run.py prints: outcome, not grade.passed.
    passed = sum(1 for r in results if r.outcome == OUTCOME_PASS)
    inconclusive = sum(1 for r in results if r.outcome == OUTCOME_INCONCLUSIVE)
    conclusive = len(results) - inconclusive
    print(f"{passed}/{conclusive} conclusive runs passed")
    if inconclusive:
        print(
            f"{inconclusive} run(s) INCONCLUSIVE, excluded from that rate "
            "(not a capability result)"
        )
    # write_report omits "markdown" when aggregation failed (its whole point
    # is that the expensive artifact survives the cheap one going wrong), so
    # subscripting it here made the recovery path itself crash -- taking the
    # JSON path down with it, on exactly the run that needed reporting most.
    markdown = paths.get("markdown")
    print(
        f"report: {markdown}"
        if markdown
        else (
            "report: NOT WRITTEN -- aggregation failed, so there was nothing to "
            "render. The per-run records are intact; see report_problems in the "
            "JSON below."
        )
    )
    print(f"json:   {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
