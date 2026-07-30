"""Aggregation and report rendering, from synthetic runs."""

from __future__ import annotations

import json

from llmux.runner.analyze import aggregate, render_markdown, write_report
from llmux.runner.run import RunResult, estimate_cost
from llmux.runner.scenarios import GradeResult
from llmux.runner.taxonomy import Finding
from llmux.runner.transcript import Transcript

from tests.llmux_runner.test_taxonomy import make_call, make_transcript


def run_result(
    scenario_id: str,
    model: str,
    *,
    difficulty: str = "easy",
    passed: bool = True,
    score: float = 1.0,
    failures: tuple[str, ...] = (),
    findings: list[Finding] | None = None,
    transcript: Transcript | None = None,
    harness_error: str | None = None,
) -> RunResult:
    return RunResult(
        scenario_id=scenario_id,
        model=model,
        difficulty=difficulty,
        grade=GradeResult(passed=passed, score=score, failures=failures),
        transcript=transcript
        or make_transcript(
            [make_call(0, "get_doc_review_view"), make_call(1, "suggest_doc_edit")],
            num_turns=3,
        ),
        findings=findings or [],
        wall_s=12.0,
        harness_error=harness_error,
    )


def test_aggregate_splits_by_model_and_difficulty():
    results = [
        run_result("s1", "sonnet", difficulty="easy", passed=True, score=1.0),
        run_result("s2", "sonnet", difficulty="hard", passed=False, score=0.5),
        run_result("s1", "opus", difficulty="easy", passed=True, score=1.0),
        run_result("s2", "opus", difficulty="hard", passed=True, score=1.0),
    ]
    agg = aggregate(results)
    assert agg.total_runs == 4
    assert agg.total_passes == 3
    assert agg.pass_rate == 0.75
    assert agg.by_model["sonnet"].pass_rate == 0.5
    assert agg.by_model["opus"].pass_rate == 1.0
    assert agg.by_difficulty["easy"].pass_rate == 1.0
    assert agg.by_model_difficulty["sonnet"]["hard"].pass_rate == 0.0
    assert agg.by_model_difficulty["sonnet"]["hard"].mean_score == 0.5


def test_aggregate_records_per_scenario_detail_and_tool_usage():
    transcript = make_transcript(
        [
            make_call(0, "get_doc_review_view"),
            make_call(1, "suggest_doc_edit", error="start_index must be >= 1"),
            make_call(2, "suggest_doc_edit"),
        ]
    )
    agg = aggregate(
        [run_result("s1", "sonnet", passed=False, score=0.4, transcript=transcript)]
    )
    entry = agg.by_scenario["s1"]["runs"]["sonnet"]
    assert entry["tool_calls"] == 3
    assert entry["failed_tool_calls"] == 1
    assert entry["tool_sequence"] == [
        "get_doc_review_view",
        "suggest_doc_edit",
        "suggest_doc_edit",
    ]
    assert agg.tool_usage["suggest_doc_edit"] == {"calls": 2, "errors": 1}
    assert agg.tool_usage["get_doc_review_view"]["errors"] == 0


def test_aggregate_builds_the_taxonomy_across_runs():
    findings = [Finding("index_error", "start_index must be >= 1")]
    agg = aggregate(
        [
            run_result("s1", "sonnet", passed=False, findings=findings),
            run_result("s2", "sonnet", passed=False, findings=findings),
            run_result("s3", "sonnet", passed=True),
        ]
    )
    assert agg.taxonomy["index_error"]["runs"] == 2
    assert agg.taxonomy["index_error"]["systemic"] is True


def test_harness_errors_are_kept_separate_from_grades():
    agg = aggregate(
        [run_result("s1", "sonnet", passed=False, harness_error="server never started")]
    )
    assert agg.harness_errors == ["s1 [sonnet]: server never started"]


def test_markdown_report_contains_the_sections_a_reader_needs():
    agg = aggregate(
        [
            run_result(
                "s1",
                "sonnet",
                passed=False,
                score=0.4,
                failures=("final text is wrong",),
                findings=[Finding("index_error", "suggest_doc_edit rejected on indexes")],
            ),
            run_result("s1", "opus", passed=True),
        ]
    )
    markdown = render_markdown(agg, stamp="20260730-120000", corpus="llmux/scenarios/generated")
    assert "# LLM UX run report -- 20260730-120000" in markdown
    assert "## Mistake taxonomy" in markdown
    assert "`index_error`" in markdown
    assert "## Pass rate by model x difficulty" in markdown
    assert "## Per-scenario scores" in markdown
    assert "## Failures in detail" in markdown
    assert "final text is wrong" in markdown
    assert "get_doc_review_view -> suggest_doc_edit" in markdown
    assert "## Tool usage" in markdown


def test_clean_batch_reports_no_findings():
    markdown = render_markdown(aggregate([run_result("s1", "sonnet")]), stamp="x")
    assert "No findings: every run was clean." in markdown


def test_write_report_emits_markdown_and_json(tmp_path):
    results = [run_result("s1", "sonnet", passed=False, score=0.25)]
    paths = write_report(results, tmp_path, stamp="20260730-235959", corpus="c")
    assert paths["markdown"] == tmp_path / "20260730-235959.md"
    assert paths["json"] == tmp_path / "20260730-235959.json"
    payload = json.loads(paths["json"].read_text())
    assert payload["stamp"] == "20260730-235959"
    assert payload["corpus"] == "c"
    assert payload["summary"]["totals"]["runs"] == 1
    assert payload["runs"][0]["scenario_id"] == "s1"
    assert payload["runs"][0]["transcript"]["tool_calls"][0]["tool"] == (
        "get_doc_review_view"
    )


def test_cost_estimate_scales_with_the_matrix():
    assert estimate_cost(2, ["sonnet"]) == 0.60
    assert estimate_cost(2, ["sonnet", "opus"]) == 0.60 + 2.40
    assert estimate_cost(1, ["some-unknown-model"]) == 0.50
