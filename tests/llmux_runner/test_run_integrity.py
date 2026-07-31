"""The three ways batch 20260730-224247 lied, and the guards against them.

That batch reported a degradation curve for the tool surface. Reading its
artifacts afterwards showed three separate reasons the numbers were not
what they claimed:

1. five built-in tools the deny list did not know about were advertised to
   every agent, and two runs spent turns calling one of them;
2. ``rate_limit_event`` records were present and unparsed, so nobody could
   tell a throttled run from an incapable one;
3. the batch's report died at write time in an unrelated module, taking a
   finished, paid-for batch's results with it.

These tests are the regression barrier for each.
"""

from __future__ import annotations

import json

import pytest

from llmux.runner import session
from llmux.runner.analyze import aggregate, render_markdown, write_report
from llmux.runner.run import (
    OUTCOME_FAIL,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_PASS,
    RunResult,
    detect_contamination,
)
from llmux.runner.scenarios import GradeResult
from llmux.runner.transcript import Transcript, parse_stream_json


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _init(tools: list[str]) -> str:
    return _line(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "s1",
            "tools": tools,
            "mcp_servers": [{"name": "gdocsmock", "status": "connected"}],
        }
    )


def _rate_limit(status: str, **extra) -> str:
    return _line(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "rateLimitType": "five_hour",
                "resetsAt": 1785445200,
                **extra,
            },
        }
    )


def _tool_use(tool_id: str, name: str) -> str:
    return _line(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": {}}
                ],
            },
        }
    )


def _tool_result(tool_id: str, text: str, is_error: bool = False) -> str:
    return _line(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": text,
                        "is_error": is_error,
                    }
                ],
            },
        }
    )


RESULT = _line(
    {
        "type": "result",
        "subtype": "success",
        "num_turns": 4,
        "total_cost_usd": 0.2,
        "duration_ms": 1000,
        "result": "done",
    }
)


