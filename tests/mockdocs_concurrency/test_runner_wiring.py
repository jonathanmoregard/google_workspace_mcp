"""The runner half: declaration, classification, and batch comparability.

The load-bearing constraint here is that nothing about a single-writer run
changes. ``reports/20260730-211540.md`` has to stay comparable with whatever
comes next, so the concurrency work is additive on every axis: an extra
optional ``meta.json`` key, an extra keyword-only argument to ``classify``
that defaults to off, and new taxonomy classes that never displace old ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmux.runner import interference as interference_mod
from llmux.runner.interference import InterferenceReport
from llmux.runner.scenarios import load_scenario
from llmux.runner.taxonomy import (
    CLASSES,
    CONCURRENCY_CLASSES,
    HARNESS_CLASSES,
    POSITIVE_CLASSES,
    ScenarioFacts,
    classify,
)
from llmux.runner.transcript import parse_stream_json
from mockdocs.concurrency import ConcurrencyRecord, InterferenceEngine, parse_script
from mockdocs.fake_services import FakeBackend

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "llmux" / "interference"
FIXTURES = REPO_ROOT / "llmux" / "runner" / "_fixtures"


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_a_scenario_without_interferences_is_untouched(tmp_path):
    """The whole existing corpus must keep behaving as if none of this exists."""
    scenario = load_scenario(FIXTURES / "fx-accept-reject")
    assert interference_mod.declared_interferences(scenario) == []
    assert interference_mod.materialise(scenario, tmp_path) == {}
    assert not (tmp_path / "interference.json").exists()


def test_materialise_writes_a_script_the_server_can_load(tmp_path):
    scenario = load_scenario(CORPUS / "ix-vanished-id")
    env = interference_mod.materialise(scenario, tmp_path)

    assert set(env) == {interference_mod.ENV_VAR}
    path = Path(env[interference_mod.ENV_VAR])
    assert path.is_absolute() and path.is_file()

    reloaded = parse_script(json.loads(path.read_text(encoding="utf-8")))
    assert [i.name for i in reloaded] == ["bob-accepts-own-fix"]
    assert reloaded[0].trigger.tools == (
        "list_document_suggestions",
        "get_doc_review_view",
    )


def test_a_malformed_script_is_rejected_before_any_tokens_are_spent():
    with pytest.raises(interference_mod.InterferenceError):
        parse_script([{"kind": "teleport", "name": "nope"}])
    with pytest.raises(interference_mod.InterferenceError):
        parse_script(
            [
                {"kind": "shift_indexes", "name": "dup"},
                {"kind": "shift_indexes", "name": "dup"},
            ]
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _report(fired, calls, errors=()) -> InterferenceReport:
    return InterferenceReport(
        record=ConcurrencyRecord.from_dict(
            {
                "declared": [f["name"] for f in fired],
                "fired": fired,
                "agent_calls": calls,
                "errors": list(errors),
            }
        )
    )


def _call(ordinal, tool, *, ok=True, **args):
    return {
        "ordinal": ordinal,
        "tool": tool,
        "tool_ordinal": 1,
        "args": args,
        "ok": ok,
        "error": None,
    }


EMPTY_TRANSCRIPT = parse_stream_json([])
FACTS = ScenarioFacts(id="ix", difficulty="hard", tags=("concurrency",))


def codes(report):
    return {
        f.code
        for f in classify(FACTS, EMPTY_TRANSCRIPT, passed=False, interference=report)
    }


def test_classify_is_unchanged_when_no_interference_is_passed():
    """The default path is the one every previous batch was scored on."""
    before = classify(FACTS, EMPTY_TRANSCRIPT, passed=False)
    after = classify(FACTS, EMPTY_TRANSCRIPT, passed=False, interference=None)
    assert [f.as_dict() for f in before] == [f.as_dict() for f in after]
    assert not (CONCURRENCY_CLASSES & {f.code for f in before})


def test_acting_on_a_vanished_id_is_classified():
    report = _report(
        [
            {
                "name": "bob-accepts",
                "kind": "resolve_under_agent",
                "when": "after",
                "editor": "bob",
                "at_call": 1,
                "at_tool": "list_document_suggestions",
                "document_id": "d",
                "effect": {
                    "suggestion_id": "sug.bob.2",
                    "action": "accept",
                    "existed": True,
                    "also_removed": [],
                },
            }
        ],
        [
            _call(1, "list_document_suggestions"),
            _call(2, "manage_document_suggestion", ok=False, suggestion_id="sug.bob.2"),
            _call(3, "manage_document_suggestion", ok=False, suggestion_id="sug.bob.2"),
        ],
    )
    found = codes(report)
    assert "acted_on_vanished_id" in found
    assert "ignored_concurrent_change" in found  # it never looked again
    assert "recovered_after_conflict" not in found
    assert report.blind_retries()


def test_a_stale_index_write_is_classified_even_though_it_succeeded():
    report = _report(
        [
            {
                "name": "dana-banner",
                "kind": "shift_indexes",
                "when": "after",
                "editor": "dana",
                "at_call": 1,
                "at_tool": "get_doc_content",
                "document_id": "d",
                "effect": {"mode": "insert", "index": 0, "utf16_shift": 27},
            }
        ],
        [
            _call(1, "get_doc_content"),
            _call(2, "suggest_doc_edit", start_index=41, end_index=50),
        ],
    )
    assert "wrote_with_stale_indexes" in codes(report)


def test_re_reading_between_the_change_and_the_write_clears_it():
    report = _report(
        [
            {
                "name": "dana-banner",
                "kind": "shift_indexes",
                "when": "after",
                "editor": "dana",
                "at_call": 1,
                "at_tool": "get_doc_content",
                "document_id": "d",
                "effect": {"mode": "insert", "index": 0, "utf16_shift": 27},
            }
        ],
        [
            _call(1, "get_doc_content"),
            _call(2, "list_document_suggestions"),
            _call(3, "suggest_doc_edit", start_index=68, end_index=77),
        ],
    )
    found = codes(report)
    assert "wrote_with_stale_indexes" not in found
    assert "recovered_after_conflict" in found


def test_a_broken_interleaving_is_a_harness_fault_not_an_agent_mistake():
    report = _report([], [], errors=["shift_indexes blew up"])
    report.record.declared.append("never-fired")
    findings = classify(FACTS, EMPTY_TRANSCRIPT, passed=False, interference=report)
    faults = [f for f in findings if f.code == "harness_interference_fault"]
    assert len(faults) == 2  # the engine error, and the interference that never fired
    assert all(f.source == "interference" for f in faults)
    # Nothing about the agent is asserted from a broken interleaving.
    assert not (CONCURRENCY_CLASSES - HARNESS_CLASSES) & {f.code for f in findings}


# ---------------------------------------------------------------------------
# Taxonomy hygiene
# ---------------------------------------------------------------------------


def test_the_new_classes_are_additive_and_documented():
    for code in CONCURRENCY_CLASSES:
        assert code in CLASSES and CLASSES[code].strip()
    assert POSITIVE_CLASSES < CONCURRENCY_CLASSES
    assert "harness_interference_fault" in HARNESS_CLASSES
    # The pre-existing classes a previous batch was scored on are all still
    # present and still mean what they meant.
    for code in (
        "wrong_tool_for_intent",
        "param_shape_error",
        "index_error",
        "stale_state",
        "gave_up_early",
        "hallucinated_tool",
        "ignored_error",
        "accepted_when_should_reject",
        "no_end_state_verification",
        "harness_mcp_unavailable",
    ):
        assert code in CLASSES


def test_the_report_renders_the_concurrent_editor_section():
    from llmux.runner.analyze import Aggregate, render_markdown

    agg = Aggregate()
    agg.interference = [
        {
            "scenario_id": "ix-vanished-id",
            "model": "sonnet",
            "pass": False,
            "fired": [{"name": "bob-accepts-own-fix", "at_call": 2}],
            "reread_after_change": False,
            "blind_retries": 2,
            "stale_index_writes": 0,
            "violations": [],
        }
    ]
    markdown = render_markdown(agg, stamp="test")
    assert "Concurrent-editor runs" in markdown
    assert "bob-accepts-own-fix@2" in markdown


# ---------------------------------------------------------------------------
# End to end through the state snapshot
# ---------------------------------------------------------------------------


def test_the_interleaving_reaches_the_grader_through_the_snapshot(tmp_path):
    """The grader is out of process, so this path is the whole contract."""
    from mockdocs.state import read_state, write_state

    scenario = load_scenario(CORPUS / "ix-vanished-id")
    backend = FakeBackend(me=scenario.me)
    backend.seed(scenario.seed)
    engine = InterferenceEngine(
        backend, interference_mod.declared_interferences(scenario)
    )
    engine.around(
        "list_document_suggestions", {"document_id": "ix-doc-launch"}, lambda: None
    )

    path = tmp_path / "state.json"
    write_state(backend, path)
    report = InterferenceReport.from_backend(read_state(path))

    assert report is not None
    assert report.fired_names == ["bob-accepts-own-fix"]
    assert report.vanished_ids == {"sug.bob.2": 1}
    assert report.violations == []
    assert report.ineffective == []
