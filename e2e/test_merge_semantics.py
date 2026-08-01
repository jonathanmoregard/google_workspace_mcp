"""What the live API actually does when same-author suggestions touch.

Written against prod 2026-08-01 to retire three documented GUESSES (see
``docs/findings/merge.md`` for the raw transcripts):

1. ``mockdocs.model.MERGE_TOLERANCE = 0`` was a guess. It is **correct**, and
   the same value holds for all four insert/delete orderings: an edit that
   touches an existing same-author suggestion joins it, one unchanged
   character between them is enough to keep two cards.
2. The mock migrates an absorbed suggestion's comment thread onto the
   survivor (spec §10's *recommended* column). Live Docs never faces the
   question through this API: **no existing suggestion is ever destroyed by
   a merge**, so no thread is ever orphaned.
3. Whether a merge renames or reports a pre-merge id (``mockdocs/adapter.py``
   ``_resolve_merges``). Live never renames: the **pre-existing** suggestion
   absorbs the new edit and keeps its own id, and the write that was absorbed
   reports no ``createdSuggestionIds`` at all.

The single mechanism behind all three: what looks like a merge is
**absorption at creation time**. A SUGGEST edit abutting or overlapping an
existing same-author suggestion never mints a second id -- it extends the
one already there. Two suggestions that already exist stay two, even when a
later edit pushes them into contact (``test_two_cards_pushed_into_contact_...``).

That is a real divergence from ``mockdocs``' §6, which merges to a fixpoint
with the NEWEST suggestion surviving and migrates threads. The mock was left
alone deliberately -- see docs/findings/merge.md "What changes in the repo".
"""

from __future__ import annotations

import pytest

from e2e.mcp_session import tool_json
from e2e.run_report import REPORT
from e2e.util import poll_until

pytestmark = pytest.mark.e2e_preview

#: Body index 1 is '0', so index 10 is '9' -- a landmark that survives every
#: pending edit below, since suggested deletions keep their characters in the
#: SUGGESTIONS_INLINE coordinate space.
DIGITS = "0123456789ABCDEFGHIJ"

#: "alpha bravo charlie delta echo" with the words at:
#: alpha [1,6)  bravo [7,12)  charlie [13,20)  delta [21,26)  echo [27,31)
WORDS = "alpha bravo charlie delta echo"


def _suggest(mcp, email: str, doc_id: str, **kwargs) -> dict:
    args = {"user_google_email": email, "document_id": doc_id}
    args.update(kwargs)
    return tool_json(mcp.call_tool("suggest_doc_edit", args))


def _cards(mcp, email: str, doc_id: str, *, expect: int) -> dict[str, dict]:
    """``{suggestion_id: record}`` once the listing shows ``expect`` cards.

    Polls rather than sleeps: the write is committed before the tool returns,
    but the thread-bearing read is a separate request.
    """

    def _check():
        listing = tool_json(
            mcp.call_tool(
                "list_document_suggestions",
                {
                    "user_google_email": email,
                    "document_id": doc_id,
                    "fields": "full",
                },
            )
        )
        if listing["suggestion_count"] != expect:
            return None
        return {r["suggestion_id"]: r for r in listing["suggestions"]}

    return poll_until(_check, timeout=30, description=f"exactly {expect} card(s)")


# ---------------------------------------------------------------------------
# Q1 - the real merge tolerance
# ---------------------------------------------------------------------------