def _run(transcript: Transcript, **kwargs) -> RunResult:
    kwargs.setdefault("grade", GradeResult(passed=True, score=1.0, failures=()))
    return RunResult(
        scenario_id=kwargs.pop("scenario_id", "sc-1"),
        model=kwargs.pop("model", "sonnet"),
        difficulty=kwargs.pop("difficulty", "easy"),
        transcript=transcript,
        contamination=detect_contamination(transcript),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Built-in tool leakage
# ---------------------------------------------------------------------------


#: Verbatim from `system`/`init` of
#: reports/20260730-224247/runs/stress-120-research-full__sonnet. None of
#: these was in BUILTIN_TOOLS_DENIED at the time.
LEAKED_IN_THE_CONTAMINATED_BATCH = (
    "AskUserQuestion",
    "EnterPlanMode",
    "Monitor",
    "PushNotification",
    "RemoteTrigger",
)


class TestToolLeakage:
    @pytest.mark.parametrize("name", LEAKED_IN_THE_CONTAMINATED_BATCH)
    def test_the_observed_leaks_are_now_denied(self, name):
        assert name in session.BUILTIN_TOOLS_DENIED

    def test_a_clean_init_reports_no_contamination(self):
        transcript = parse_stream_json(
            [
                _init(
                    [
                        "mcp__gdocsmock__list_document_suggestions",
                        "mcp__gdocsmock__manage_document_suggestion",
                    ]
                ),
                RESULT,
            ]
        )
        assert transcript.leaked_builtin_tools("gdocsmock") == []
        assert detect_contamination(transcript) == []

    def test_advertised_builtins_are_named_and_actionable(self):
        transcript = parse_stream_json(
            [
                _init(
                    ["mcp__gdocsmock__list_document_suggestions", "Monitor", "Bash"]
                ),
                RESULT,
            ]
        )
        assert transcript.leaked_builtin_tools("gdocsmock") == ["Bash", "Monitor"]
        (note,) = detect_contamination(transcript)
        assert "Bash, Monitor" in note
        assert "BUILTIN_TOOLS_DENIED" in note

    def test_the_client_internal_waiter_is_not_a_leak(self):
        """WaitForMcpServers is the CLI connecting our own server; counting
        it would put a permanent false positive in every report."""
        transcript = parse_stream_json(
            [
                _init(
                    ["mcp__gdocsmock__list_document_suggestions", "WaitForMcpServers"]
                ),
                RESULT,
            ]
        )
        assert transcript.leaked_builtin_tools("gdocsmock") == []

    def test_a_denied_builtin_call_is_reported_but_not_inconclusive(self):
        """The permission gate did its job: the call failed, the agent
        recovered. Worth reporting, not worth voiding the measurement."""
        transcript = parse_stream_json(
            [
                _init(["mcp__gdocsmock__list_document_suggestions", "Monitor"]),
                _tool_use("t1", "Monitor"),
                _tool_result("t1", "Permission to use Monitor has been denied", True),
                RESULT,
            ]
        )
        result = _run(transcript)
        assert transcript.builtin_tools_called() == ["Monitor"]
        assert any("called non-MCP" in note for note in result.contamination)
        assert result.outcome == OUTCOME_PASS

    def test_a_successful_builtin_call_voids_the_measurement(self):
        transcript = parse_stream_json(
            [
                _init(["mcp__gdocsmock__list_document_suggestions", "Bash"]),
                _tool_use("t1", "Bash"),
                _tool_result("t1", "total 4\n"),
                RESULT,
            ]
        )
        result = _run(transcript)
        assert result.outcome == OUTCOME_INCONCLUSIVE
        assert "outside the measured surface" in " ".join(result.inconclusive_reasons)


# ---------------------------------------------------------------------------
# 2. Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimits:
    def test_events_are_parsed(self):
        transcript = parse_stream_json(
            [_init([]), _rate_limit("allowed"), _rate_limit("allowed"), RESULT]
        )
        assert len(transcript.rate_limit_events) == 2
        assert transcript.rate_limit_events[0]["rateLimitType"] == "five_hour"

    def test_allowed_events_are_not_a_rate_limit(self):
        """The contaminated batch had two of these and they meant nothing:
        the CLI emits them as routine bookkeeping. Treating their presence
        as a cause is exactly the misreading this guards against."""
        transcript = parse_stream_json(
            [_init([]), _rate_limit("allowed"), _rate_limit("allowed"), RESULT]
        )
        assert transcript.throttling_events == []
        assert transcript.rate_limited is False
        assert _run(transcript).outcome == OUTCOME_PASS

    @pytest.mark.parametrize("status", ["rejected", "allowed_warning", "throttled"])
    def test_any_non_allowed_status_marks_the_run_inconclusive(self, status):
        transcript = parse_stream_json([_init([]), _rate_limit(status), RESULT])
        assert transcript.rate_limited is True
        result = _run(transcript)
        assert result.outcome == OUTCOME_INCONCLUSIVE
        assert "rate limited" in result.inconclusive_reasons[0]
        assert "resets_at" in result.inconclusive_reasons[0]

    def test_a_rate_limit_terminal_reason_also_counts(self):
        transcript = parse_stream_json(
            [
                _init([]),
                _line(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "terminal_reason": "rate_limit",
                        "num_turns": 1,
                    }
                ),
            ]
        )
        assert transcript.rate_limited is True

    def test_an_inconclusive_run_keeps_its_pass_flag(self):
        """Pass/fail arithmetic is deliberately unchanged so batches stay
        comparable; INCONCLUSIVE is an extra label, not a third grade."""
        transcript = parse_stream_json([_init([]), _rate_limit("rejected"), RESULT])
        result = _run(transcript, grade=GradeResult(passed=True, score=1.0))
        assert result.passed is True
        assert result.outcome == OUTCOME_INCONCLUSIVE
        assert aggregate([result]).total_passes == 1

    def test_a_timeout_is_inconclusive_not_a_capability_result(self):
        transcript = parse_stream_json([_init([]), RESULT])
        result = _run(
            transcript,
            grade=GradeResult(passed=False, score=0.35),
            timed_out=True,
            harness_error="run exceeded the 1500s wall clock",
        )
        assert result.outcome == OUTCOME_INCONCLUSIVE
        assert "wall clock" in " ".join(result.inconclusive_reasons)

    def test_a_clean_failure_is_still_a_failure(self):
        transcript = parse_stream_json([_init([]), _rate_limit("allowed"), RESULT])
        result = _run(transcript, grade=GradeResult(passed=False, score=0.4))
        assert result.outcome == OUTCOME_FAIL
        assert result.inconclusive_reasons == []


# ---------------------------------------------------------------------------
# 3. Report resilience
# ---------------------------------------------------------------------------


