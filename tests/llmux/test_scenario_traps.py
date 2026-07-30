"""The adversarial scenarios actually trap the naive solution.

A scenario is only "adversarial" if the obvious wrong approach *fails*. That
is not self-evident from the scenario definition -- it depends on the model,
the merge rule and the index arithmetic all lining up -- so each trap is
pinned here by running the naive solution and asserting the grade is not a
pass. If a change to mockdocs ever makes one of these traps close by itself,
this file says so instead of the corpus quietly getting easier.
"""

from __future__ import annotations

import json

import pytest

from mockdocs.adapter import utf16_offsets

from llmux.scenarios.oracle import run_solution
from llmux.scenarios.primitives import after
from llmux.scenarios.validate import GENERATED, backend_from_seed, load_grader


def _load(name: str):
    directory = GENERATED / name
    seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    solution = json.loads((directory / "solution.json").read_text(encoding="utf-8"))
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    return directory, seed, solution, expected["document_id"]


def _accept_everything(backend, document_id) -> list[dict]:
    return [
        {
            "tool": "manage_document_suggestion",
            "args": {"action": "accept", "suggestion_id": sid},
        }
        for sid in sorted(backend.get_document(document_id).registry)
    ]


def test_stale_index_lands_in_the_wrong_sentence():
    """Compute the insertion index before resolving and it is 34 UTF-16 units
    too far right, because accepted deletions and rejected insertions both
    remove characters."""
    directory, seed, solution, doc_id = _load("adversarial-stale-index")
    backend = backend_from_seed(seed)
    doc = backend.get_document(doc_id)

    grapheme, _ = after("Section 2: Plan").resolve(doc)
    stale_index = utf16_offsets(doc.chars)[grapheme]
    correct_index = solution[-1]["args"]["start_index"]
    assert stale_index != correct_index

    naive = solution[:-1] + [
        {
            "tool": "suggest_doc_edit",
            "args": {"start_index": stale_index, "text": " (approved)"},
        }
    ]
    run_solution(backend, doc_id, naive)

    text = backend.get_document(doc_id).display_text()
    assert "Section 2: Plan (approved)" not in text
    assert "the (approved) end of the month" in text
    verdict = load_grader(directory)(backend)
    assert verdict["pass"] is False


def test_both_marks_replacement_destroys_the_word_it_looks_like_it_keeps():
    """``Replace: "popul" with "ular"`` reads as "popular"; §7 makes it
    "ularar"."""
    directory, seed, solution, doc_id = _load("adversarial-both-marks-replacement")
    backend = backend_from_seed(seed)
    run_solution(backend, doc_id, _accept_everything(backend, doc_id))

    assert "ularar" in backend.get_document(doc_id).display_text()
    verdict = load_grader(directory)(backend)
    assert verdict["pass"] is False
    assert verdict["score"] < 1.0


def test_a_noop_decision_is_only_observable_through_the_comment():
    """Accepting a no-op and rejecting it produce byte-identical documents,
    which is exactly why the task demands a comment: without it there is
    nothing to grade, and with it the partition is fully observable."""
    directory, seed, solution, doc_id = _load("adversarial-noop-suggestions")
    correct = backend_from_seed(seed)
    run_solution(correct, doc_id, solution)

    naive = backend_from_seed(seed)
    run_solution(naive, doc_id, _accept_everything(naive, doc_id))

    assert (
        naive.get_document(doc_id).display_text()
        == correct.get_document(doc_id).display_text()
    ), "the text alone cannot distinguish the two, by construction"

    grade = load_grader(directory)
    assert grade(correct)["pass"] is True
    naive_verdict = grade(naive)
    assert naive_verdict["pass"] is False
    assert any("comment thread" in f for f in naive_verdict["failures"])


def test_rejecting_the_parent_first_kills_the_nested_suggestion_id():
    """§11.1 I2: rejecting Alice removes the only characters Bob's nested
    insertion marks, so it leaves the registry. The accept the task asks for
    then 400s on an id that was valid one call earlier -- the same end state
    (L3), reached through an error the agent has to interpret."""
    directory, seed, solution, doc_id = _load("hard-nested-insertion")
    backend = backend_from_seed(seed)
    rejects_first = sorted(
        solution, key=lambda call: 0 if call["args"]["action"] == "reject" else 1
    )
    with pytest.raises(RuntimeError, match="no longer exists|HttpError"):
        run_solution(backend, doc_id, rejects_first)

    # ...and the intended order works on the same scenario.
    clean = backend_from_seed(seed)
    run_solution(clean, doc_id, solution)
    assert load_grader(directory)(clean)["pass"] is True


def test_an_off_by_one_anchor_quotes_the_wrong_text():
    """One UTF-16 unit early on an emoji-bearing document is not an error --
    it is a comment anchored to the wrong characters, which only the quote
    reveals."""
    directory, seed, solution, doc_id = _load("hard-utf16-emoji-anchors")
    backend = backend_from_seed(seed)
    shifted = [
        {
            "tool": call["tool"],
            "args": {**call["args"], "start_index": call["args"]["start_index"] - 1},
        }
        if call["tool"] == "create_anchored_doc_comment"
        else call
        for call in solution
    ]
    run_solution(backend, doc_id, shifted)

    quotes = {t["plainTextQuote"] for t in backend.comments[doc_id]}
    assert quotes == {" index rebuild", "\nPrimary"}
    assert load_grader(directory)(backend)["pass"] is False


def test_the_merged_card_is_not_a_pure_deletion():
    """H2's decoy: Carol's two edits merged into one Replace card (§6/L8), so
    "the suggestion that deletes 'guidance'" is not a deletion at all."""
    directory, seed, solution, doc_id = _load("hard-overlap-and-merge")
    backend = backend_from_seed(seed)
    doc = backend.get_document(doc_id)
    decoys = [
        sid
        for sid in doc.registry
        if "guidance" in doc.label(sid)["struck"] and doc.label(sid)["added"]
    ]
    assert len(decoys) == 1
    assert doc.label(decoys[0])["kind"] == "Replace"

    naive = solution + [
        {
            "tool": "manage_document_suggestion",
            "args": {"action": "accept", "suggestion_id": decoys[0]},
        }
    ]
    fresh = backend_from_seed(seed)
    with pytest.raises(RuntimeError):
        # The oracle already rejected it, so accepting now 400s -- and an
        # agent that accepted it *instead* loses the word entirely.
        run_solution(fresh, doc_id, naive)


def test_every_adversarial_scenario_fails_the_accept_everything_agent():
    """The cheapest possible wrong policy must not pass any of them."""
    for directory in sorted(GENERATED.iterdir()):
        meta_path = directory / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["tier"] != "adversarial":
            continue
        seed = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
        backend = backend_from_seed(seed)
        run_solution(
            backend,
            meta["document_id"],
            _accept_everything(backend, meta["document_id"]),
        )
        verdict = load_grader(directory)(backend)
        assert verdict["pass"] is False, f"{meta['id']} passes on accept-everything"
