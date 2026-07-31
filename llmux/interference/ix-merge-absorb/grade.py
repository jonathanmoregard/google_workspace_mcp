"""Grade ix-merge-absorb: SPEC §6 eats the id the agent is holding.

The production-confirmed case. The agent creates a suggestion and is handed
its id; the same account, from another session, then edits adjacent text.
§6 merges them, survivor selection is greatest ``touchedAt``, so the *later*
edit survives and the agent's id is the one absorbed. Its reply comes back
400 "the suggestion ID ... is invalid" even though its edit is still sitting
in the document -- it is simply no longer called what the agent thinks it is
called.

Everything here is therefore id-agnostic on purpose. Pinning the expected
suggestion id would grade the agent's phrasing (a replacement produces two
ids and a merge, an insertion one) rather than its recovery. What is checked
is the shape §6 guarantees: one surviving card covering both pieces of text,
with the agent's reply attached to it.
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

DOCUMENT_ID = "ix-doc-data"
ME = "mockuser"
MUST_FIRE = ("second-session-extends-the-edit",)
AGENT_TEXT = " We will publish the raw data too."
OTHER_TEXT = " (pending legal review)"

TIMELINE = [
    {
        "name": "agent-appends-the-sentence",
        "kind": "overlapping_suggestion",
        "editor": ME,
        "params": {
            "after_text": "We will release the model weights.",
            "text": AGENT_TEXT,
        },
    },
    {
        "name": "second-session-extends-the-edit",
        "kind": "merge_absorb",
        "editor": ME,
        "params": {
            "after_suggestion": "$latest",
            "text": OTHER_TEXT,
            "author": ME,
        },
    },
    {
        # Replying to the SURVIVOR, which is what recovery looks like. The id
        # is resolved from the replayed state, never written down.
        "name": "agent-replies-to-the-survivor",
        "kind": "reply_thread",
        "editor": ME,
        "params": {
            "suggestion_id": "sug.mockuser.2",
            "content": "Publishing the data is what makes the weights useful.",
        },
    },
]


def grade(backend: Any) -> dict[str, Any]:
    report, harness = harness_gate(backend, MUST_FIRE)
    if harness:
        return {"pass": False, "score": 0.0, "failures": harness}

    # A merge_absorb that did not actually merge is reported by harness_gate
    # as ineffective, so by here the §6 absorption is known to have happened.
    card = Scorecard()
    doc = backend.documents.get(DOCUMENT_ID)
    if not card.check(
        doc is not None, f"document {DOCUMENT_ID} is gone from the backend"
    ):
        return card.result()

    want = expected_doc(__file__, TIMELINE, DOCUMENT_ID)
    ours = mine(doc, ME)

    card.check(
        len(doc.registry) == 1,
        f"expected exactly one pending suggestion after the merge, found "
        f"{len(doc.registry)}: {', '.join(sorted(doc.registry))}",
    )
    card.check(
        doc.original_text() == want.original_text(),
        f"the base text is {doc.original_text()!r}, expected "
        f"{want.original_text()!r}: the addition was applied directly instead "
        "of being suggested",
    )
    card.check(
        doc.final_text() == want.final_text(),
        f"accepting everything gives {doc.final_text()!r}, expected "
        f"{want.final_text()!r}",
    )

    if not ours:
        card.skip("no pending suggestion by the agent survives to carry a reply")
        card.skip("cannot check the merged card's contents: nothing is pending")
    else:
        survivor = doc.registry[ours[-1]]
        added = doc.label(survivor.id)["added"]
        card.check(
            AGENT_TEXT.strip() in added and OTHER_TEXT.strip() in added,
            f"the surviving card adds {added!r}; after the §6 merge it should "
            f"cover both the agent's sentence and {OTHER_TEXT.strip()!r}",
        )
        card.check(
            any(post.author == ME for post in survivor.thread),
            f"the surviving suggestion {survivor.id} carries no reply from the "
            "agent: the reply was aimed at the absorbed id and never landed "
            "anywhere",
        )

    card.absorb(adaptation_failures(report))
    return card.result()
