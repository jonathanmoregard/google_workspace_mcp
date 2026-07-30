"""Aggregate runs into a pass-rate table and a mistake taxonomy.

Two outputs from the same data: ``reports/<timestamp>.md`` for a human
deciding what to change about the tool surface, and ``reports/<timestamp>.json``
for anything that wants to diff two batches (did renaming that tool actually
move the wrong-tool-for-intent rate?).

The report leads with the taxonomy rather than the pass rate on purpose: a
pass rate says how the models did, the taxonomy says what the tools did to
them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from llmux.runner.taxonomy import CLASSES, Finding, repeat_report

DIFFICULTY_ORDER = ("easy", "medium", "hard", "unknown")


@dataclass
class Cell:
    """Pass rate for one bucket of runs."""

    runs: int = 0
    passes: int = 0
    score_total: float = 0.0

    def add(self, passed: bool, score: float) -> None:
        self.runs += 1
        self.passes += int(bool(passed))
        self.score_total += score

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    @property
    def mean_score(self) -> float:
        return self.score_total / self.runs if self.runs else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "passes": self.passes,
            "pass_rate": round(self.pass_rate, 3),
            "mean_score": round(self.mean_score, 3),
        }


@dataclass
class Aggregate:
    """Everything the report renders."""

    total_runs: int = 0
    total_passes: int = 0
    total_cost_usd: float = 0.0
    total_wall_s: float = 0.0
    total_turns: int = 0
    by_model: dict[str, Cell] = field(default_factory=dict)
    by_difficulty: dict[str, Cell] = field(default_factory=dict)
    by_model_difficulty: dict[str, dict[str, Cell]] = field(default_factory=dict)
    by_scenario: dict[str, dict[str, Any]] = field(default_factory=dict)
    taxonomy: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    harness_errors: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.total_passes / self.total_runs if self.total_runs else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "totals": {
                "runs": self.total_runs,
                "passes": self.total_passes,
                "pass_rate": round(self.pass_rate, 3),
                "cost_usd": round(self.total_cost_usd, 4),
                "wall_s": round(self.total_wall_s, 1),
                "turns": self.total_turns,
            },
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
        }


def aggregate(results: Sequence[Any]) -> Aggregate:
    """Fold run results into the report model.

    Accepts anything with the ``RunResult`` shape, so the aggregation can be
    unit-tested with stubs instead of live runs.
    """
    agg = Aggregate()
    per_run_findings: list[Sequence[Finding]] = []

    for result in results:
        agg.total_runs += 1
        agg.total_passes += int(bool(result.passed))
        agg.total_cost_usd += float(result.transcript.cost_usd or 0.0)
        agg.total_wall_s += float(result.wall_s or 0.0)
        agg.total_turns += int(result.transcript.num_turns or 0)

        score = float(result.grade.score)
        agg.by_model.setdefault(result.model, Cell()).add(result.passed, score)
        agg.by_difficulty.setdefault(result.difficulty, Cell()).add(
            result.passed, score
        )
        agg.by_model_difficulty.setdefault(result.model, {}).setdefault(
            result.difficulty, Cell()
        ).add(result.passed, score)

        entry = agg.by_scenario.setdefault(
            result.scenario_id,
            {"difficulty": result.difficulty, "runs": {}},
        )
        agent_calls = result.transcript.agent_tool_calls
        entry["runs"][result.model] = {
            "pass": bool(result.passed),
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

        if result.harness_error:
            agg.harness_errors.append(
                f"{result.scenario_id} [{result.model}]: {result.harness_error}"
            )
        per_run_findings.append(result.findings)

    agg.taxonomy = repeat_report(per_run_findings)
    return agg


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
        f"{agg.total_runs} run(s), {agg.total_passes} passed "
        f"({_pct(agg.pass_rate)}), {agg.total_turns} turns, "
        f"${agg.total_cost_usd:.2f}, {agg.total_wall_s / 60:.1f} min wall."
    )
    lines.append("")

    if agg.harness_errors:
        lines.append("## Harness problems (read these first)")
        lines.append("")
        lines.append(
            "These runs did not measure the tool surface -- something in the "
            "harness went wrong."
        )
        lines.append("")
        for problem in agg.harness_errors:
            lines.append(f"- {problem}")
        lines.append("")

    lines.append("## Mistake taxonomy")
    lines.append("")
    if not agg.taxonomy:
        lines.append("No findings: every run was clean.")
    else:
        lines.append("| class | runs | share | occurrences | repeated in-run | systemic |")
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
        lines.append("")
        for code, entry in agg.taxonomy.items():
            lines.append(f"### `{code}`")
            lines.append("")
            lines.append(CLASSES.get(code, entry.get("description") or ""))
            lines.append("")
            for example in entry.get("examples", []):
                lines.append(f"- {example}")
            lines.append("")

    lines.append("## Pass rate by model x difficulty")
    lines.append("")
    difficulties = _sorted_difficulties(agg.by_difficulty)
    header = "| model | " + " | ".join(difficulties) + " | overall |"
    lines.append(header)
    lines.append("| --- | " + " | ".join("---:" for _ in difficulties) + " | ---: |")
    for model in sorted(agg.by_model):
        cells = agg.by_model_difficulty.get(model, {})
        row = [f"| {model} "]
        for difficulty in difficulties:
            cell = cells.get(difficulty)
            row.append(
                f"| {_pct(cell.pass_rate)} ({cell.passes}/{cell.runs}) "
                if cell
                else "| - "
            )
        overall = agg.by_model[model]
        row.append(f"| {_pct(overall.pass_rate)} ({overall.passes}/{overall.runs}) |")
        lines.append("".join(row))
    lines.append("")

    lines.append("## Per-scenario scores")
    lines.append("")
    lines.append("| scenario | difficulty | model | pass | score | turns | calls (err) | $ |")
    lines.append("| --- | --- | --- | :---: | ---: | ---: | ---: | ---: |")
    for scenario_id in sorted(agg.by_scenario):
        entry = agg.by_scenario[scenario_id]
        for model in sorted(entry["runs"]):
            run = entry["runs"][model]
            lines.append(
                f"| {scenario_id} | {entry['difficulty']} | {model} | "
                f"{'PASS' if run['pass'] else 'FAIL'} | {run['score']:.2f} | "
                f"{run['turns']} | {run['tool_calls']} ({run['failed_tool_calls']}) | "
                f"{run['cost_usd']:.3f} |"
            )
    lines.append("")

    failing = [
        (sid, model, run)
        for sid, entry in sorted(agg.by_scenario.items())
        for model, run in sorted(entry["runs"].items())
        if not run["pass"]
    ]
    if failing:
        lines.append("## Failures in detail")
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
        lines.append(f"| `{tool}` | {usage['calls']} | {usage['errors']} | {_pct(rate)} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def reanalyze(runs_dir: Path) -> list[Any]:
    """Rebuild run results from stored artifacts -- no tokens, no agents.

    Every run directory keeps its transcript, its end-state dump and the
    scenario it came from, which is enough to re-grade and re-classify it.
    That is how a taxonomy rule change gets validated against real runs
    instead of against synthetic ones.
    """
    from llmux.runner.run import RunResult
    from llmux.runner.scenarios import GradeResult, load_scenario
    from llmux.runner.scenarios import grade_backend
    from llmux.runner.taxonomy import ScenarioFacts, classify
    from llmux.runner.transcript import parse_transcript_file
    from mockdocs.state import StateFormatError, read_state

    runs_dir = Path(runs_dir)
    results: list[Any] = []
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        record_path = run_dir / "run.json"
        if not record_path.is_file():
            continue
        record = json.loads(record_path.read_text(encoding="utf-8"))
        scenario_path = record.get("scenario_path")
        if not scenario_path:
            raise ValueError(
                f"{record_path} predates scenario_path recording; rerun the batch"
            )
        scenario = load_scenario(Path(scenario_path))
        transcript = parse_transcript_file(run_dir / "transcript.jsonl")
        try:
            grade = grade_backend(scenario, read_state(run_dir / "state.json"))
        except StateFormatError as exc:
            grade = GradeResult.crashed(f"no gradeable end state: {exc}")
        findings = classify(
            ScenarioFacts.from_scenario(scenario),
            transcript,
            passed=grade.passed,
            failures=grade.failures,
            timed_out=bool(record.get("timed_out")),
        )
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
                scenario_path=scenario.path,
            )
        )
    return results


def write_report(
    results: Sequence[Any],
    reports_dir: Path,
    *,
    stamp: str,
    corpus: Optional[str] = None,
) -> dict[str, Path]:
    """Write ``<stamp>.md`` and ``<stamp>.json``; return both paths."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    agg = aggregate(results)

    markdown_path = reports_dir / f"{stamp}.md"
    json_path = reports_dir / f"{stamp}.json"
    markdown_path.write_text(
        render_markdown(agg, stamp=stamp, corpus=corpus), encoding="utf-8"
    )
    payload = {
        "stamp": stamp,
        "corpus": corpus,
        "summary": agg.as_dict(),
        "runs": [r.as_dict() for r in results],
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {"markdown": markdown_path, "json": json_path}


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
    passed = sum(1 for r in results if r.passed)
    print(f"{passed}/{len(results)} runs passed")
    print(f"report: {paths['markdown']}")
    print(f"json:   {paths['json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
