"""What the review tools do with suggestion kinds the analysis layer does
not model (marker: e2e_preview).

``gdocs_preview/analysis.py`` extracts suggestions by walking a document's
CONTENT MARKS -- ``suggestedInsertionIds`` / ``suggestedDeletionIds`` /
``suggestedTextStyleChanges`` on a paragraph element. A note in its docstring
called two whole families "out of scope by design": table row/column
structure and paragraph-level style. Neither half of that sentence survived
contact with the API (``docs/findings/coverage.md``, measured 2026-08-02):

- Table row and column insert/delete ARE reported, because the affected
  cells' text runs carry the ordinary insertion/deletion marks. There was
  never anything to be out of scope about.
- Paragraph style IS silently dropped -- but only when it changes nothing
  about the runs. ``HEADING_2`` drags a ``suggestedTextStyleChanges`` along
  and so came through; alignment, line spacing and indent leave the runs
  untouched and vanished entirely, as do ``createParagraphBullets``,
  ``updateTableRowStyle`` and ``updateTableCellStyle``.

A vanished card was not merely undescribed: it was uncounted, so
``list_document_suggestions`` answered ``suggestion_count: 0`` about a
document with an open suggestion in it, and ``get_doc_review_view`` returned
an empty ``suggestion_ids`` beside prose containing no marker. Both are
complete-looking answers that are not complete, which is the one failure this
package exists to prevent. These tests pin the fix -- an
``unreported_suggestion_count`` derived from the API's OWN pending-thread
inventory -- against prod rather than against a fixture.

**Why the suggestions are created with a harness-side Docs client.** No tool
on this MCP surface can make one: ``suggest_doc_edit`` writes text only, and
upstream's ``batch_update_doc`` / ``update_paragraph_style`` write in EDIT
mode with no ``writeControl``. The fixture state is therefore built the way
scratch-doc teardown is (``conftest.harness_drive``) -- directly, because it
is not the thing under test. What is under test is what the tools SAY about
a document in that state.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from e2e.conftest import create_doc_via_mcp, new_scratch_title
from e2e.mcp_session import tool_json
from e2e.run_report import REPORT

pytestmark = pytest.mark.e2e_preview

SEED_TEXT = "Alpha line one.\nBravo line two.\nCharlie line three.\n"


@pytest.fixture(scope="session")
def harness_docs(ga_auth):
    """Direct Docs v1 client, for building fixture state only.

    Not the MCP surface, and deliberately: the surface cannot express a
    SUGGEST-mode ``updateParagraphStyle`` at all (see the module docstring),
    and a test that cannot create the state it is about tests nothing.
    """
    service = build("docs", "v1", credentials=ga_auth.credentials)
    yield service
    service.close()


@pytest.fixture(scope="module")
def seeded_doc(mcp, ga_auth, doc_tracker, preview_probe):
    """Factory for MODULE-scoped scratch docs, tracked and trashed as usual.

    ``make_scratch_doc`` is function-scoped, which would give this file a
    document per test. The Docs API allows 60 write requests per minute per
    user and every doc here costs three to five, so the tests below share one
    document per fixture STATE instead of one per assertion; the only test
    that mutates a document takes its own via ``make_scratch_doc``.

    Registration goes through the session ``doc_tracker`` exactly as
    ``make_scratch_doc`` does, so teardown and ``test_zz_teardown_audit``
    see these documents like any other.

    Gated on ``preview_probe`` rather than ``preview_ready`` because a
    module-scoped fixture cannot depend on a function-scoped one, and
    building the state needs SUGGEST mode: without enrollment this would
    error where the tests themselves would have skipped.
    """
    if preview_probe["preview"].get("availability") != "available":
        pytest.skip(
            "E2E PREVIEW SKIPPED - Developer Preview not available for these "
            "credentials; SUGGEST-mode fixture state cannot be built."
        )
    created: list[str] = []

    def factory(suffix: str) -> str:
        title = new_scratch_title(suffix)
        doc_id = create_doc_via_mcp(mcp, ga_auth.email, title)
        doc_tracker.register(doc_id, title)
        created.append(doc_id)
        return doc_id

    yield factory
    for doc_id in created:
        doc_tracker.cleanup(doc_id)


def _batch(docs, doc_id: str, requests: list[dict], *, suggest: bool) -> dict:
    """One batchUpdate, retrying the per-minute write quota.

    The Docs API allows 60 write requests per minute per user and this file
    seeds a document per test, so a full-suite run brushes the limit. A 429 is
    the quota refilling, not a failure of anything under test.
    """
    body: dict[str, Any] = {"requests": requests}
    if suggest:
        body["writeControl"] = {"writeMode": "SUGGEST"}
    deadline = time.monotonic() + 120
    while True:
        try:
            return docs.documents().batchUpdate(documentId=doc_id, body=body).execute()
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status != 429 or time.monotonic() >= deadline:
                raise
            time.sleep(15)


def _seed_text(docs, doc_id: str) -> None:
    _batch(
        docs,
        doc_id,
        [{"insertText": {"location": {"index": 1}, "text": SEED_TEXT}}],
        suggest=False,
    )


def _seed_table(docs, doc_id: str) -> int:
    """Append a 2x2 table; return its ``startIndex``."""
    ga = docs.documents().get(documentId=doc_id).execute()
    end = ga["body"]["content"][-1]["endIndex"]
    _batch(
        docs,
        doc_id,
        [{"insertTable": {"rows": 2, "columns": 2, "location": {"index": end - 1}}}],
        suggest=False,
    )
    ga = docs.documents().get(documentId=doc_id).execute()
    return next(el for el in ga["body"]["content"] if "table" in el)["startIndex"]


def _suggest_alignment(docs, doc_id: str) -> None:
    """The measured minimal case: a paragraph style change that touches no
    run, and therefore leaves the document content completely unmarked."""
    _batch(
        docs,
        doc_id,
        [
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 5},
                    "paragraphStyle": {"alignment": "CENTER"},
                    "fields": "alignment",
                }
            }
        ],
        suggest=True,
    )


def _suggest_table_row(docs, doc_id: str, request: str) -> None:
    table_start = _seed_table(docs, doc_id)
    _batch(
        docs,
        doc_id,
        [
            {
                request: {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": table_start},
                        "rowIndex": 0,
                        "columnIndex": 0,
                    },
                    **({"insertBelow": True} if request == "insertTableRow" else {}),
                }
            }
        ],
        suggest=True,
    )


# ---------------------------------------------------------------------------
# Fixture states, one document each, shared by the read-only tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alignment_doc(seeded_doc, harness_docs) -> str:
    """Exactly ONE pending suggestion, and it marks no content at all."""
    doc_id = seeded_doc("-coverage-align")
    _seed_text(harness_docs, doc_id)
    _suggest_alignment(harness_docs, doc_id)
    return doc_id


@pytest.fixture(scope="module")
def mixed_doc(seeded_doc, harness_docs) -> str:
    """One text suggestion and one invisible one: the realistic review state,
    and the exact shape of the bug -- the text card used to be the whole
    answer."""
    doc_id = seeded_doc("-coverage-mixed")
    _seed_text(harness_docs, doc_id)
    _batch(
        harness_docs,
        doc_id,
        [{"insertText": {"location": {"index": 6}, "text": "NEW "}}],
        suggest=True,
    )
    _suggest_alignment(harness_docs, doc_id)
    return doc_id


@pytest.fixture(scope="module")
def inserted_row_doc(seeded_doc, harness_docs) -> str:
    doc_id = seeded_doc("-coverage-row")
    _seed_text(harness_docs, doc_id)
    _suggest_table_row(harness_docs, doc_id, "insertTableRow")
    return doc_id


@pytest.fixture(scope="module")
def deleted_row_doc(seeded_doc, harness_docs) -> str:
    """Its own document: an insert and a delete on the same row are adjacent
    same-author edits, and prod merges those into one card
    (``summaryText: "Delete row\\nAdd row"``, measured 2026-08-02), which
    would conflate the two cases under test."""
    doc_id = seeded_doc("-coverage-delrow")
    _seed_text(harness_docs, doc_id)
    _suggest_table_row(harness_docs, doc_id, "deleteTableRow")
    return doc_id


def _listing(mcp, email: str, doc_id: str, **extra) -> dict:
    args = {"user_google_email": email, "document_id": doc_id}
    args.update(extra)
    return tool_json(mcp.call_tool("list_document_suggestions", args))


def _review_view(mcp, email: str, doc_id: str, **extra) -> dict:
    args = {"user_google_email": email, "document_id": doc_id}
    args.update(extra)
    return tool_json(mcp.call_tool("get_doc_review_view", args))


# ---------------------------------------------------------------------------
# Q1: paragraph-level style
# ---------------------------------------------------------------------------


class TestAPendingSuggestionWithNoContentMark:
    """An alignment suggestion exists, is OPEN, and marks nothing."""

    def test_the_listing_counts_it_and_names_its_kind(
        self, mcp, ga_auth, preview_ready, alignment_doc
    ):
        listing = _listing(mcp, ga_auth.email, alignment_doc)

        # The premise: the analysis layer really cannot see this card. If
        # Google ever starts marking the runs too, this assertion is where
        # that shows up rather than in a silently changed count.
        assert listing["suggestion_count"] == 0, listing
        assert listing["suggestions"] == [], listing

        # ...and the tool says so anyway, from the API's own thread array.
        assert listing["unreported_suggestion_count"] == 1, listing
        (card,) = listing["unreported_suggestions"]
        assert card["suggestion_id"].startswith("suggest."), card
        assert card["status"] == "OPEN", card
        assert card["author"], card
        # Google's own label is the only place the KIND is named; nothing in
        # the document content says what this suggestion does.
        assert card["summary_text"].startswith("Format"), card
        assert card["summary_text"] in listing["notice_unreported"]
        assert "Do NOT report a review as complete" in listing["notice_unreported"]
        REPORT.note(
            "updateParagraphStyle(alignment) in SUGGEST mode: pending OPEN "
            f"thread {card['suggestion_id']} with summaryText "
            f"{card['summary_text']!r}, and NO content mark -- "
            "suggestion_count 0, unreported_suggestion_count 1."
        )

    def test_the_review_view_counts_it_too(
        self, mcp, ga_auth, preview_ready, alignment_doc
    ):
        """``suggestion_ids`` is this tool's version of the same claim, and
        it was equally short: the card renders no CriticMarkup anywhere."""
        view = _review_view(mcp, ga_auth.email, alignment_doc)

        assert view["read_source"] == "preview_threads", view
        assert view["suggestion_ids"] == [], view
        assert "{+" not in view["body_text"] and "{-" not in view["body_text"]
        assert view["unreported_suggestion_count"] == 1, view
        assert view["unreported_suggestions"][0]["status"] == "OPEN"

    def test_a_modelled_card_beside_it_is_still_described_normally(
        self, mcp, ga_auth, preview_ready, mixed_doc
    ):
        """Two pending suggestions, one of which used to be the whole answer."""
        listing = _listing(mcp, ga_auth.email, mixed_doc, fields="full")

        assert listing["suggestion_count"] == 1, listing
        (described,) = listing["suggestions"]
        assert described["type"] == "insertion"
        assert "NEW" in described["post_text"]
        assert listing["unreported_suggestion_count"] == 1, listing
        assert (
            listing["unreported_suggestions"][0]["suggestion_id"]
            != described["suggestion_id"]
        )


class TestResolvingAnUnmodelledSuggestion:
    """The thread survives a reject; the count must not.

    Measured 2026-08-02: ``rejectSuggestion`` strips every content mark but
    leaves the thread in ``suggestions[]`` with ``status: "REJECTED"``. So the
    thread array is the document's whole suggestion HISTORY, not its pending
    set, and a count that subtracted it raw would grow monotonically and never
    reach zero. This is the test that would catch that.
    """

    def test_rejecting_it_by_id_works_and_clears_the_count(
        self, mcp, ga_auth, preview_ready, make_scratch_doc, harness_docs
    ):
        doc_id = make_scratch_doc("-coverage-reject")
        _seed_text(harness_docs, doc_id)
        _suggest_alignment(harness_docs, doc_id)

        before = _listing(mcp, ga_auth.email, doc_id)
        assert before["unreported_suggestion_count"] == 1, before
        suggestion_id = before["unreported_suggestions"][0]["suggestion_id"]
        # The notice promises these ids are actionable. They are.
        assert "manage_document_suggestion" in before["notice_unreported"]

        resolved = tool_json(
            mcp.call_tool(
                "manage_document_suggestion",
                {
                    "user_google_email": ga_auth.email,
                    "document_id": doc_id,
                    "action": "reject",
                    "suggestion_id": suggestion_id,
                },
            )
        )
        assert suggestion_id in json_ids(resolved), resolved

        after = _listing(mcp, ga_auth.email, doc_id)
        assert after["suggestion_count"] == 0, after
        assert after["unreported_suggestion_count"] == 0, after
        assert "unreported_suggestions" not in after, after
        REPORT.note(
            "reject of an unmodelled (paragraph-style) suggestion: HTTP 200, "
            "content mark gone, thread retained with status REJECTED -- so "
            "unreported_suggestion_count returns to 0 only because resolved "
            "threads are filtered out."
        )


def json_ids(resolved: dict) -> list[str]:
    """Every suggestion id the resolution response mentions."""
    ids = list(resolved.get("rejected_suggestion_ids") or [])
    ids += list(resolved.get("accepted_suggestion_ids") or [])
    ids += list(resolved.get("resolved_suggestion_ids") or [])
    return ids


# ---------------------------------------------------------------------------
# Q2: table structure
# ---------------------------------------------------------------------------


class TestTableStructureSuggestions:
    """The half of the "out of scope" note that was simply wrong.

    ``insertTableRow`` marks the new row's cell text runs with
    ``suggestedInsertionIds`` (and the ``tableRow`` element itself, which this
    package never needed to read); ``deleteTableRow`` / ``deleteTableColumn``
    mark the cells' ``suggestedDeletionIds``. Each is therefore an ordinary
    record with an address and a table flag.
    """

    def test_an_inserted_row_is_reported_as_an_ordinary_suggestion(
        self, mcp, ga_auth, preview_ready, inserted_row_doc
    ):
        listing = _listing(mcp, ga_auth.email, inserted_row_doc, fields="full")

        assert listing["suggestion_count"] == 1, listing
        (card,) = listing["suggestions"]
        # ``mixed``, not ``insertion``: measured 2026-08-02, prod marks the
        # new row's empty cell runs with a ``suggestedTextStyleChanges``
        # (baselineOffset) as well as the ``suggestedInsertionIds``, so the
        # record carries both kinds. Asserted rather than smoothed over --
        # this is the API's answer, not the one that was expected.
        assert card["type"] == "mixed", card
        assert card["in_table"] is True, card
        assert card["summary_text"] == "Add row", card
        # It has a real address, so it can be filtered and written against.
        assert card["segment"] == "body"
        assert card["start_index"] is not None and card["end_index"] is not None
        # And nothing is left over for the unreported block to report.
        assert listing["unreported_suggestion_count"] == 0, listing
        REPORT.note(
            "insertTableRow in SUGGEST mode: reported as an ordinary record "
            f"(type {card['type']!r}, summaryText {card['summary_text']!r}, "
            "in_table=true). The 'table structure is out of scope' note was "
            "wrong; the type is 'mixed' because the new cells' runs carry a "
            "text-style change alongside the insertion mark."
        )

    def test_a_deleted_row_is_reported_too(
        self, mcp, ga_auth, preview_ready, deleted_row_doc
    ):
        listing = _listing(mcp, ga_auth.email, deleted_row_doc, fields="full")

        assert listing["suggestion_count"] == 1, listing
        (card,) = listing["suggestions"]
        assert card["type"] == "deletion", card
        assert card["in_table"] is True, card
        assert card["summary_text"] == "Delete row", card
        assert listing["unreported_suggestion_count"] == 0, listing


# ---------------------------------------------------------------------------
# The count is only ever claimed by a read that could check it
# ---------------------------------------------------------------------------


class TestTheCountIsNeverClaimedByABlindRead:
    """``0`` is an absence claim, and the thread array it is derived from
    exists only on the Developer Preview read."""

    def test_a_degraded_read_says_it_cannot_tell(
        self, mcp, degraded_read_mcp, ga_auth, preview_ready, alignment_doc
    ):
        # The healthy server can see it...
        healthy = _listing(mcp, ga_auth.email, alignment_doc)
        assert healthy["unreported_suggestion_count"] == 1, healthy

        # ...and the one whose preview read is broken says so, rather than
        # reporting the 0 its empty thread map would otherwise produce.
        blind = _listing(degraded_read_mcp, ga_auth.email, alignment_doc)
        assert blind["read_source"] == "ga_documents_get", blind
        assert blind["unreported_suggestion_count"] is None, blind
        assert blind["unreported_suggestions_unavailable"] == "read_degraded", blind
        assert "never looked" in blind["notice_unreported"]

    def test_a_preview_view_mode_cannot_read_threads_at_all(
        self, mcp, ga_auth, preview_ready, alignment_doc
    ):
        """A finding in its own right, measured 2026-08-02: the API refuses

            400 "Comments may not be requested when previewing suggestions."

        for BOTH ``PREVIEW_SUGGESTIONS_ACCEPTED`` and
        ``PREVIEW_WITHOUT_SUGGESTIONS`` when ``commentsViewMode`` is set --
        which ``preview_read`` always sets. So those two view modes always
        fall back to the GA read, and everything thread-derived, this count
        included, is unavailable there. The response says so instead of
        answering 0.
        """
        view = _review_view(
            mcp, ga_auth.email, alignment_doc, view_mode="PREVIEW_SUGGESTIONS_ACCEPTED"
        )

        assert view["read_source"] == "ga_documents_get", view
        assert "Comments may not be requested" in view["degraded_reason"], view
        assert view["unreported_suggestion_count"] is None, view
        assert view["unreported_suggestions_unavailable"] == "read_degraded", view
        REPORT.note(
            "documents.get with suggestionsViewMode=PREVIEW_SUGGESTIONS_ACCEPTED "
            "and commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED is a 400 "
            '("Comments may not be requested when previewing suggestions"), so '
            "both PREVIEW_* view modes ALWAYS degrade to the GA read."
        )
