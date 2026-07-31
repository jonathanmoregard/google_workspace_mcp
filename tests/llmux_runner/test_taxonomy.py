"""Mistake classification, fed synthetic transcripts.

Each test builds the transcript that a specific failure mode produces and
asserts the class it lands in -- the taxonomy is only useful if a run's
classes are reproducible from its tool calls.
"""

from __future__ import annotations

import json

from llmux.runner.taxonomy import (
    CLASSES,
    Finding,
    ScenarioFacts,
    classify,
    repeat_report,
)
from llmux.runner.transcript import ToolCall, Transcript


def make_call(
    index: int,
    tool: str,
    args: dict | None = None,
    *,
    error: str | None = None,
) -> ToolCall:
    return ToolCall(
        index=index,
        name=f"mcp__gdocsmock__{tool}",
        server="gdocsmock",
        tool=tool,
        args=args or {},
        tool_use_id=f"t{index}",
        is_error=error is not None,
        result_text=error or "{}",
        answered=True,
    )


def make_transcript(calls: list[ToolCall], **kwargs) -> Transcript:
    transcript = Transcript(
        tool_calls=calls,
        mcp_servers=[{"name": "gdocsmock", "status": "connected"}],
        num_turns=kwargs.pop("num_turns", len(calls) + 1),
        subtype=kwargs.pop("subtype", "success"),
        result_text=kwargs.pop("result_text", "Done."),
    )
    for key, value in kwargs.items():
        setattr(transcript, key, value)
    return transcript


SUGGESTION_FACTS = ScenarioFacts(
    id="s1",
    difficulty="easy",
    tags=("suggestions",),
    expected={"accept": ["sug.bob.1"], "reject": ["sug.carol.1"]},
)


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def test_every_class_is_documented():
    """A class with no description is unreadable in the report."""
    assert set(CLASSES) >= {
        "wrong_tool_for_intent",
        "param_shape_error",
        "index_error",
        "stale_state",
        "gave_up_early",
        "hallucinated_tool",
        "ignored_error",
        "accepted_when_should_reject",
        "no_end_state_verification",
    }
    assert all(description.strip() for description in CLASSES.values())


