"""SUGGEST-mode batchUpdate semantics, pinned against the real Docs API.

Two questions were carried as "transcribed, never verified" for the whole
life of this fork (``docs/preview-api-reference.md`` open item 5;
``gdocs_preview/write_tools.py`` replacement path). Both are answered here,
by the API rather than by a doc page, and the answers are recorded verbatim
in ``docs/findings/suggest-semantics.md``.

1. **Which request types a SUGGEST batch refuses.** The 8 types on Google's
   published "unsupported in suggest mode" list really are refused --
   ``400 INVALID_ARGUMENT: Invalid requests[i].<type>: Request does not
   support application as suggestion.`` The 8 **preview thread operations**
   (``insertComment``, ``addCommentReply``, ``updateCommentPost``,
   ``deleteComment``, ``deleteCommentReply``, ``acceptSuggestion``,
   ``rejectSuggestion``, ``deleteSuggestion``) are **not** refused: they run
   normally inside a SUGGEST batch. ``mockdocs`` used to reject them, on an
   overlay guess; it no longer does.

2. **How a multi-request SUGGEST batch resolves indexes.** Progressively --
   each request is addressed against the document as the earlier requests in
   the same batch left it, exactly like EDIT mode. The twist is the space it
   progresses in: a *suggested* deletion leaves its text in place (marked),
   so it shifts nothing, while a *suggested* insertion is present in the
   ``SUGGESTIONS_INLINE`` space immediately and shifts everything after it.

These tests talk to ``documents.batchUpdate`` directly rather than through
the MCP surface, because the subject under test is the Google API itself --
no MCP tool sends an ``addDocumentTab`` or a bare two-insert batch. Scratch
docs still come from ``make_scratch_doc``, so teardown and the audit are
unchanged.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from e2e.run_report import REPORT

pytestmark = pytest.mark.e2e_preview

SEED = "0123456789"

#: Prod's verbatim refusal for a request type that SUGGEST mode does not
#: support. The whole point of this module is that the message is specific:
#: a request rejected for being malformed says something else entirely
#: ("The provided table start location is invalid."), so a test asserting
#: only "400" would pass on a request that was never valid to begin with.
REFUSAL = "Request does not support application as suggestion."

#: Google's published list (developers.google.com/workspace/docs/api/how-tos/
#: suggestions), mirrored in ``mockdocs.adapter.SUGGEST_UNSUPPORTED_OFFICIAL``.
OFFICIALLY_UNSUPPORTED = (
    "addDocumentTab",
    "createNamedRange",
    "deleteFooter",
    "deleteHeader",
    "deleteNamedRange",
    "deleteTab",
    "updateDocumentTabProperties",
    "updateTableColumnProperties",
)


@pytest.fixture(scope="module")
def harness_docs(ga_auth):
    """Direct Docs v1 client. Harness-side, like ``harness_drive``.

    Not the MCP surface on purpose: these tests assert what the *API* does
    with request types and batch shapes the tools deliberately never emit.
    """
    service = build("docs", "v1", credentials=ga_auth.credentials)
    yield service
    service.close()


# ---------------------------------------------------------------------------
# Raw-API helpers
# ---------------------------------------------------------------------------


def _execute(request, *, attempts: int = 6) -> dict:
    """Execute a googleapiclient request, backing off on HTTP 429.

    Not synchronisation -- the suite never sleeps to wait for a write to
    become visible (``e2e/util.poll_until`` does that). This is the Docs
    write quota, ``WriteRequestsPerMinutePerUser``, whose limit is 60 and
    which this module is dense enough to reach on its own.
    """
    delay = 5.0
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status != 429 or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 40.0)
    raise AssertionError("unreachable")


def _batch(docs, doc_id: str, requests: list[dict], write_mode: str | None = None):
    body: dict[str, Any] = {"requests": requests}
    if write_mode is not None:
        body["writeControl"] = {"writeMode": write_mode}
    return _execute(docs.documents().batchUpdate(documentId=doc_id, body=body))


def _batch_fails(
    docs, doc_id: str, requests: list[dict], write_mode: str | None = None
) -> tuple[int | None, str]:
    """Run a batch that must fail; return (http status, API message)."""
    try:
        response = _batch(docs, doc_id, requests, write_mode)
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        content = getattr(error, "content", b"") or b""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        return status, content
    pytest.fail(f"expected a failure, got HTTP 200: {response!r}")


def _text(docs, doc_id: str, view: str = "SUGGESTIONS_INLINE") -> str:
    document = (
        _execute(docs.documents().get(documentId=doc_id, suggestionsViewMode=view))
        or {}
    )
    parts = []
    for element in document.get("body", {}).get("content", []):
        for run in (element.get("paragraph") or {}).get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


def _tab_ids(docs, doc_id: str) -> list[str]:
    document = _execute(
        docs.documents().get(documentId=doc_id, includeTabsContent=True)
    )

    def walk(tabs):
        found = []
        for tab in tabs or []:
            found.append(tab["tabProperties"]["tabId"])
            found.extend(walk(tab.get("childTabs")))
        return found

    return walk(document.get("tabs"))


@pytest.fixture
def seeded_doc(make_scratch_doc, harness_docs) -> str:
    """Scratch doc whose body is exactly ``SEED`` -- index 1 is ``'0'``.

    Seeded through the raw API rather than ``create_doc(content=...)`` so the
    body is the seed and nothing else: every index in this module is counted
    off that string.
    """
    doc_id = make_scratch_doc("-suggest-semantics")
    _batch(
        harness_docs, doc_id, [{"insertText": {"location": {"index": 1}, "text": SEED}}]
    )
    return doc_id


# ---------------------------------------------------------------------------
# Q1 -- what a SUGGEST batch refuses
# ---------------------------------------------------------------------------


def test_the_eight_official_request_types_are_refused_in_suggest_mode(
    preview_ready, harness_docs, seeded_doc
):
    """Each of Google's 8 is refused in SUGGEST mode **and accepted in EDIT
    mode on the same document, in the same state**.

    The EDIT leg is what makes this evidence rather than noise: a request
    that fails because its own arguments are wrong fails in both modes, and
    proves nothing about SUGGEST. Only the SUGGEST leg is allowed to fail,
    and only with :data:`REFUSAL`.
    """
    docs, doc_id = harness_docs, seeded_doc

    # State that makes the requests otherwise VALID. The table goes at the
    # end of the body so it never moves the seed's indexes; the second tab is
    # created by the addDocumentTab case itself, so everything before that
    # case runs against a single-tab document and no request has to guess
    # which tab it meant.
    header_id = _batch(docs, doc_id, [{"createHeader": {"type": "DEFAULT"}}])[
        "replies"
    ][0]["createHeader"]["headerId"]
    footer_id = _batch(docs, doc_id, [{"createFooter": {"type": "DEFAULT"}}])[
        "replies"
    ][0]["createFooter"]["footerId"]
    named_range_id = _batch(
        docs,
        doc_id,
        [
            {
                "createNamedRange": {
                    "name": "e2e_nr",
                    "range": {"startIndex": 1, "endIndex": 6},
                }
            }
        ],
    )["replies"][0]["createNamedRange"]["namedRangeId"]
    _batch(
        docs,
        doc_id,
        [{"insertTable": {"endOfSegmentLocation": {}, "rows": 2, "columns": 2}}],
    )
    default_tab_id = _tab_ids(docs, doc_id)[0]

    # Positive control: SUGGEST mode works on THIS document, right now. A
    # test where every SUGGEST batch fails cannot tell "this request type is
    # refused" from "SUGGEST is unavailable to this caller".
    control = _batch(
        docs,
        doc_id,
        [{"insertText": {"location": {"index": 1}, "text": "zz"}}],
        "SUGGEST",
    )
    assert control["suggestionResponses"][0]["createdSuggestionIds"]

    def table_start() -> int:
        # Read AFTER the control: a batchUpdate index is a SUGGESTIONS_INLINE
        # index, and the control's suggested "zz" occupies two of them. A
        # tableStartLocation computed before it is off by two and the request
        # fails as invalid -- in BOTH modes, proving nothing.
        return next(
            element["startIndex"]
            for element in _execute(docs.documents().get(documentId=doc_id))["body"][
                "content"
            ]
            if "table" in element
        )

    state: dict[str, str] = {}

    def after_add_document_tab() -> None:
        state["extra_tab_id"] = next(
            t for t in _tab_ids(docs, doc_id) if t != default_tab_id
        )

    # (kind, build request, callback after the EDIT leg). Ordered so each
    # case's precondition still holds when it runs: the deletes destroy what
    # made them valid, and addDocumentTab is what creates deleteTab's tab.
    cases: list[tuple[str, Any, Any]] = [
        (
            "createNamedRange",
            lambda: {
                "createNamedRange": {
                    "name": "e2e_nr_2",
                    "range": {"startIndex": 1, "endIndex": 4},
                }
            },
            None,
        ),
        (
            "updateDocumentTabProperties",
            lambda: {
                "updateDocumentTabProperties": {
                    "tabProperties": {"tabId": default_tab_id, "title": "e2e-renamed"},
                    "fields": "title",
                }
            },
            None,
        ),
        (
            "updateTableColumnProperties",
            lambda: {
                "updateTableColumnProperties": {
                    "tableStartLocation": {"index": table_start()},
                    "columnIndices": [0],
                    "tableColumnProperties": {
                        "widthType": "FIXED_WIDTH",
                        "width": {"magnitude": 120, "unit": "PT"},
                    },
                    "fields": "width,widthType",
                }
            },
            None,
        ),
        (
            "deleteNamedRange",
            lambda: {"deleteNamedRange": {"namedRangeId": named_range_id}},
            None,
        ),
        ("deleteHeader", lambda: {"deleteHeader": {"headerId": header_id}}, None),
        ("deleteFooter", lambda: {"deleteFooter": {"footerId": footer_id}}, None),
        (
            "addDocumentTab",
            lambda: {"addDocumentTab": {"tabProperties": {"title": "e2e-tab"}}},
            after_add_document_tab,
        ),
        ("deleteTab", lambda: {"deleteTab": {"tabId": state["extra_tab_id"]}}, None),
    ]
    assert {kind for kind, _, _ in cases} == set(OFFICIALLY_UNSUPPORTED)

    messages = {}
    for kind, make_request, after_edit in cases:
        request = make_request()
        status, message = _batch_fails(docs, doc_id, [request], "SUGGEST")
        messages[kind] = message
        assert status == 400, f"{kind}: expected 400, got {status}: {message}"
        assert REFUSAL in message, (
            f"{kind} failed in SUGGEST mode, but not for being unsupported "
            f"there: {message}"
        )
        assert f"requests[0].{kind}" in message, f"{kind}: {message}"

        # The same request, same document, same state, in EDIT mode. An
        # HttpError here means the request was never valid and the SUGGEST
        # leg above was measuring something else.
        _batch(docs, doc_id, [request], "EDIT")
        if after_edit is not None:
            after_edit()

    REPORT.record_error_shape(
        "batchUpdate writeMode=SUGGEST, officially-unsupported request types "
        f"({', '.join(sorted(messages))})",
        400,
        messages["addDocumentTab"],
        classification="suggest_mode_unsupported_request_type",
    )


def test_every_preview_thread_operation_runs_inside_a_suggest_batch(
    preview_ready, harness_docs, seeded_doc
):
    """All 8 preview thread ops are accepted -- and take effect -- in a
    SUGGEST-mode batch.

    This retires ``docs/preview-api-reference.md`` open item 5. The overlay
    treated them as SUGGEST-incompatible ("they act on threads, not
    content"); the API disagrees. Each op below is asserted on its EFFECT,
    not merely on HTTP 200: the three deletes are proven by the follow-up
    404, and accept/reject/delete-suggestion by their id echoes.
    """
    docs, doc_id = harness_docs, seeded_doc

    def suggest_batch(requests):
        return _batch(docs, doc_id, requests, "SUGGEST")

    def new_suggestion(text: str) -> str:
        response = suggest_batch(
            [{"insertText": {"location": {"index": 1}, "text": text}}]
        )
        return response["suggestionResponses"][0]["createdSuggestionIds"][0]

    # insertComment, in a SUGGEST batch, yields a REAL comment thread.
    thread = suggest_batch(
        [
            {
                "insertComment": {
                    "content": "suggest-mode comment",
                    "range": {"startIndex": 2, "endIndex": 6},
                }
            }
        ]
    )["replies"][0]["insertComment"]["commentThread"]
    comment_id = thread["commentId"]
    head_post_id = thread["headPost"]["postId"]
    assert thread["status"] == "OPEN"
    assert thread["headPost"]["content"] == "suggest-mode comment"
    assert thread["headPost"]["author"]["displayName"]
    assert thread["plainTextQuote"]

    # addCommentReply
    reply = suggest_batch(
        [{"addCommentReply": {"commentId": comment_id, "post": {"content": "reply"}}}]
    )["replies"][0]["addCommentReply"]["post"]
    assert reply["content"] == "reply"
    reply_post_id = reply["postId"]

    # updateCommentPost
    updated = suggest_batch(
        [
            {
                "updateCommentPost": {
                    "commentId": comment_id,
                    "postId": head_post_id,
                    "content": "edited in suggest mode",
                }
            }
        ]
    )
    assert updated["commentUpdateState"] == "ALL_SAVED"

    # acceptSuggestion / rejectSuggestion / deleteSuggestion
    accepted_id = new_suggestion("AA")
    assert suggest_batch([{"acceptSuggestion": {"suggestionId": accepted_id}}])[
        "suggestionResponses"
    ][0]["acceptedSuggestionIds"] == [accepted_id]
    assert "AA" in _text(docs, doc_id, "PREVIEW_WITHOUT_SUGGESTIONS"), (
        "acceptSuggestion inside a SUGGEST batch returned the id but did not "
        "apply the suggestion to the base text"
    )

    rejected_id = new_suggestion("RR")
    assert suggest_batch([{"rejectSuggestion": {"suggestionId": rejected_id}}])[
        "suggestionResponses"
    ][0]["rejectedSuggestionIds"] == [rejected_id]

    deleted_id = new_suggestion("DD")
    assert suggest_batch([{"deleteSuggestion": {"suggestionId": deleted_id}}])[
        "suggestionResponses"
    ][0]["deletedSuggestionIds"] == [deleted_id]
    status, message = _batch_fails(
        docs, doc_id, [{"deleteSuggestion": {"suggestionId": deleted_id}}], "EDIT"
    )
    assert status == 404 and deleted_id in message

    # deleteCommentReply, then deleteComment -- both proven by the 404 after.
    assert (
        suggest_batch(
            [{"deleteCommentReply": {"commentId": comment_id, "postId": reply_post_id}}]
        )["commentUpdateState"]
        == "ALL_SAVED"
    )
    status, message = _batch_fails(
        docs,
        doc_id,
        [{"deleteCommentReply": {"commentId": comment_id, "postId": reply_post_id}}],
        "EDIT",
    )
    assert status == 404, message

    assert (
        suggest_batch([{"deleteComment": {"commentId": comment_id}}])[
            "commentUpdateState"
        ]
        == "ALL_SAVED"
    )
    status, message = _batch_fails(
        docs, doc_id, [{"deleteComment": {"commentId": comment_id}}], "EDIT"
    )
    assert status == 404 and comment_id in message

    REPORT.note(
        "all 8 preview thread operations accepted in writeMode=SUGGEST "
        "(insertComment, addCommentReply, updateCommentPost, deleteComment, "
        "deleteCommentReply, acceptSuggestion, rejectSuggestion, "
        "deleteSuggestion) -- overlay exclusion was wrong"
    )


def test_a_suggest_batch_mixes_a_content_edit_with_a_thread_operation(
    preview_ready, harness_docs, seeded_doc
):
    """One SUGGEST batch, one suggestion and one comment thread out of it.

    ``suggestionResponses`` stays 1:1 with the requests (the comment's slot
    is ``{}``) and ``commentUpdateState`` reports on the thread half, exactly
    as it does in EDIT mode.
    """
    response = _batch(
        harness_docs,
        seeded_doc,
        [
            {"insertText": {"location": {"index": 1}, "text": "MIX"}},
            {
                "insertComment": {
                    "content": "mixed-batch comment",
                    "range": {"startIndex": 2, "endIndex": 5},
                }
            },
        ],
        "SUGGEST",
    )
    assert len(response["suggestionResponses"]) == 2
    assert response["suggestionResponses"][0]["createdSuggestionIds"]
    assert response["suggestionResponses"][1] == {}
    assert response["replies"][0] == {}
    assert response["replies"][1]["insertComment"]["commentThread"]["commentId"]
    assert response["commentUpdateState"] == "ALL_SAVED"


# ---------------------------------------------------------------------------
# Q2 -- how a SUGGEST batch resolves indexes
# ---------------------------------------------------------------------------


def test_suggest_batches_resolve_indexes_progressively_like_edit_batches(
    preview_ready, harness_docs, make_scratch_doc
):
    """Insert ``AAAA`` at 1, then ``B`` at 5, in one batch.

    Progressive resolution puts ``B`` immediately after ``AAAA``
    (``AAAAB0123456789``); pre-batch resolution would put it at the seed's
    own index 5, before ``'4'`` (``AAAA0123B456789``). Both modes give the
    first answer, so **SUGGEST and EDIT agree**, and the agreement is what
    lets a caller reason about one batch the way it reasons about the other.
    """
    requests = [
        {"insertText": {"location": {"index": 1}, "text": "AAAA"}},
        {"insertText": {"location": {"index": 5}, "text": "B"}},
    ]
    progressive = "AAAAB0123456789\n"
    pre_batch = "AAAA0123B456789\n"

    outcomes = {}
    for mode in ("SUGGEST", "EDIT"):
        doc_id = make_scratch_doc(f"-index-{mode.lower()}")
        _batch(
            harness_docs,
            doc_id,
            [{"insertText": {"location": {"index": 1}, "text": SEED}}],
        )
        _batch(harness_docs, doc_id, requests, mode)
        outcomes[mode] = _text(harness_docs, doc_id)

    assert outcomes["SUGGEST"] == progressive, (
        f"SUGGEST resolved request 1 against {outcomes['SUGGEST']!r}; "
        f"pre-batch resolution would have given {pre_batch!r}"
    )
    assert outcomes["EDIT"] == progressive
    REPORT.note(
        "index resolution in a 2-request batch (insert@1 'AAAA', insert@5 'B') "
        f"over {SEED!r}: SUGGEST -> {outcomes['SUGGEST']!r}, "
        f"EDIT -> {outcomes['EDIT']!r} (both progressive, not pre-batch)"
    )


def test_a_suggested_deletion_shifts_nothing_but_an_edit_deletion_does(
    preview_ready, harness_docs, make_scratch_doc
):
    """Delete [1, 5), then insert ``Z`` at 6, in one batch.

    This is where the two modes part company, and it is not a disagreement
    about *when* indexes are resolved -- both are progressive -- but about
    what the earlier request did to the document:

    - SUGGEST marks ``0123`` deleted and leaves the characters in the
      ``SUGGESTIONS_INLINE`` space, so index 6 is still ``'5'``:
      ``01234Z56789``.
    - EDIT removes them, so index 6 lands five characters earlier, in
      ``456789``: ``45678Z9``.
    """
    requests = [
        {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}},
        {"insertText": {"location": {"index": 6}, "text": "Z"}},
    ]
    outcomes = {}
    for mode in ("SUGGEST", "EDIT"):
        doc_id = make_scratch_doc(f"-shift-{mode.lower()}")
        _batch(
            harness_docs,
            doc_id,
            [{"insertText": {"location": {"index": 1}, "text": SEED}}],
        )
        _batch(harness_docs, doc_id, requests, mode)
        outcomes[mode] = _text(harness_docs, doc_id)

    assert outcomes["SUGGEST"] == "01234Z56789\n"
    assert outcomes["EDIT"] == "45678Z9\n"


def test_the_replacement_shape_suggest_doc_edit_sends_lands_at_start_index(
    preview_ready, harness_docs, seeded_doc
):
    """``suggest_doc_edit``'s replacement is ``deleteContentRange[s, e)``
    then ``insertText@s`` in one SUGGEST batch, and it is correct.

    The reason is the previous test's, not the comment the code used to
    carry: the suggested deletion does not shift the inline space, so the
    insertion's ``s`` still means ``s``. Under EDIT semantics the same two
    requests happen to agree, which is why the shape was never caught being
    justified by the wrong rule.
    """
    docs, doc_id = harness_docs, seeded_doc
    response = _batch(
        docs,
        doc_id,
        [
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}},
            {"insertText": {"location": {"index": 1}, "text": "X"}},
        ],
        "SUGGEST",
    )
    # One suggestion, reported twice: created by request 0, summary updated
    # by request 1 (HANDOVER Section 4.5).
    created = response["suggestionResponses"][0]["createdSuggestionIds"]
    assert len(created) == 1
    assert response["suggestionResponses"][1]["updatedSummarySuggestionIds"] == created

    assert _text(docs, doc_id, "SUGGESTIONS_INLINE") == "X0123456789\n"
    assert _text(docs, doc_id, "PREVIEW_WITHOUT_SUGGESTIONS") == f"{SEED}\n"
    assert _text(docs, doc_id, "PREVIEW_SUGGESTIONS_ACCEPTED") == "X456789\n"
