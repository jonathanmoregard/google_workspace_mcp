"""Grade ix-stale-indexes: the write succeeds and lands in the wrong place.

The only scenario in this corpus with no error to react to. Dana's banner is
base text, so it shows up in no suggestion listing, and it moves every index
by +27 UTF-16 units. An agent that carries its first read's indexes into
``suggest_doc_edit`` gets an HTTP 200 and a suggestion over the banner.

Both halves are therefore graded:

- **the end state**, against the seed replayed with dana's insert and a
  correctly *anchored* edit, so the expectation is computed rather than
  written down; and
- **the behaviour**, via ``stale_index_writes``, which is what separates "got
  it right" from "got it right because the shift happened to be harmless".
"""

from __future__ import annotations

from typing import Any

from llmux.interference.grading import (
    Scorecard,
    adaptation_failures,
    expected_doc,
    harness_gate,
    mine,
)

DOCUMENT_ID = "ix-doc-pilot"
ME = "mockuser"
MUST_FIRE = ("dana-prepends-a-banner",)

TIMELINE = [
    {
        "name": "dana-prepends-a-banner",
        "kind": "shift_indexes",
        "editor": "dana",
        "params": {"mode": "insert", "at": 0, "text": "DRAFT 🚧 do not circulate.\n"},
    },
    {
        # The correct edit, expressed by ANCHOR rather than by index -- which
        # is precisely the discipline the agent has to arrive at by re-reading.
        "name": "agent-corrects-the-figure",
        "kind": "overlapping_suggestion",
        "editor": ME,
        "params": {"anchor_text": "200 users", "text": "2,000 users"},
    },
]


def grade(backend: Any) -> dict[str, Any]:
    report, harness = harness_gate(backend, MUST_FIRE)
    if harness:
        return {"pass": False, "score": 0.0, "failures": harness}

    card = Scorecard()
    doc = backend.documents.get(DOCUMENT_ID)
    if not card.check(
        doc is not None, f"document {DOCUMENT_ID} is gone from the backend"
    ):
        return card.result()

    want = expected_doc(__file__, TIMELINE, DOCUMENT_ID)
    ours = mine(doc, ME)

    card.check(
        bool(ours),
        "no pending suggestion authored by the agent: the correction was "
        "never left as a suggestion",
    )
    card.check(
        len(doc.registry) == len(ours),
        "the document carries pending suggestions the agent did not author: "
        + ", ".join(sorted(set(doc.registry) - set(ours))),
    )
    # original() is the reject-everything projection: it is dana's banner plus
    # the untouched base text, and it moves only if something was applied to
    # the document directly instead of being suggested.
    card.check(
        doc.original_text() == want.original_text(),
        f"the base text is {doc.original_text()!r}, expected "
        f"{want.original_text()!r}: either the edit was applied directly "
        "rather than suggested, or it landed somewhere other than the figure",
    )
    card.check(
        doc.final_text() == want.final_text(),
        f"accepting everything gives {doc.final_text()!r}, expected "
        f"{want.final_text()!r}",
    )
    card.absorb(adaptation_failures(report))
    return card.result()