class Exploding:
    """A run record that blows up wherever the report touches it."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.scenario_id = "sc-boom"
        self.model = "sonnet"
        self.difficulty = "hard"
        self.grade = GradeResult(passed=False, score=0.0)
        self.transcript = Transcript()
        self.findings = []
        self.wall_s = 1.0
        self.harness_error = None
        self.interference = None
        self.contamination = []
        self.passed = False
        self.outcome = OUTCOME_FAIL
        self.inconclusive_reasons = []

    def as_dict(self):
        if self.where == "as_dict":
            raise ImportError("cannot import name 'POSITIVE_CLASSES'")
        return {"scenario_id": self.scenario_id, "model": self.model}

    def __getattribute__(self, name):
        if name == "findings" and object.__getattribute__(self, "where") == "fold":
            raise ImportError("cannot import name 'POSITIVE_CLASSES'")
        return object.__getattribute__(self, name)


def good_run() -> RunResult:
    return _run(
        parse_stream_json([_init(["mcp__gdocsmock__x"]), RESULT]),
        scenario_id="sc-good",
        grade=GradeResult(passed=True, score=1.0),
    )


class TestReportResilience:
    def test_one_unfoldable_run_costs_a_row_not_the_report(self):
        agg = aggregate([good_run(), Exploding("fold")])
        assert agg.total_runs == 2
        assert "sc-good" in agg.by_scenario
        assert agg.aggregation_errors
        assert "POSITIVE_CLASSES" in agg.aggregation_errors[0]

    def test_one_unserialisable_run_costs_a_record_not_the_batch(self, tmp_path):
        paths = write_report(
            [good_run(), Exploding("as_dict")], tmp_path, stamp="20260731-000000"
        )
        payload = json.loads(paths["json"].read_text())
        assert len(payload["runs"]) == 2
        assert payload["runs"][0]["scenario_id"] == "sc-good"
        assert "POSITIVE_CLASSES" in payload["runs"][1]["record_error"]
        assert payload["report_problems"]

    def test_a_rendering_crash_still_leaves_both_files(self, tmp_path, monkeypatch):
        import llmux.runner.analyze as analyze

        def boom(*_args, **_kwargs):
            raise ImportError("cannot import name 'POSITIVE_CLASSES'")

        monkeypatch.setattr(analyze, "render_markdown", boom)
        paths = write_report([good_run()], tmp_path, stamp="20260731-000001")

        assert json.loads(paths["json"].read_text())["runs"][0]["scenario_id"] == (
            "sc-good"
        )
        text = paths["markdown"].read_text()
        assert "Rendering this report failed" in text
        assert "llmux.runner.analyze" in text

    def test_an_aggregation_crash_still_writes_every_run(self, tmp_path, monkeypatch):
        import llmux.runner.analyze as analyze

        def boom(*_args, **_kwargs):
            raise ImportError("cannot import name 'POSITIVE_CLASSES'")

        monkeypatch.setattr(analyze, "aggregate", boom)
        paths = write_report([good_run()], tmp_path, stamp="20260731-000002")

        payload = json.loads(paths["json"].read_text())
        assert payload["runs"][0]["scenario_id"] == "sc-good"
        assert "POSITIVE_CLASSES" in payload["summary"]["aggregate_error"]
        assert "markdown" not in paths


class TestReportContent:
    def test_inconclusive_runs_lead_the_report(self):
        throttled = _run(
            parse_stream_json([_init([]), _rate_limit("rejected"), RESULT]),
            scenario_id="sc-throttled",
            grade=GradeResult(passed=False, score=0.35),
        )
        text = render_markdown(
            aggregate([good_run(), throttled]), stamp="20260731-000003"
        )
        assert "## Inconclusive runs" in text
        assert "sc-throttled" in text
        assert text.index("## Inconclusive runs") < text.index(
            "## Pass rate by model"
        )

    def test_contamination_gets_its_own_section(self):
        leaky = _run(
            parse_stream_json(
                [_init(["mcp__gdocsmock__x", "Monitor"]), RESULT]
            ),
            scenario_id="sc-leaky",
        )
        text = render_markdown(aggregate([leaky]), stamp="20260731-000004")
        assert "## Tool-surface contamination" in text
        assert "Monitor" in text
        assert "toolprobe" in text
