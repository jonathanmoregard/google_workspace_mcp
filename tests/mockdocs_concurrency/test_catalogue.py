"""Each interference does what its name says, and the checker really checks.

The catalogue is only useful if each entry produces the specific failure it
claims to. These tests pin the observable consequence of each one -- a
vanished id, a moved index, a dead thread, a both-marks character, an
absorbed suggestion -- rather than the fact that it ran.

The last two tests are about the checker itself. An invariant checker that
cannot fail is worse than none, because it converts "we never looked" into
"we looked and it was fine".
"""

from __future__ import annotations

import pytest

from mockdocs.concurrency import (
    Interference,
    InterferenceError,
    Trigger,
    apply_interference,
    check_model,
    replay,
)
from mockdocs.fake_services import FakeBackend
from mockdocs.model import Char, MockDoc

SEED = {
    "me": "mockuser",
    "documents": [
        {
            "document_id": "cat-doc",
            "title": "Catalogue",
            "text": "The brave new plan ships in March.\nRisks are unchanged.\n",
            "suggestions": [
                {"op": "replace", "start": 4, "end": 9, "text": "bold", "author": "bob"}
            ],
            "comments": [{"content": "hm", "quote": "Risks", "author": "erin"}],
        }
    ],
}


def backend_with_seed() -> FakeBackend:
    backend = FakeBackend(me="mockuser")
    backend.seed(SEED)
    return backend


def fire(backend, kind, params, editor="other"):
    interference = Interference(
        name=kind, kind=kind, trigger=Trigger(), editor=editor, params=params
    )
    effect, violations, _ = apply_interference(backend, interference)
    assert violations == [], violations
    return effect


def test_resolve_under_agent_removes_the_id_and_names_the_collateral():
    backend = backend_with_seed()
    doc = backend.documents["cat-doc"]
    target = sorted(doc.registry)[0]

    effect = fire(
        backend, "resolve_under_agent", {"suggestion_id": target, "action": "accept"}
    )

    assert effect["existed"] is True
    assert target not in doc.registry
    assert "bold" in doc.display_text()  # accepted, so the replacement landed
    assert isinstance(effect["also_removed"], list)


def test_resolve_under_agent_on_a_dead_id_is_a_recorded_no_op():
    backend = backend_with_seed()
    effect = fire(
        backend,
        "resolve_under_agent",
        {"suggestion_id": "sug.ghost.1", "action": "reject"},
    )
    assert effect["existed"] is False


def test_shift_indexes_moves_every_index_without_creating_a_suggestion():
    backend = backend_with_seed()
    doc = backend.documents["cat-doc"]
    before = sorted(doc.registry)

    effect = fire(
        backend,
        "shift_indexes",
        {"mode": "insert", "at": 0, "text": "DRAFT 🚧 "},
        "dana",
    )

    # One grapheme of the banner is two UTF-16 units, so the shift a Python
    # character count would compute is off by one. That gap is the point.
    assert effect["utf16_shift"] == 9
    assert len("DRAFT 🚧 ") == 8
    assert effect["suggestion_id"] is None
    assert sorted(doc.registry) == before, "a base-text edit must not create a card"
    assert doc.original_text().startswith("DRAFT 🚧 "), (
        "the banner is base text, so it is in the reject-everything view too"
    )


def test_shift_indexes_delete_removes_base_text():
    backend = backend_with_seed()
    doc = backend.documents["cat-doc"]
    effect = fire(
        backend, "shift_indexes", {"mode": "delete", "start": 0, "span": 4}, "dana"
    )
    assert effect["utf16_shift"] == -4
    assert not doc.display_text().startswith("The ")


def test_resolve_thread_delete_removes_the_thread():
    backend = backend_with_seed()
    effect = fire(
        backend,
        "resolve_thread",
        {"comment_id": "comment.1", "action": "delete"},
        "erin",
    )
    assert effect["existed"] is True
    assert backend.comments["cat-doc"] == []


