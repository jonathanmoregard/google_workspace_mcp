"""Property tests for the interleaving engine itself.

The engine has to be trustworthy before anything it produces can be evidence
about an agent, so it is tested first and on its own terms:

* an interference fires **exactly once**, and fires **iff** the agent's call
  sequence actually contains the call it was pinned to;
* the same script over the same call sequence produces the **same** record,
  byte for byte -- there is no clock, no randomness and no scheduling in the
  firing decision;
* firings are **ordered** by agent call, and a ``before`` firing precedes an
  ``after`` firing on the same call;
* the spec invariants hold **throughout**, not merely at the end.

If any of these fail, a finding of ``acted_on_vanished_id`` in a real batch
would be indistinguishable from a bug in this module -- which is exactly the
confusion the tests exist to prevent.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mockdocs.concurrency import (
    Interference,
    InterferenceEngine,
    Trigger,
    parse_script,
)
from mockdocs.fake_services import FakeBackend
from mockdocs.state import dump_backend, load_backend

#: Tool names an interleaved run realistically produces.
TOOLS = (
    "list_document_suggestions",
    "get_doc_review_view",
    "suggest_doc_edit",
    "manage_document_suggestion",
    "reply_to_doc_thread",
)

SEED = {
    "me": "mockuser",
    "documents": [
        {
            "document_id": "prop-doc",
            "title": "Property Doc",
            "text": "Alpha beta gamma.\nDelta epsilon 🎉 zeta.\n",
            "suggestions": [
                {"op": "delete", "start": 6, "end": 10, "author": "bob"},
                {"op": "insert", "index": 0, "text": "Note: ", "author": "carol"},
            ],
            "comments": [{"content": "why?", "quote": "Alpha", "author": "erin"}],
        }
    ],
}

#: Ops chosen so that a randomly generated script can never fail to APPLY --
#: an application error would show up as an invariant violation and mask the
#: property under test. Every one of them is a no-op when its target is
#: already gone.
SAFE_PARAMS = (
    ("shift_indexes", {"mode": "insert", "at": 0, "text": "X"}),
    ("shift_indexes", {"mode": "insert", "at": 0, "text": "🎉"}),
    ("shift_indexes", {"mode": "delete", "start": 0, "span": 1}),
    ("resolve_under_agent", {"suggestion_id": "sug.bob.1", "action": "accept"}),
    ("resolve_under_agent", {"suggestion_id": "sug.bob.1", "action": "reject"}),
    ("resolve_under_agent", {"suggestion_id": "sug.carol.2", "action": "accept"}),
    ("resolve_thread", {"comment_id": "comment.1", "action": "delete"}),
    ("resolve_thread", {"comment_id": "comment.1", "action": "resolve"}),
    ("merge_absorb", {"at": 0, "text": "hm ", "author": "mockuser"}),
)


def make_backend() -> FakeBackend:
    backend = FakeBackend(me="mockuser")
    backend.seed(SEED)
    return backend


@st.composite
def interference_specs(draw: st.DrawFn) -> dict:
    kind, params = draw(st.sampled_from(SAFE_PARAMS))
    return {
        "name": f"ix{draw(st.integers(min_value=0, max_value=999))}",
        "kind": kind,
        "editor": draw(st.sampled_from(("bob", "carol", "erin", "mockuser"))),
        "params": dict(params),
        "trigger": {
            "when": draw(st.sampled_from(("before", "after"))),
            "tool": draw(
                st.one_of(
                    st.none(),
                    st.sampled_from(TOOLS),
                    st.lists(st.sampled_from(TOOLS), min_size=1, max_size=2),
                )
            ),
            "nth": draw(st.integers(min_value=1, max_value=3)),
        },
    }


@st.composite
def scripts(draw: st.DrawFn) -> list[dict]:
    specs = draw(st.lists(interference_specs(), min_size=0, max_size=4))
    for position, spec in enumerate(specs):  # names must be unique
        spec["name"] = f"{spec['name']}-{position}"
    return specs


call_sequences = st.lists(st.sampled_from(TOOLS), min_size=0, max_size=8)


def drive(script: list[dict], calls: list[str]) -> InterferenceEngine:
    backend = make_backend()
    engine = InterferenceEngine(backend, parse_script(script))
    for tool in calls:
        engine.around(tool, {"document_id": "prop-doc"}, lambda: None)
    return engine


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scripts(), call_sequences)
def test_fires_once_and_only_when_its_call_happened(script, calls):
    engine = drive(script, calls)
    record = engine.record

    fired = record.fired_names
    assert len(fired) == len(set(fired)), "an interference fired more than once"

    for spec in script:
        trigger = Trigger.from_dict(spec["trigger"])
        matching = sum(1 for tool in calls if trigger.matches(tool, {}))
        expected = matching >= trigger.nth
        assert (spec["name"] in fired) is expected, (
            f"{spec['name']} fired={spec['name'] in fired} but its trigger "
            f"matched {matching} of {len(calls)} calls with nth={trigger.nth}"
        )


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scripts(), call_sequences)
def test_the_same_sequence_produces_the_same_record(script, calls):
    first = drive(script, calls)
    second = drive(script, calls)
    assert first.record.as_dict() == second.record.as_dict()
    # ...and the documents they left behind are identical too, which is what
    # makes a graded interleaved run reproducible rather than merely repeatable.
    assert json.dumps(dump_backend(first.backend), sort_keys=True) == json.dumps(
        dump_backend(second.backend), sort_keys=True
    )


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scripts(), call_sequences)
def test_firings_are_ordered_by_agent_call(script, calls):
    record = drive(script, calls).record
    ordinals = [entry.at_call for entry in record.fired]
    assert ordinals == sorted(ordinals)
    for earlier, later in zip(record.fired, record.fired[1:]):
        if earlier.at_call == later.at_call:
            phases = (earlier.when, later.when)
            assert phases != ("after", "before"), (
                "a before-firing was recorded after an after-firing on the "
                "same agent call"
            )


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scripts(), call_sequences)
def test_invariants_hold_after_every_interference(script, calls):
    engine = drive(script, calls)
    assert engine.record.violations == []
    engine.backend.documents["prop-doc"].check_invariants()


@settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(scripts(), call_sequences)
def test_the_record_survives_a_state_round_trip(script, calls):
    engine = drive(script, calls)
    restored = load_backend(dump_backend(engine.backend))
    assert restored.concurrency.as_dict() == engine.record.as_dict()


# ---------------------------------------------------------------------------
# Worked examples: the properties above are general, these pin the meaning.
# ---------------------------------------------------------------------------


def test_nth_counts_only_matching_calls():
    """ "After the agent's 2nd list_document_suggestions" means exactly that."""
    script = [
        {
            "name": "second-listing",
            "kind": "shift_indexes",
            "editor": "bob",
            "params": {"mode": "insert", "at": 0, "text": "!"},
            "trigger": {
                "when": "after",
                "tool": "list_document_suggestions",
                "nth": 2,
            },
        }
    ]
    engine = drive(
        script,
        [
            "list_document_suggestions",
            "suggest_doc_edit",
            "get_doc_review_view",
            "list_document_suggestions",
            "list_document_suggestions",
        ],
    )
    (fired,) = engine.record.fired
    assert fired.at_call == 4
    assert fired.at_tool == "list_document_suggestions"


def test_before_fires_ahead_of_the_tool_body():
    """A ``before`` trigger changes the document the call is about to see."""
    backend = make_backend()
    doc = backend.documents["prop-doc"]
    engine = InterferenceEngine(
        backend,
        parse_script(
            [
                {
                    "name": "pull-the-rug",
                    "kind": "resolve_under_agent",
                    "editor": "bob",
                    "params": {"suggestion_id": "sug.bob.1", "action": "accept"},
                    "trigger": {"when": "before", "tool": "manage_document_suggestion"},
                }
            ]
        ),
    )
    seen: list[bool] = []
    engine.around(
        "manage_document_suggestion",
        {"suggestion_id": "sug.bob.1"},
        lambda: seen.append("sug.bob.1" in doc.registry),
    )
    assert seen == [False], "the tool body still saw the suggestion"


def test_args_contain_narrows_a_trigger_to_one_target():
    script = [
        {
            "name": "only-for-carols-id",
            "kind": "shift_indexes",
            "editor": "bob",
            "params": {"mode": "insert", "at": 0, "text": "!"},
            "trigger": {
                "when": "before",
                "tool": "manage_document_suggestion",
                "args_contain": {"suggestion_id": "carol"},
            },
        }
    ]
    backend = make_backend()
    engine = InterferenceEngine(backend, parse_script(script))
    engine.around(
        "manage_document_suggestion", {"suggestion_id": "sug.bob.1"}, lambda: None
    )
    assert engine.record.fired == []
    engine.around(
        "manage_document_suggestion", {"suggestion_id": "sug.carol.2"}, lambda: None
    )
    assert [f.name for f in engine.record.fired] == ["only-for-carols-id"]


def test_an_interference_that_cannot_apply_is_recorded_not_raised():
    """An engine fault must be visible to the grader, never fatal to the run.

    A run that dies here would look like an agent timeout; a run that records
    it is correctly readable as a harness fault.
    """
    backend = make_backend()
    engine = InterferenceEngine(
        backend,
        [
            Interference(
                name="impossible",
                kind="reply_thread",
                trigger=Trigger(when="after", tools=("list_document_suggestions",)),
                editor="erin",
                params={"suggestion_id": "sug.nobody.9", "content": "hi"},
            )
        ],
    )
    engine.around("list_document_suggestions", {}, lambda: None)
    assert engine.record.fired_names == ["impossible"]
    assert engine.record.violations  # surfaces as harness_interference_fault


def test_an_interference_that_hits_nothing_is_reported_as_ineffective():
    """Silently doing nothing is the subtlest way a scenario can rot.

    A script must stay safe to apply to any state, so a missing target is a
    no-op rather than an error -- which means the emptiness has to be caught
    when the record is read, or the run reads as a well-behaved agent on an
    undisturbed document.
    """
    from llmux.runner.interference import InterferenceReport

    backend = make_backend()
    engine = InterferenceEngine(
        backend,
        [
            Interference(
                name="ghost",
                kind="resolve_thread",
                trigger=Trigger(when="after", tools=("list_document_suggestions",)),
                editor="erin",
                params={"comment_id": "comment.404", "action": "resolve"},
            )
        ],
    )
    engine.around("list_document_suggestions", {}, lambda: None)
    assert engine.record.violations == []  # nothing broke...
    report = InterferenceReport.from_backend(backend)
    assert [name for name, _ in report.ineffective] == [
        "ghost"
    ]  # ...but nothing happened