def test_a_touching_same_author_edit_joins_the_existing_card(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Gap 0 => one card, and the id it keeps is the FIRST one.

    Raw prod exchange (2026-08-01), second batch:

        {"requests": [{"insertText": {"location": {"index": 11},
                                      "text": "Y"}}],
         "writeControl": {"writeMode": "SUGGEST"}}
        -> {"suggestionResponses": [
              {"updatedSummarySuggestionIds": ["suggest.e79qrxxlopy"]}]}

    No ``createdSuggestionIds``, and the id named is the one the FIRST batch
    created. That answers Q3: the survivor is an original, not a new id, and
    nothing is renamed.
    """
    doc_id = make_scratch_doc("-merge-gap0", content=DIGITS)
    email = ga_auth.email

    first = _suggest(mcp, email, doc_id, start_index=10, text="X")
    assert first["mode"] == "insertion"
    (sid,) = first["created_suggestion_ids"]

    (record,) = _cards(mcp, email, doc_id, expect=1).values()
    assert (record["start_index"], record["end_index"]) == (10, 11), record

    # Index 11 is exactly where the pending insertion ends: gap 0.
    second = _suggest(mcp, email, doc_id, start_index=11, text="Y")
    REPORT.note(
        "gap-0 insert-then-insert: "
        f"created_suggestion_ids={second['created_suggestion_ids']!r}, "
        f"verification={second['verification']!r}"
    )
    assert second["created_suggestion_ids"] == [], (
        "prod minted a second suggestion for a touching same-author edit - "
        f"MERGE_TOLERANCE is no longer 0. Response: {second}"
    )

    cards = _cards(mcp, email, doc_id, expect=1)
    assert list(cards) == [sid], (
        f"the survivor is not the pre-existing id {sid!r}: {list(cards)!r}"
    )
    survivor = cards[sid]
    assert survivor["summary_text"] == "Add: “XY”", survivor
    assert (survivor["start_index"], survivor["end_index"]) == (10, 12), survivor

    # The write had no id of its own to report, so the tool must fall back to
    # the card now covering the range rather than answering with nothing.
    verification = second["verification"]
    assert verification["created_suggestions"] == [], verification
    (echo,) = verification["suggestions_at_edit_range"]
    assert echo["suggestion_id"] == sid, echo


def test_a_one_character_gap_keeps_two_cards(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Gap 1 => two cards. With the test above, MERGE_TOLERANCE == 0 exactly.

    Measured for all four orderings on fresh documents (one per case):

        mode      gap 0                      gap 1
        ins_ins   updatedSummarySuggestionIds  createdSuggestionIds
        ins_del   updatedSummarySuggestionIds  createdSuggestionIds
        del_ins   updatedSummarySuggestionIds  createdSuggestionIds
        del_del   updatedSummarySuggestionIds  createdSuggestionIds

    so the tolerance does NOT differ between insert-then-insert and
    insert-then-delete, which spec §13.2 suspected it might.
    """
    doc_id = make_scratch_doc("-merge-gap1", content=DIGITS)
    email = ga_auth.email

    first = _suggest(mcp, email, doc_id, start_index=10, text="X")
    (sid1,) = first["created_suggestion_ids"]

    # Index 12 leaves exactly one unchanged character ('9', at [11,12))
    # between the two pending insertions.
    second = _suggest(mcp, email, doc_id, start_index=12, text="Y")
    REPORT.note(
        "gap-1 insert-then-insert: "
        f"created_suggestion_ids={second['created_suggestion_ids']!r}"
    )
    (sid2,) = second["created_suggestion_ids"]
    assert sid2 != sid1

    cards = _cards(mcp, email, doc_id, expect=2)
    assert set(cards) == {sid1, sid2}, cards
    assert cards[sid1]["summary_text"] == "Add: “X”", cards[sid1]
    assert cards[sid2]["summary_text"] == "Add: “Y”", cards[sid2]


def test_a_deletion_touching_a_pending_insertion_joins_it(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Tolerance 0 is not kind-specific: a delete abutting an insert joins it.

    The joined card relabels itself ``Replace: "9" with "X"`` -- spec §8's
    label function applied to the union of the two edits, computed by prod.
    """
    doc_id = make_scratch_doc("-merge-insdel", content=DIGITS)
    email = ga_auth.email

    first = _suggest(mcp, email, doc_id, start_index=10, text="X")
    (sid,) = first["created_suggestion_ids"]

    # The pending "X" occupies [10,11); '9' is now at [11,12).
    second = _suggest(mcp, email, doc_id, start_index=11, end_index=12)
    assert second["mode"] == "deletion"
    REPORT.note(
        "gap-0 insert-then-delete: "
        f"created_suggestion_ids={second['created_suggestion_ids']!r}"
    )
    assert second["created_suggestion_ids"] == [], second

    cards = _cards(mcp, email, doc_id, expect=1)
    assert list(cards) == [sid], cards
    assert cards[sid]["summary_text"] == "Replace: “9” with “X”", cards[sid]


# ---------------------------------------------------------------------------
# Q2 + Q3 - threads, and ids, when suggestions are pushed together
# ---------------------------------------------------------------------------


def test_an_edit_spanning_two_pending_cards_destroys_neither_card_nor_thread(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """The question spec §13.3 asks never arises on prod.

    Two threaded same-author deletions, then one deletion spanning both. The
    mock would collapse all three into a single card and concatenate the
    threads. Prod extends exactly ONE of the two and leaves the other
    completely alone -- id, range, label and replies all intact:

        {"requests": [{"deleteContentRange":
                        {"range": {"startIndex": 7, "endIndex": 26}}}],
         "writeControl": {"writeMode": "SUGGEST"}}
        -> {"suggestionResponses": [
              {"updatedSummarySuggestionIds": ["suggest.k4fbvxmszvkh"]}]}

    Read back: ``suggest.k4fbvxmszvkh`` = "Delete: bravo charlie delta"
    [7,26) carrying its own reply, ``suggest.99go7nu36559`` = "Delete: delta"
    [21,26) carrying its own. The characters in [21,26) simply hold two
    deletion ids at once.

    So there is no absorbed thread to migrate, and no absorbed id to report
    or resolve. If prod ever starts destroying a card here, this test fails
    and ``docs/findings/merge.md`` is what needs revisiting.

    **WHICH of the two absorbs is not predictable.** Five identical
    constructions run back to back gave the left-hand card three times and
    the right-hand card twice, with and without replies attached
    (docs/findings/merge.md, probe 6). So this test asserts the invariant --
    one card grows to cover the whole span, the other is untouched -- and
    never which id it was. An agent must not predict it either.
    """
    doc_id = make_scratch_doc("-merge-span", content=WORDS)
    email = ga_auth.email

    (bravo,) = _suggest(mcp, email, doc_id, start_index=7, end_index=12)[
        "created_suggestion_ids"
    ]
    (delta,) = _suggest(mcp, email, doc_id, start_index=21, end_index=26)[
        "created_suggestion_ids"
    ]
    for sid, content in ((bravo, "ALPHA-on-bravo"), (delta, "BETA-on-delta")):
        reply = tool_json(
            mcp.call_tool(
                "reply_to_doc_thread",
                {
                    "user_google_email": email,
                    "document_id": doc_id,
                    "suggestion_id": sid,
                    "reply_content": content,
                },
            )
        )
        assert reply["post_id"], reply

    spanning = _suggest(mcp, email, doc_id, start_index=7, end_index=26)
    REPORT.note(
        "deletion spanning two threaded same-author cards: "
        f"created_suggestion_ids={spanning['created_suggestion_ids']!r}"
    )
    assert spanning["created_suggestion_ids"] == [], spanning

    cards = _cards(mcp, email, doc_id, expect=2)
    assert set(cards) == {bravo, delta}, (
        "prod destroyed a pending suggestion by merging - spec §13.3's "
        f"thread question becomes live again. Cards: {list(cards)!r}"
    )

    # Exactly one card grew to cover the whole spanned range; the other kept
    # the range and label it had. Which one is nondeterministic, so both
    # assignments are legal and neither is asserted.
    grown = [
        sid for sid, r in cards.items() if (r["start_index"], r["end_index"]) == (7, 26)
    ]
    assert len(grown) == 1, (
        f"expected exactly one card covering [7,26): {[(s, r['start_index'], r['end_index']) for s, r in cards.items()]!r}"
    )
    (absorber,) = grown
    untouched = bravo if absorber == delta else delta
    REPORT.note(
        f"spanning deletion was absorbed by {'bravo' if absorber == bravo else 'delta'}"
        f" ({absorber!r}); {untouched!r} was left alone"
    )
    assert cards[absorber]["summary_text"] == "Delete: “bravo charlie delta”", cards[
        absorber
    ]
    expected = (7, 12) if untouched == bravo else (21, 26)
    assert (
        cards[untouched]["start_index"],
        cards[untouched]["end_index"],
    ) == expected, cards[untouched]
    assert cards[untouched]["summary_text"] == (
        "Delete: “bravo”" if untouched == bravo else "Delete: “delta”"
    ), cards[untouched]

    # Each thread stayed on its OWN card: nothing migrated, nothing was lost.
    assert [r["content"] for r in cards[bravo]["replies"]] == ["ALPHA-on-bravo"]
    assert [r["content"] for r in cards[delta]["replies"]] == ["BETA-on-delta"]


def test_two_cards_pushed_into_contact_do_not_collapse(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Absorption happens at CREATION time only, never retroactively.

    Two deletions one character apart (gap 1, so two cards), then a third
    deletion consuming exactly that character. It joins one of them (which one
    is nondeterministic), and the result is two cards that now TOUCH --
    ``[7,12)+[12,20)`` or ``[7,13)+[13,20)`` -- and stay two. Verified stable
    on a re-read 20 s later, so this is not an eventually-consistent view of a
    merge that has not happened yet.
    """
    doc_id = make_scratch_doc("-merge-contact", content=WORDS)
    email = ga_auth.email

    (bravo,) = _suggest(mcp, email, doc_id, start_index=7, end_index=12)[
        "created_suggestion_ids"
    ]
    (charlie,) = _suggest(mcp, email, doc_id, start_index=13, end_index=20)[
        "created_suggestion_ids"
    ]
    assert set(_cards(mcp, email, doc_id, expect=2)) == {bravo, charlie}

    # The single space at [12,13) abuts BOTH pending cards.
    fill = _suggest(mcp, email, doc_id, start_index=12, end_index=13)
    REPORT.note(
        "gap-filling deletion touching two cards: "
        f"created_suggestion_ids={fill['created_suggestion_ids']!r}"
    )
    assert fill["created_suggestion_ids"] == [], fill

    cards = _cards(mcp, email, doc_id, expect=2)
    assert set(cards) == {bravo, charlie}, cards
    ranges = sorted((r["start_index"], r["end_index"], sid) for sid, r in cards.items())
    # Adjacent, same author, same kind - and still two independent decisions.
    assert ranges[0][1] == ranges[1][0], ranges
    REPORT.note(f"touching-but-separate cards after the gap fill: {ranges!r}")