def test_resolve_thread_resolve_leaves_it_readable_but_closed():
    backend = backend_with_seed()
    fire(
        backend,
        "resolve_thread",
        {"comment_id": "comment.1", "action": "resolve"},
        "erin",
    )
    (thread,) = backend.comments["cat-doc"]
    assert thread["status"] == "RESOLVED"


def test_overlapping_suggestion_creates_the_both_marks_state():
    """SPEC §5.1: text typed inside a pending deletion inherits its mark."""
    backend = backend_with_seed()
    doc = backend.documents["cat-doc"]

    frank = fire(
        backend,
        "overlapping_suggestion",
        {"anchor_text": "Risks are unchanged."},
        "frank",
    )
    frank_id = frank["suggestion_id"]
    mine = fire(
        backend,
        "overlapping_suggestion",
        {"anchor_text": "unchanged", "text": "stable"},
        "gale",
    )

    trapped = [
        c for c in doc.chars if mine["suggestion_id"] in c.ins and frank_id in c.dels
    ]
    assert trapped, "the overlap did not produce the both-marks state"
    # SPEC §3: such a character is in neither extreme projection.
    assert "stable" in doc.display_text()
    assert "stable" not in doc.original_text()
    assert "stable" not in doc.final_text()
    for char in trapped:
        assert char.render_state() == "both"


def test_merge_absorb_takes_the_agents_id_away():
    """SPEC §6: survivor is the greatest touchedAt, so the later edit wins."""
    backend = backend_with_seed()
    doc = backend.documents["cat-doc"]

    ours = fire(
        backend,
        "overlapping_suggestion",
        {"after_text": "Risks are unchanged.", "text": " We think."},
        "mockuser",
    )
    remembered = ours["suggestion_id"]
    assert remembered in doc.registry

    effect = fire(
        backend,
        "merge_absorb",
        {"after_suggestion": "$latest", "text": " (tbc)", "author": "mockuser"},
        "mockuser",
    )

    assert effect["merged"] is True
    assert remembered in effect["absorbed_ids"]
    assert remembered not in doc.registry, "the remembered id survived the merge"
    assert effect["survivor_id"] in doc.registry
    # L7: merging changed the granularity of choice, not the content.
    assert "We think." in doc.final_text() and "(tbc)" in doc.final_text()


def test_merge_absorb_refuses_when_the_agents_write_never_landed():
    backend = backend_with_seed()
    with pytest.raises(InterferenceError, match="no pending suggestion by"):
        fire(
            backend,
            "merge_absorb",
            {"after_suggestion": "$latest", "text": "x", "author": "nobody"},
        )


def test_replay_is_the_same_algebra_as_the_live_engine():
    """A grader's expected state and the live run share one code path."""
    live = backend_with_seed()
    fire(live, "shift_indexes", {"mode": "insert", "at": 0, "text": "Hi. "}, "dana")
    fire(
        live,
        "resolve_under_agent",
        {
            "suggestion_id": sorted(live.documents["cat-doc"].registry)[0],
            "action": "reject",
        },
    )

    replayed = replay(
        SEED,
        [
            {
                "name": "a",
                "kind": "shift_indexes",
                "editor": "dana",
                "params": {"mode": "insert", "at": 0, "text": "Hi. "},
            },
            {
                "name": "b",
                "kind": "resolve_under_agent",
                "editor": "other",
                "params": {"suggestion_id": "sug.bob.2", "action": "reject"},
            },
        ],
    )
    assert (
        replayed.documents["cat-doc"].display_text()
        == live.documents["cat-doc"].display_text()
    )


# ---------------------------------------------------------------------------
# The checker must be able to fail
# ---------------------------------------------------------------------------


def test_check_model_catches_an_orphan_mark():
    doc = MockDoc(text="abc", document_id="broken")
    doc.chars[0] = Char("a", ins={"sug.ghost.1"}, colour="ghost")
    violations = check_model(doc)
    assert any("I1" in v for v in violations)


def test_check_model_catches_a_moved_extreme():
    doc = MockDoc(text="abc", document_id="broken")
    violations = check_model(doc, original_before="xyz", final_before="xyz")
    assert sum("L7" in v for v in violations) == 2