def test_index_error_from_a_rejected_offset():
    transcript = make_transcript(
        [
            make_call(0, "get_doc_review_view"),
            make_call(
                1,
                "suggest_doc_edit",
                {"start_index": 0, "text": "x"},
                error="start_index must be >= 1. Take indexes verbatim from ...",
            ),
            make_call(2, "suggest_doc_edit", {"start_index": 23, "text": "x"}),
            make_call(3, "get_doc_review_view"),
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=True)
    assert "index_error" in codes(findings)
    assert "no_end_state_verification" not in codes(findings)


def test_param_shape_error_from_a_mutually_exclusive_argument_pair():
    transcript = make_transcript(
        [
            make_call(
                0,
                "reply_to_doc_thread",
                {"comment_id": "c1", "suggestion_id": "s1"},
                error="Provide exactly one of comment_id or suggestion_id.",
            )
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "param_shape_error" in codes(findings)


def test_stale_state_only_after_a_successful_mutation():
    after_write = make_transcript(
        [
            make_call(0, "suggest_doc_edit", {"start_index": 5, "text": "hi"}),
            make_call(
                1,
                "manage_document_suggestion",
                {"action": "accept", "suggestion_id": "sug.bob.1"},
                error="Invalid requests[0]: the suggestion ID sug.bob.1 is invalid.",
            ),
        ]
    )
    assert "stale_state" in codes(classify(SUGGESTION_FACTS, after_write, passed=False))

    without_write = make_transcript(
        [
            make_call(
                0,
                "manage_document_suggestion",
                {"action": "accept", "suggestion_id": "made.up"},
                error="Invalid requests[0]: the suggestion ID made.up is invalid.",
            )
        ]
    )
    findings = classify(SUGGESTION_FACTS, without_write, passed=False)
    assert "stale_state" not in codes(findings)
    assert "param_shape_error" in codes(findings)


def test_hallucinated_tool_from_an_unknown_name_in_the_session():
    transcript = make_transcript([make_call(0, "accept_all_suggestions")])
    transcript.available_tools = ["mcp__gdocsmock__manage_document_suggestion"]
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "hallucinated_tool" in codes(findings)


def test_an_init_list_without_mcp_tools_never_implies_hallucination():
    """Regression: ``system/init`` fires while MCP is still connecting.

    A real batch flagged five perfectly good calls as hallucinated because
    init had advertised only ``WaitForMcpServers``.
    """
    transcript = make_transcript(
        [make_call(0, "get_doc_content"), make_call(1, "inspect_doc_structure")]
    )
    transcript.available_tools = ["WaitForMcpServers"]
    assert "hallucinated_tool" not in codes(
        classify(SUGGESTION_FACTS, transcript, passed=True)
    )


def test_client_internal_calls_are_not_classified():
    from llmux.runner.transcript import ToolCall

    wait = ToolCall(
        index=0,
        name="WaitForMcpServers",
        server=None,
        tool="WaitForMcpServers",
        args={},
        tool_use_id="t0",
        is_error=False,
        answered=True,
    )
    transcript = make_transcript([wait, make_call(1, "get_doc_review_view")])
    transcript.available_tools = ["mcp__gdocsmock__get_doc_review_view"]
    assert "hallucinated_tool" not in codes(
        classify(SUGGESTION_FACTS, transcript, passed=True)
    )


def test_hallucinated_param_from_the_error_text():
    transcript = make_transcript(
        [
            make_call(
                0,
                "suggest_doc_edit",
                {"replacement_text": "x"},
                error="got an unexpected keyword argument 'replacement_text'",
            )
        ]
    )
    assert "hallucinated_tool" in codes(
        classify(SUGGESTION_FACTS, transcript, passed=False)
    )


def test_ignored_error_when_a_failure_is_never_retried():
    transcript = make_transcript(
        [
            make_call(
                0, "create_anchored_doc_comment", error="end_index must be greater"
            ),
            make_call(1, "get_doc_review_view"),
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "ignored_error" in codes(findings)

    retried = make_transcript(
        [
            make_call(
                0, "create_anchored_doc_comment", error="end_index must be greater"
            ),
            make_call(
                1, "create_anchored_doc_comment", {"start_index": 1, "end_index": 4}
            ),
            make_call(2, "get_doc_review_view"),
        ]
    )
    assert "ignored_error" not in codes(
        classify(SUGGESTION_FACTS, retried, passed=True)
    )


def test_wrong_tool_for_intent_when_a_suggestion_task_edits_directly():
    transcript = make_transcript(
        [
            make_call(0, "get_doc_content"),
            make_call(
                1, "modify_doc_text", {"document_id": "d", "replacement_text": "color"}
            ),
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "wrong_tool_for_intent" in codes(findings)


def test_wrong_tool_for_intent_when_an_anchored_task_uses_the_drive_comment_tool():
    facts = ScenarioFacts(id="s2", tags=("comments", "anchored"), expected={})
    transcript = make_transcript(
        [make_call(0, "manage_document_comment", {"action": "create", "content": "?"})]
    )
    assert "wrong_tool_for_intent" in codes(classify(facts, transcript, passed=False))


def test_accepted_when_should_reject_from_the_ground_truth():
    transcript = make_transcript(
        [
            make_call(0, "list_document_suggestions"),
            make_call(
                1,
                "manage_document_suggestion",
                {"action": "accept", "suggestion_id": "sug.carol.1"},
            ),
            make_call(2, "list_document_suggestions"),
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "accepted_when_should_reject" in codes(findings)


def test_accepted_when_should_reject_from_a_grader_failure_string():
    transcript = make_transcript([make_call(0, "list_document_suggestions")])
    findings = classify(
        ScenarioFacts(id="s3", tags=("suggestions",)),
        transcript,
        passed=False,
        failures=[
            "the ship date was removed: the deletion suggestion was accepted "
            "when it should have been rejected"
        ],
    )
    assert "accepted_when_should_reject" in codes(findings)


def test_no_end_state_verification_when_writes_are_never_read_back():
    transcript = make_transcript(
        [
            make_call(0, "get_doc_review_view"),
            make_call(1, "suggest_doc_edit", {"start_index": 5, "text": "x"}),
        ]
    )
    assert "no_end_state_verification" in codes(
        classify(SUGGESTION_FACTS, transcript, passed=True)
    )


def test_a_self_verifying_write_is_annotated_but_still_counted():
    """The rule must not be quietly redefined: a write that read the
    document back for itself still fires the class (so batches stay
    comparable) and says so in the detail."""
    self_verifying = make_call(1, "suggest_doc_edit", {"start_index": 5, "text": "x"})
    # The CLI escapes the tool's JSON into its own envelope, which is
    # exactly what the classifier has to cope with.
    self_verifying.result_text = json.dumps(
        {
            "result": json.dumps(
                {
                    "mode": "insertion",
                    "verification": {
                        "source": "post_write_read",
                        "created_suggestions": [],
                    },
                }
            )
        }
    )
    findings = classify(
        SUGGESTION_FACTS,
        make_transcript([make_call(0, "get_doc_review_view"), self_verifying]),
        passed=True,
    )
    (finding,) = [f for f in findings if f.code == "no_end_state_verification"]
    assert "carried a verification block" in finding.detail

    blind = make_call(1, "suggest_doc_edit", {"start_index": 5, "text": "x"})
    (bare,) = [
        f
        for f in classify(
            SUGGESTION_FACTS,
            make_transcript([make_call(0, "get_doc_review_view"), blind]),
            passed=True,
        )
        if f.code == "no_end_state_verification"
    ]
    assert "carried a verification block" not in bare.detail


def test_gave_up_early_on_timeout_budget_and_prose():
    timed_out = make_transcript([make_call(0, "get_doc_review_view")])
    assert "gave_up_early" in codes(
        classify(SUGGESTION_FACTS, timed_out, passed=False, timed_out=True)
    )

    budget = make_transcript(
        [make_call(0, "get_doc_review_view")], subtype="error_max_budget"
    )
    assert "gave_up_early" in codes(classify(SUGGESTION_FACTS, budget, passed=False))

    prose = make_transcript(
        [make_call(0, "get_doc_review_view")],
        result_text="I could not find a tool that suggests edits, so I stopped here.",
    )
    assert "gave_up_early" in codes(classify(SUGGESTION_FACTS, prose, passed=False))


def test_harness_fault_is_separated_from_agent_mistakes():
    transcript = Transcript(
        mcp_servers=[{"name": "gdocsmock", "status": "pending"}],
        subtype="success",
        result_text="I'll start by reading the document.",
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=False)
    assert "harness_mcp_unavailable" in codes(findings)


def test_repeats_are_flagged_within_a_run():
    transcript = make_transcript(
        [
            make_call(0, "suggest_doc_edit", error="start_index must be >= 1"),
            make_call(1, "suggest_doc_edit", error="end_index must be greater than"),
            make_call(2, "suggest_doc_edit", {"start_index": 5, "text": "x"}),
            make_call(3, "get_doc_review_view"),
        ]
    )
    findings = classify(SUGGESTION_FACTS, transcript, passed=True)
    index_findings = [f for f in findings if f.code == "index_error"]
    assert len(index_findings) == 2
    assert all(f.repeated for f in index_findings)


def test_repeat_report_marks_a_class_systemic_at_thirty_percent_of_runs():
    clean: list[Finding] = []
    one_index = [Finding("index_error", "x")]
    report = repeat_report([one_index, clean, clean, clean])
    assert report["index_error"]["runs"] == 1
    assert report["index_error"]["run_share"] == 0.25
    assert report["index_error"]["systemic"] is False

    report = repeat_report([one_index, one_index, clean, clean])
    assert report["index_error"]["run_share"] == 0.5
    assert report["index_error"]["systemic"] is True


def test_repeat_report_marks_in_run_repeats_systemic_even_when_rare():
    twice = [Finding("param_shape_error", "a"), Finding("param_shape_error", "b")]
    report = repeat_report([twice] + [[] for _ in range(9)])
    assert report["param_shape_error"]["run_share"] == 0.1
    assert report["param_shape_error"]["repeated_within_run"] == 1
    assert report["param_shape_error"]["systemic"] is True
