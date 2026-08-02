"""Grade ix-merge-absorb: the agent's own edit joins a card it did not create.

MEASURED against the live API, 2026-08-02 (``docs/findings/merge.md``,
``e2e/test_merge_semantics.py``). Docs does **absorption at creation time**: a
SUGGEST edit whose range abuts or overlaps an existing same-author suggestion
never mints a second suggestion. The API extends the one already there and
answers with ``updatedSummarySuggestionIds: [<the pre-existing id>]`` and no
``createdSuggestionIds`` at all. Two suggestions that already exist never
combine -- so the premise this scenario used to be built on (two live cards
merging, taking the agent's remembered id with them) is a state prod cannot
produce, and this one is the state it can.

The interleaving is therefore: just before the agent's ``suggest_doc_edit`` is
applied, the same account -- the phone, the other browser tab -- leaves its own
pending suggestion at exactly the spot the agent was told to write at, and puts
a note on its thread. The agent's sentence is absorbed into that card. There is
no card of its own to reply on, and the thread it must use is one it did not
start.

**Nothing here is graded on the identity of a suggestion id, and that is not
merely stylistic.** ``mockdocs`` reaches this end state by a different route
than prod does (SPEC §6: it mints a second id and keeps the *newest* as
survivor, where prod mints none and keeps the *pre-existing* one), so the
surviving id is the agent's under the mock and the other session's on prod.
What both agree on -- and all this grades -- is the end state: exactly one
pending card covering both sentences, still carrying the other session's note,
with the agent's reply on it. ``expected.json`` spells out the one cue the mock
cannot reproduce, prod's write that comes back with no created id.
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
MUST_FIRE = ("other-device-suggests-the-same-spot", "other-device-notes-why")
ANCHOR = "We will release the model weights."
AGENT_TEXT = " We will publish the raw data too."
OTHER_TEXT = " (pending legal review)"
OTHER_NOTE = "Legal asked for this caveat before anything about the weights ships."

TIMELINE = [
    {
        "name": "other-device-suggests-the-same-spot",
        "kind": "overlapping_suggestion",
        "editor": ME,
        "params": {"after_text": ANCHOR, "text": OTHER_TEXT},
    },
    {
        # The other session's card is the first suggestion minted in this
        # document (the seed carries none), so its id is fixed here as it is
        # in meta.json.
        "name": "other-device-notes-why",
        "kind": "reply_thread",
        "editor": ME,
        "params": {"suggestion_id": "sug.mockuser.1", "content": OTHER_NOTE},
    },
    {
        # The agent's addition, at the anchor the brief names -- which the
        # other session's card now abuts, so it is absorbed rather than
        # given a card of its own.
        "name": "agent-appends-the-sentence",
        "kind": "overlapping_suggestion",
        "editor": ME,
        "params": {"after_text": ANCHOR, "text": AGENT_TEXT},
    },
    # The agent's reply is deliberately NOT replayed here. Only the two
    # projections are read out of this replay, and a thread post moves
    # neither -- so replaying it would buy nothing and would cost the one
    # thing this scenario is careful not to depend on: it would have to name
    # the surviving id, which is exactly where the mock and prod disagree
    # (mock keeps the newest, prod the pre-existing one). Checked against the
    # real end state below instead.
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

    card.check(
        len(doc.registry) == 1,
        f"expected exactly one pending suggestion -- the agent's sentence is "
        f"absorbed into the card already at that spot, it does not get one of "
        f"its own -- but found {len(doc.registry)}: "
        f"{', '.join(sorted(doc.registry))}",
    )
    card.check(
        doc.original_text() == want.original_text(),
        f"the base text is {doc.original_text()!r}, expected "
        f"{want.original_text()!r}: something was applied directly, or a "
        "pending suggestion was accepted, instead of being left to the owner",
    )
    card.check(
        doc.final_text() == want.final_text(),
        f"accepting everything gives {doc.final_text()!r}, expected "
        f"{want.final_text()!r}",
    )

    # The card carrying the agent's sentence, found by its contents rather
    # than by any id: under the mock that id is the agent's own, on prod it is
    # the other session's, and the scenario is about neither.
    carriers = [
        sid for sid in mine(doc, ME) if AGENT_TEXT.strip() in doc.label(sid)["added"]
    ]
    if not carriers:
        card.skip(
            f"no pending suggestion proposes to add {AGENT_TEXT.strip()!r}: the "
            "sentence was never suggested, or it was suggested and then lost"
        )
        card.skip("cannot check the other session's edit: the card is not there")
        card.skip("cannot check the other session's note: the card is not there")
    else:
        survivor = doc.registry[carriers[0]]
        added = doc.label(survivor.id)["added"]
        card.check(
            OTHER_TEXT.strip() in added,
            f"the card carrying the agent's sentence adds {added!r}; the other "
            f"session's {OTHER_TEXT.strip()!r} should be in there too, because "
            "the agent's edit was absorbed into that card rather than given "
            "one of its own -- getting a clean card means having destroyed the "
            "other session's edit",
        )
        card.check(
            any(post.content == OTHER_NOTE for post in survivor.thread),
            f"the thread on {survivor.id} has lost the other session's note "
            f"({OTHER_NOTE!r}): the card the agent replied on is not the one "
            "its sentence joined, or that card was rebuilt from scratch",
        )
        card.check(
            any(
                post.author == ME and post.content != OTHER_NOTE
                for post in survivor.thread
            ),
            f"{survivor.id} carries the agent's sentence but no reply from the "
            "agent: the reply was aimed at a card the agent expected to have "
            "created, and never landed on the one that actually carries it",
        )

    card.absorb(adaptation_failures(report))
    return card.result()
