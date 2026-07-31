"""Developer Preview e2e scenarios (marker: e2e_preview).

Tests gated on ``preview_ready`` skip - with the capabilities probe's
classification evidence in the skip message - until the credentials' GCP
project is enrolled in the Workspace Developer Preview. Several tests
double as empirical probes that RECORD real payload/error shapes into
e2e/last_run.md, resolving unknowns the plan flagged:

- the response-union extraction paths R3 guessed for the native tools
  (``replies[0].insertComment.commentThread`` and
  ``replies[0].addCommentReply.post``) - surfaced through the tools'
  ``comment_id`` / ``post_id`` / ``author`` JSON fields
- whether Docs preview thread ids interoperate with the Drive GA comment
  surface (list/update/delete/resolve)
- how many suggestion ids a SUGGEST replacement (delete+insert) yields
- the exact grammar of ``SuggestionThread.summaryText`` for each edit kind
  (the mock's §8 ``label()`` must match it - prod is the oracle)
- real error message shapes feeding preview_status.classify_preview_error
- whether the post-write verification read is immediately consistent (the
  write tools echo it inline, so a lagging read would echo nothing)
- whether prod really garbage-collects a suggestion whose last marked
  character an accept removes (SPEC §7/§11.1 I2 - ASSUMED until
  ``test_accept_can_garbage_collect_another_suggestion`` ran), and whether
  it merges adjacent same-author suggestions (SPEC §6, also assumed)
"""

from __future__ import annotations

import json

import pytest

from e2e.mcp_session import tool_json, tool_text
from e2e.run_report import REPORT
from e2e.util import poll_until

pytestmark = pytest.mark.e2e_preview

BASE_TEXT = "The quick brown fox jumps over the lazy dog."


@pytest.fixture
def base_doc(make_scratch_doc) -> str:
    """Scratch doc pre-seeded with BASE_TEXT (index 1 = 'T')."""
    return make_scratch_doc("-preview", content=BASE_TEXT)


def _suggest_insert(mcp, email: str, doc_id: str, text: str, *, index: int) -> dict:
    return tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": index,
                "text": text,
            },
        )
    )


def _list_suggestions(mcp, email: str, doc_id: str, **extra) -> dict:
    """The listing, at ``fields='full'`` unless a test says otherwise.

    The tool's own default is ``summary`` -- these tests are about the
    per-suggestion analysis (pre/post text, replies, author objects), which
    is what ``full`` carries. The default and the narrowing parameters have
    their own tests.
    """
    args = {"user_google_email": email, "document_id": doc_id, "fields": "full"}
    args.update(extra)
    return tool_json(mcp.call_tool("list_document_suggestions", args))


def _wait_for_suggestions(mcp, email: str, doc_id: str, minimum: int = 1) -> dict:
    def _check():
        listing = _list_suggestions(mcp, email, doc_id)
        return listing if listing["suggestion_count"] >= minimum else None

    return poll_until(
        _check, timeout=30, description=f"at least {minimum} suggestion(s) listed"
    )


def _create_anchored_comment(
    mcp, email: str, doc_id: str, content: str, start: int, end: int
) -> dict:
    """create_anchored_doc_comment + record-reality assertions on the
    guessed InsertCommentResponse extraction path."""
    created = tool_json(
        mcp.call_tool(
            "create_anchored_doc_comment",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "content": content,
                "start_index": start,
                "end_index": end,
            },
        )
    )
    REPORT.note(
        "create_anchored_doc_comment extraction "
        "(replies[0].insertComment.commentThread): "
        f"comment_id={created['comment_id']!r}, "
        f"post_id={created['post_id']!r}, "
        f"author={created['author']!r}, "
        f"anchor_id={created['anchor_id']!r}, "
        f"quoted_text={created['quoted_text']!r}, "
        f"comment_update_state={created['comment_update_state']!r}"
    )
    assert created["comment_id"], (
        "comment_id is null: the InsertCommentResponse union member differs "
        "from the 'insertComment.commentThread' path - fix the "
        f"extraction in gdocs_preview/write_tools.py. Full response: {created}"
    )
    # Requirement: every comment object carries an id AND an author.
    assert created["post_id"], created
    assert created["author"] and created["author"]["display_name"], created
    assert created["author"]["me"] is True, created
    return created


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_suggest_edit_creates_listable_suggestion(
    preview_ready, mcp, ga_auth, base_doc
):
    """suggest_doc_edit insertion -> list_document_suggestions pre/post +
    a REAL author, end to end."""
    response = _suggest_insert(mcp, ga_auth.email, base_doc, "very ", index=5)
    assert response["mode"] == "insertion"
    assert response["requests_applied"] == 1
    REPORT.note(
        "suggest_doc_edit(insertion) created_suggestion_ids="
        f"{response['created_suggestion_ids']!r} (empty means the API "
        "omitted the ids in suggestionResponses - recorded reality)"
    )

    # The write must be self-verifying: the echo is what stops an agent
    # having to spend a turn on list_document_suggestions (write_tools.py).
    verification = response["verification"]
    REPORT.note(f"suggest_doc_edit verification block: {verification!r}")
    assert verification["source"] == "post_write_read", verification
    assert verification["read_source"] == "preview_threads", verification
    (echo,) = verification["created_suggestions"]
    assert echo["type"] == "insertion", echo
    assert "very" in echo["post_text"] and "very" not in echo["pre_text"], echo
    assert echo["start_index"] is not None and echo["end_index"] is not None, echo
    assert echo["summary_text"], echo

    listing = _wait_for_suggestions(mcp, ga_auth.email, base_doc)
    record = listing["suggestions"][0]
    assert record["suggestion_id"]
    assert record["type"] == "insertion"
    assert "very" in record["post_text"]
    assert "very" not in record["pre_text"]
    if response["created_suggestion_ids"]:
        assert record["suggestion_id"] in response["created_suggestion_ids"]

    # The thread-bearing read (tabs + commentsViewMode) must be the one used,
    # and it must yield the real author of the suggestion we just made.
    REPORT.note(
        f"list_document_suggestions read_source={listing['read_source']!r}, "
        f"tabs={listing['tabs']!r}, author={record['author']!r}, "
        f"status={record['status']!r}, summary_text={record['summary_text']!r}"
    )
    assert listing["read_source"] == "preview_threads", (
        "the preview (tabs + threads) read degraded to the GA read: "
        f"{listing.get('degraded_reason')!r}"
    )
    assert record["author_source"] == "suggestion_thread"
    author = record["author"]
    assert author, f"author is null on an enrolled run: {record}"
    assert author["display_name"], author
    assert author["me"] is True, author
    assert (author["user"] or "").startswith("users/"), author
    assert record["status"] == "OPEN"
    assert record["create_time"]
    assert record["summary_text"], record
    # Every tab carries a real id in the preview read.
    assert record["tab_id"], record
    assert listing["tabs"] and listing["tabs"][0]["tab_id"] == record["tab_id"]


def test_summary_text_grammar_matches_the_mock_labels(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """RECORD + pin Google's ``SuggestionThread.summaryText`` grammar.

    The mock's SPEC §8 ``label()`` claims the same grammar; prod is the
    oracle, so a divergence here means mockdocs/model.py must change.
    Verified 2026-07-30: typographic quotes, and
    ``Replace: "<struck>" with "<added>"`` for a replacement.
    """
    from mockdocs.model import MockDoc

    doc_id = make_scratch_doc("-summary", content="Hello brave world.")
    email = ga_auth.email

    # Replacement at the HIGHER index first so the pending suggestions do not
    # merge: "brave" is [7, 12), "Hello" is [1, 6).
    tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": 7,
                "end_index": 12,
                "text": "bold",
            },
        )
    )
    listing = _wait_for_suggestions(mcp, email, doc_id)
    summaries = [r["summary_text"] for r in listing["suggestions"]]
    REPORT.note(f"SuggestionThread.summaryText (replacement): {summaries!r}")
    assert summaries and summaries[0] == "Replace: “brave” with “bold”", summaries

    # The mock's label() must produce the identical string.
    mock_doc = MockDoc(text="Hello brave world.")
    mock_sid = mock_doc.replace(6, 11, "bold", "alice")
    assert mock_doc.label(mock_sid)["text"] == summaries[0], (
        "mockdocs label() diverged from prod summaryText - prod is the oracle"
    )

    # Pure deletion and pure insertion, on their own documents.
    delete_doc = make_scratch_doc("-summary-del", content="Hello brave world.")
    tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": delete_doc,
                "start_index": 7,
                "end_index": 12,
            },
        )
    )
    delete_summary = _wait_for_suggestions(mcp, email, delete_doc)["suggestions"][0][
        "summary_text"
    ]
    REPORT.note(f"SuggestionThread.summaryText (deletion): {delete_summary!r}")
    assert delete_summary == "Delete: “brave”"

    insert_doc = make_scratch_doc("-summary-add", content="Hello world.")
    _suggest_insert(mcp, email, insert_doc, "Say ", index=1)
    insert_summary = _wait_for_suggestions(mcp, email, insert_doc)["suggestions"][0][
        "summary_text"
    ]
    REPORT.note(f"SuggestionThread.summaryText (insertion): {insert_summary!r}")
    assert insert_summary == "Add: “Say”"


def test_fields_filters_and_pagination_against_the_real_api(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """The narrowing parameters, against prod rather than the mock.

    The reduction they exist for was measured on mockdocs, whose records are
    modelled on the preview shapes but are not byte-identical to them. What
    prod has to confirm is not the ratio but the semantics: that summary
    keeps the thread-derived fields (author, status, summaryText) it claims
    to, that the author filter matches the display names Google actually
    returns, that an index-range filter agrees with the indexes prod
    reports, and that a page token issued by one call is honoured by the
    next against a live document.
    """
    email = ga_auth.email
    doc_id = make_scratch_doc("-page", content="Alpha beta gamma delta epsilon.")

    # Three non-adjacent suggestions, highest index first so they cannot
    # merge into each other (SPEC §6, confirmed against prod). Index 1 is the
    # first character: "Alpha" is [1, 6), "gamma" [12, 17), "epsilon" [24, 31).
    for start, end, text in ((24, 31, "OMEGA"), (12, 17, "GAMMA"), (1, 6, "ALPHA")):
        tool_json(
            mcp.call_tool(
                "suggest_doc_edit",
                {
                    "user_google_email": email,
                    "document_id": doc_id,
                    "start_index": start,
                    "end_index": end,
                    "text": text,
                },
            )
        )
    _wait_for_suggestions(mcp, email, doc_id, minimum=3)
    # No `fields` argument here: this call is the tool's own default.
    listing = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {"user_google_email": email, "document_id": doc_id},
        )
    )
    total = listing["suggestion_count"]
    assert total >= 3, listing

    # -- summary is the default, and keeps the thread-derived fields -------
    assert listing["fields"] == "summary"
    assert listing["read_source"] == "preview_threads", listing.get("degraded_reason")
    assert listing["matched_count"] == total
    assert listing["returned_count"] == total
    assert listing["page"]["has_more"] is False
    record = listing["suggestions"][0]
    REPORT.note(f"list_document_suggestions fields='summary' record: {record!r}")
    assert set(record) == {
        "suggestion_id",
        "type",
        "author",
        "summary_text",
        "segment",
        "segment_id",
        "tab_id",
        "start_index",
        "end_index",
        "status",
    }, record
    assert isinstance(record["author"], str) and record["author"], record
    assert record["status"] == "OPEN", record
    assert record["summary_text"], record
    assert record["start_index"] is not None and record["end_index"] is not None
    # The address, not decoration: prod indexes are per (tabId, segmentId).
    assert record["segment"] == "body", record
    assert record["segment_id"] is None, record
    assert record["tab_id"], record

    # -- full still carries everything summary drops ----------------------
    full = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {"user_google_email": email, "document_id": doc_id, "fields": "full"},
        )
    )
    full_record = next(
        r
        for r in full["suggestions"]
        if r["suggestion_id"] == record["suggestion_id"]
    )
    assert full_record["author"]["display_name"] == record["author"], (
        "summary flattened the author to something other than the display "
        f"name prod returned: {full_record['author']!r} vs {record['author']!r}"
    )
    assert full_record["summary_text"] == record["summary_text"]
    assert full_record["status"] == record["status"]
    assert full_record["pre_text"] is not None
    assert full_record["author_source"] == "suggestion_thread"

    # -- author filter, using the display name PROD reported ---------------
    mine = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "author": record["author"],
            },
        )
    )
    assert mine["matched_count"] == total, mine["filters"]
    assert mine["suggestion_count"] == total

    absent = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "author": "Nobody At All",
            },
        )
    )
    assert absent["matched_count"] == 0
    assert record["author"] in absent["filters"]["authors_present"]

    # -- index range, against the indexes prod itself reported -------------
    target = sorted(listing["suggestions"], key=lambda r: r["start_index"])[0]
    ranged = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": target["start_index"],
                "end_index": target["end_index"],
            },
        )
    )
    REPORT.note(
        f"index-range filter [{target['start_index']}, {target['end_index']}) "
        f"matched {ranged['matched_count']} of {ranged['suggestion_count']}"
    )
    assert target["suggestion_id"] in [
        r["suggestion_id"] for r in ranged["suggestions"]
    ], ranged
    assert ranged["matched_count"] < total, (
        "an index range covering one suggestion matched all of them"
    )

    # -- pagination round trip against a live document ---------------------
    seen: list[str] = []
    token = None
    for _ in range(total + 2):
        page = tool_json(
            mcp.call_tool(
                "list_document_suggestions",
                {
                    "user_google_email": email,
                    "document_id": doc_id,
                    "page_size": 1,
                    **({"page_token": token} if token else {}),
                },
            )
        )
        assert page["suggestion_count"] == total
        assert page["returned_count"] <= 1
        seen.extend(r["suggestion_id"] for r in page["suggestions"])
        token = page["page"]["next_page_token"]
        if not token:
            break
    assert len(seen) == total, seen
    assert len(set(seen)) == total, f"pagination repeated a suggestion: {seen}"
    assert set(seen) == {r["suggestion_id"] for r in listing["suggestions"]}

    # -- a token from another query is refused, not reinterpreted ----------
    first = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {"user_google_email": email, "document_id": doc_id, "page_size": 1},
        )
    )
    refused = mcp.call_tool_raw(
        "list_document_suggestions",
        {
            "user_google_email": email,
            "document_id": doc_id,
            "page_size": 1,
            "fields": "full",
            "page_token": first["page"]["next_page_token"],
        },
    )
    assert refused.is_error, tool_text(refused)[:400]
    assert "different query" in tool_text(refused), tool_text(refused)[:400]


def test_a_header_suggestion_is_addressable_from_the_summary_listing(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """A summary card outside the body must say so, and be writable back.

    Prod numbers each ``(tabId, segmentId)`` pair from its own start, so a
    header suggestion's ``start_index`` collides with a body suggestion's.
    ``suggest_doc_edit`` defaults to the body of the default tab: if the
    default listing did not carry ``segment_id``, an agent reading a header
    card would aim its index at the body of a customer document with nothing
    warning it. This is that loop, closed against prod: create a header,
    suggest into it, list at the default ``fields='summary'``, and write back
    using only what the card says.
    """
    email = ga_auth.email
    doc_id = make_scratch_doc("-segment", content="Body line one.")

    # Returns prose, not JSON -- the header only has to exist.
    created_header = tool_text(
        mcp.call_tool(
            "update_doc_headers_footers",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "section_type": "header",
                "content": "Header line one.",
            },
        )
    )
    assert "Error" not in created_header, created_header

    def _header_paragraph():
        view = tool_json(
            mcp.call_tool(
                "get_doc_review_view",
                {
                    "user_google_email": email,
                    "document_id": doc_id,
                    "fields": "paragraphs",
                },
            )
        )
        return next(
            (p for p in view["paragraphs"] if p["segment"] == "header"), None
        )

    header = poll_until(
        _header_paragraph, timeout=30, description="the header segment to appear"
    )
    REPORT.note(
        f"header segment from get_doc_review_view: segment_id="
        f"{header['segment_id']!r}, tab_id={header['tab_id']!r}, "
        f"[{header['start_index']}, {header['end_index']})"
    )
    assert header["segment_id"], header

    # One suggestion in the body and one in the header, so the listing has to
    # tell two cards apart that prod numbers in different coordinate spaces.
    _suggest_insert(mcp, email, doc_id, "very ", index=1)
    tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": header["start_index"] + 1,
                "text": "DRAFT ",
                "segment_id": header["segment_id"],
                **({"tab_id": header["tab_id"]} if header["tab_id"] else {}),
            },
        )
    )

    def _both():
        listing = tool_json(
            mcp.call_tool(
                "list_document_suggestions",
                {"user_google_email": email, "document_id": doc_id},
            )
        )
        segments = {r["segment"] for r in listing["suggestions"]}
        return listing if {"body", "header"} <= segments else None

    listing = poll_until(
        _both, timeout=30, description="a body card and a header card"
    )
    assert listing["fields"] == "summary"
    body_card = next(r for r in listing["suggestions"] if r["segment"] == "body")
    header_card = next(r for r in listing["suggestions"] if r["segment"] == "header")
    REPORT.note(
        f"summary cards by segment: body={body_card!r} header={header_card!r}"
    )
    assert body_card["segment_id"] is None, body_card
    assert header_card["segment_id"] == header["segment_id"], header_card

    # The card is a complete address: everything suggest_doc_edit needs to
    # aim at the same place comes out of the summary record alone.
    echo = tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": header_card["start_index"],
                "text": "X",
                "segment_id": header_card["segment_id"],
                **(
                    {"tab_id": header_card["tab_id"]}
                    if header_card["tab_id"]
                    else {}
                ),
            },
        )
    )
    assert echo["created_suggestion_ids"] or echo["verification"], echo
    after = tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {"user_google_email": email, "document_id": doc_id},
        )
    )
    header_after = [r for r in after["suggestions"] if r["segment"] == "header"]
    body_after = [r for r in after["suggestions"] if r["segment"] == "body"]
    REPORT.note(
        f"after writing back from the summary card: {len(header_after)} header "
        f"card(s), {len(body_after)} body card(s)"
    )
    assert len(body_after) == 1, (
        "writing back the header card's own indexes landed in the body: "
        f"{body_after!r}"
    )


def test_review_view_fields_and_window_against_the_real_api(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """get_doc_review_view's field modes and index window, against prod.

    The claim the default rests on is that the paragraph map's ``text``
    values concatenate to exactly ``body_text``. That is a property of the
    renderer, but it is only worth anything if prod's paragraph indexes line
    up with it -- so the window is taken off prod's own map and checked
    against prod's own text.
    """
    email = ga_auth.email
    doc_id = make_scratch_doc(
        "-view", content="First line.\nSecond line.\nThird line."
    )

    full = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {"user_google_email": email, "document_id": doc_id, "fields": "full"},
        )
    )
    body_paragraphs = [p for p in full["paragraphs"] if p["segment"] == "body"]
    assert "".join(p["text"] for p in body_paragraphs) == full["body_text"], (
        "the paragraph map and body_text disagree against prod, which is the "
        "assumption the default field mode rests on"
    )

    default = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {"user_google_email": email, "document_id": doc_id},
        )
    )
    assert default["fields"] == "text"
    assert default["body_text"] == full["body_text"]
    assert "paragraphs" not in default
    assert "paragraphs" in default["omitted_fields"]
    assert len(json.dumps(default)) < len(json.dumps(full))

    paragraphs_only = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "fields": "paragraphs",
            },
        )
    )
    assert "body_text" not in paragraphs_only
    assert "".join(
        p["text"] for p in paragraphs_only["paragraphs"] if p["segment"] == "body"
    ) == full["body_text"]

    # A window taken off prod's own paragraph map returns that paragraph.
    target = body_paragraphs[1]
    windowed = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "fields": "full",
                "start_index": target["start_index"],
                "end_index": target["end_index"],
            },
        )
    )
    REPORT.note(
        f"get_doc_review_view window [{target['start_index']}, "
        f"{target['end_index']}) -> {windowed['returned_paragraph_count']} of "
        f"{windowed['paragraph_count']} paragraph(s)"
    )
    assert windowed["paragraph_count"] == len(full["paragraphs"])
    assert windowed["returned_paragraph_count"] == 1
    assert windowed["body_text"] == target["text"]
    assert windowed["paragraphs"][0]["start_index"] == target["start_index"]


def test_review_view_exposes_comment_threads_with_authors(
    preview_ready, mcp, ga_auth, base_doc
):
    """get_doc_review_view must surface the Docs-side comment threads with
    an id and an author on every thread AND every reply."""
    args = {"user_google_email": ga_auth.email, "document_id": base_doc}
    created = _create_anchored_comment(
        mcp, ga_auth.email, base_doc, "Who wrote this?", 1, 6
    )
    reply = tool_json(
        mcp.call_tool(
            "reply_to_doc_thread",
            {**args, "reply_content": "I did.", "comment_id": created["comment_id"]},
        )
    )

    def _thread_visible():
        view = tool_json(mcp.call_tool("get_doc_review_view", dict(args)))
        for comment in view.get("comments", []):
            if comment["comment_id"] == created["comment_id"] and comment["replies"]:
                return view, comment
        return None

    view, comment = poll_until(
        _thread_visible,
        timeout=30,
        description="anchored comment + reply in get_doc_review_view",
    )
    REPORT.note(
        f"get_doc_review_view read_source={view['read_source']!r}, "
        f"comment author={comment['author']!r}, "
        f"reply author={comment['replies'][0]['author']!r}, "
        f"anchor_id={comment['anchor_id']!r}, status={comment['status']!r}"
    )
    assert view["read_source"] == "preview_threads", view.get("degraded_reason")
    assert comment["author"] and comment["author"]["display_name"]
    assert comment["post_id"]
    assert comment["anchor_id"]
    assert comment["quoted_text"] == "The q"
    assert comment["status"] == "OPEN"
    (thread_reply,) = comment["replies"]
    assert thread_reply["post_id"] == reply["post_id"]
    assert thread_reply["author"] and thread_reply["author"]["display_name"]


def test_suggest_replacement_records_id_count(preview_ready, mcp, ga_auth, base_doc):
    """Replacement = deleteContentRange + insertText in one SUGGEST batch.

    Design unknown (2026-07-14 note, D3): one or two suggestion ids?
    RECORD the reality.
    """
    # "quick" occupies [5, 10) in BASE_TEXT.
    response = tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": ga_auth.email,
                "document_id": base_doc,
                "start_index": 5,
                "end_index": 10,
                "text": "sluggish",
            },
        )
    )
    assert response["mode"] == "replacement"
    assert response["requests_applied"] == 2
    REPORT.note(
        "suggest_doc_edit(replacement) created_suggestion_ids="
        f"{response['created_suggestion_ids']!r} "
        "(design unknown D3: 1 vs 2 ids for delete+insert)"
    )

    listing = _wait_for_suggestions(mcp, ga_auth.email, base_doc)
    joined_post = " ".join(r["post_text"] for r in listing["suggestions"])
    assert "sluggish" in joined_post
    REPORT.note(
        "replacement summary_text(s): "
        f"{[r['summary_text'] for r in listing['suggestions']]!r}"
    )


def test_anchored_comment_thread_lifecycle(preview_ready, mcp, ga_auth, base_doc):
    """create_anchored_doc_comment -> Drive list -> reply_to_doc_thread ->
    Drive-GA update -> Drive-GA delete.

    UI expectation (manual, documented): the comment appears in the Docs
    editor anchored to characters 1-6 ("The q") with the quoted text
    highlighted, exactly like a human-created comment. update/delete run
    through manage_document_comment (Drive GA) - empirically verifying
    Docs-thread/Drive comment id interop; outcomes are RECORDED.
    """
    args = {"user_google_email": ga_auth.email, "document_id": base_doc}
    created = _create_anchored_comment(
        mcp, ga_auth.email, base_doc, "Anchored e2e comment", 1, 6
    )
    comment_id = created["comment_id"]
    assert created["quoted_text"] == "The q"

    # Cross-surface check: does the preview thread show up in the Drive
    # comment listing, and do the ids line up? Record the answer.
    def _in_drive_listing():
        listing = tool_text(mcp.call_tool("list_document_comments", dict(args)))
        return listing if comment_id in listing else None

    try:
        poll_until(
            _in_drive_listing, timeout=20, description="preview thread in Drive listing"
        )
        id_interop = True
        REPORT.note(
            f"preview thread {comment_id!r} IS visible via Drive "
            "list_document_comments (id-space overlap: True)"
        )
    except TimeoutError:
        id_interop = False
        REPORT.note(
            f"preview thread {comment_id!r} NOT visible via Drive "
            "list_document_comments within 20s (id-space overlap: False)"
        )

    # Reply on the comment thread (preview surface).
    reply = tool_json(
        mcp.call_tool(
            "reply_to_doc_thread",
            {**args, "reply_content": "e2e thread reply", "comment_id": comment_id},
        )
    )
    assert reply["thread_type"] == "comment"
    assert reply["comment_id"] == comment_id
    REPORT.note(
        "reply_to_doc_thread extraction (replies[0].addCommentReply.post): "
        f"post_id={reply['post_id']!r}, author={reply['author']!r}, "
        f"comment_update_state={reply['comment_update_state']!r}"
    )
    assert reply["post_id"], (
        "post_id is null: the AddCommentReplyResponse union member differs "
        "from the 'addCommentReply.post' path - fix the extraction "
        f"in gdocs_preview/write_tools.py. Full response: {reply}"
    )
    assert reply["author"] and reply["author"]["display_name"], reply

    # Update, then delete, through the Drive GA factory tool (id interop).
    update_result = mcp.call_tool_raw(
        "manage_document_comment",
        {
            **args,
            "action": "update",
            "comment_id": comment_id,
            "comment_content": "Anchored e2e comment (updated)",
        },
    )
    REPORT.note(
        "Drive comments.update on preview thread id: "
        + (
            "ERROR: " + tool_text(update_result)[:200]
            if update_result.is_error
            else "ok"
        )
    )
    delete_result = mcp.call_tool_raw(
        "manage_document_comment",
        {**args, "action": "delete", "comment_id": comment_id},
    )
    REPORT.note(
        "Drive comments.delete on preview thread id: "
        + (
            "ERROR: " + tool_text(delete_result)[:200]
            if delete_result.is_error
            else "ok"
        )
    )
    if id_interop:
        # Ids line up across surfaces - GA update/delete must work on them.
        assert not update_result.is_error, tool_text(update_result)
        assert not delete_result.is_error, tool_text(delete_result)


def test_reply_to_suggestion_thread(preview_ready, mcp, ga_auth, base_doc):
    """reply_to_doc_thread on a suggestion thread (suggestion_id union arm)."""
    _suggest_insert(mcp, ga_auth.email, base_doc, "very ", index=5)
    listing = _wait_for_suggestions(mcp, ga_auth.email, base_doc)
    suggestion_id = listing["suggestions"][0]["suggestion_id"]

    reply = tool_json(
        mcp.call_tool(
            "reply_to_doc_thread",
            {
                "user_google_email": ga_auth.email,
                "document_id": base_doc,
                "reply_content": "e2e suggestion-thread reply",
                "suggestion_id": suggestion_id,
            },
        )
    )
    assert reply["thread_type"] == "suggestion"
    assert reply["suggestion_id"] == suggestion_id
    REPORT.note(
        "reply on suggestion thread: "
        f"post_id={reply['post_id']!r}, author={reply['author']!r}, "
        f"comment_update_state={reply['comment_update_state']!r}"
    )
    assert reply["post_id"], (
        "post_id is null on a suggestion-thread reply - either the "
        "AddCommentReplyResponse union member differs from the expected "
        f"path or suggestion replies omit the post. Full response: {reply}"
    )
    assert reply["author"] and reply["author"]["display_name"], reply

    # The reply must come back on the suggestion thread, with its author.
    def _reply_listed():
        record = _list_suggestions(mcp, ga_auth.email, base_doc)["suggestions"][0]
        return record if record["replies"] else None

    record = poll_until(
        _reply_listed, timeout=30, description="suggestion-thread reply listed"
    )
    (listed,) = record["replies"]
    REPORT.note(f"list_document_suggestions suggestion-thread reply: {listed!r}")
    assert listed["post_id"] == reply["post_id"]
    assert listed["content"] == "e2e suggestion-thread reply"
    assert listed["author"] and listed["author"]["display_name"]


def test_accept_and_reject_collapse_pre_post(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Accept one suggestion, reject another; verify via the
    suggestionResponses-derived id lists and a re-read (pre/post collapse
    correctly)."""
    doc_id = make_scratch_doc("-accept-reject", content="Alpha Omega.")
    email = ga_auth.email
    # Suggest at the HIGHER index first: pending suggestions occupy index
    # space in SUGGESTIONS_INLINE coordinates, so inserting left-to-right
    # would land inside (and merge into) the earlier suggestion.
    # "Alpha Omega." -> index 12 is before ".", index 6 is after "Alpha".
    _suggest_insert(mcp, email, doc_id, " REJECTED-TOKEN", index=12)
    _suggest_insert(mcp, email, doc_id, " ACCEPTED-TOKEN", index=6)

    listing = _wait_for_suggestions(mcp, email, doc_id, minimum=2)
    by_token = {}
    for record in listing["suggestions"]:
        for token in ("ACCEPTED-TOKEN", "REJECTED-TOKEN"):
            if token in record["post_text"] and token not in record["pre_text"]:
                by_token[token] = record["suggestion_id"]
    assert set(by_token) == {"ACCEPTED-TOKEN", "REJECTED-TOKEN"}, listing

    accept = tool_json(
        mcp.call_tool(
            "manage_document_suggestion",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "action": "accept",
                "suggestion_id": by_token["ACCEPTED-TOKEN"],
            },
        )
    )
    REPORT.note(
        "manage_document_suggestion(accept) accepted_suggestion_ids="
        f"{accept['accepted_suggestion_ids']!r}, "
        f"verification={accept['verification']!r}"
    )
    if accept["accepted_suggestion_ids"]:
        assert by_token["ACCEPTED-TOKEN"] in accept["accepted_suggestion_ids"]
    # The accept verifies itself: the target is gone and the text it
    # promised is what the range now reads.
    accept_verification = accept["verification"]
    assert accept_verification["source"] == "post_write_read"
    assert accept_verification["still_pending"] is False, accept_verification
    assert "ACCEPTED-TOKEN" in accept_verification["expected_text"]
    assert accept_verification["matches_expectation"] is True, accept_verification
    # Only the sibling suggestion is left, and nothing was collaterally lost.
    assert accept_verification["pending_suggestion_ids"] == [
        by_token["REJECTED-TOKEN"]
    ], accept_verification
    assert "also_removed_suggestion_ids" not in accept_verification

    reject = tool_json(
        mcp.call_tool(
            "manage_document_suggestion",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "action": "reject",
                "suggestion_id": by_token["REJECTED-TOKEN"],
            },
        )
    )
    REPORT.note(
        "manage_document_suggestion(reject) rejected_suggestion_ids="
        f"{reject['rejected_suggestion_ids']!r}, "
        f"verification={reject['verification']!r}"
    )
    if reject["rejected_suggestion_ids"]:
        assert by_token["REJECTED-TOKEN"] in reject["rejected_suggestion_ids"]
    reject_verification = reject["verification"]
    assert reject_verification["still_pending"] is False, reject_verification
    # Rejecting an insertion expects the ORIGINAL text back, i.e. the token
    # must be gone from the range.
    assert reject_verification["expected_text"] == "", reject_verification
    assert reject_verification["matches_expectation"] is True, reject_verification
    assert reject_verification["pending_suggestion_count"] == 0, reject_verification

    def _collapsed():
        read = tool_json(
            mcp.call_tool(
                "get_doc_review_view",
                {"user_google_email": email, "document_id": doc_id},
            )
        )
        return read if read["suggestion_ids"] == [] else None

    read = poll_until(
        _collapsed, timeout=30, description="suggestions collapsed after accept/reject"
    )
    assert "ACCEPTED-TOKEN" in read["body_text"]
    assert "REJECTED-TOKEN" not in read["body_text"]


def test_accept_can_garbage_collect_another_suggestion(
    preview_ready, mcp, ga_auth, make_scratch_doc
):
    """Does prod really GC a suggestion an accept empties out? RECORD it.

    SPEC §7 + §11.1 I2 say a suggestion whose last marked character
    disappears must leave the registry, and the mock implements exactly
    that -- but until this test ran, prod agreeing was an ASSUMPTION, and
    the whole collateral-removal report in manage_document_suggestion
    rests on it.

    Construction (single account, so §6 same-author merge is the risk):
    strike "brave", then suggest an insertion INSIDE the struck run.
    Accepting the deletion deletes the struck characters, and the
    insertion has nowhere left to live. Two outcomes are legitimate and
    both are recorded:

    - prod merged the two into one suggestion (§6) -> the construction is
      unreachable for one author; the GC rule stays unconfirmed and the
      test records that instead of failing;
    - prod kept them apart -> accepting the deletion must remove the
      insertion too, AND our tool must have said so in
      ``verification.also_removed_suggestion_ids``.
    """
    doc_id = make_scratch_doc("-gc", content="Hello brave world.")
    email = ga_auth.email

    # "brave" is [7, 12) in "Hello brave world.".
    deletion = tool_json(
        mcp.call_tool(
            "suggest_doc_edit",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "start_index": 7,
                "end_index": 12,
            },
        )
    )
    assert deletion["mode"] == "deletion"
    # Index 9 sits between "br" and "ave", i.e. inside the struck run. A
    # pending deletion keeps its characters in the SUGGESTIONS_INLINE
    # coordinate space, so the index needs no shifting.
    inside = _suggest_insert(mcp, email, doc_id, "XY", index=9)
    REPORT.note(
        "GC construction: deletion ids="
        f"{deletion['created_suggestion_ids']!r}, insertion-inside-it ids="
        f"{inside['created_suggestion_ids']!r}"
    )

    listing = _wait_for_suggestions(mcp, email, doc_id)
    ids = [r["suggestion_id"] for r in listing["suggestions"]]
    summaries = [r["summary_text"] for r in listing["suggestions"]]
    REPORT.note(
        f"after both edits prod reports {len(ids)} suggestion(s): "
        f"{ids!r} / {summaries!r} -- SPEC §6 same-author merge is "
        f"{'CONFIRMED' if len(ids) == 1 else 'NOT observed'} for two "
        "overlapping same-author edits made in separate batches"
    )

    if len(ids) < 2:
        REPORT.note(
            "prod collapsed the two edits into one suggestion, so a "
            "single-account collateral-GC construction is unreachable: "
            "SPEC §11.1 I2 remains ASSUMED against prod (the mock "
            "implements it and the unit tests cover our reporting of it)."
        )
        # A merged edit gets NO created id from the API, so the echo must
        # fall back to the suggestion now covering the edited range -- the
        # write must never come back with nothing to verify against.
        assert inside["created_suggestion_ids"] == [], inside
        merged = inside["verification"]
        REPORT.note(f"merged-edit verification block: {merged!r}")
        assert merged["created_suggestions"] == [], merged
        (echo,) = merged["suggestions_at_edit_range"]
        assert echo["suggestion_id"] == ids[0], (echo, ids)
        # The merged card means "replace 'brave' with 'XY'" -- which is what
        # the echo has to say, since the agent asked for neither.
        assert echo["pre_text"] == "brave", echo
        assert echo["post_text"] == "XY", echo
        assert "merges into it" in merged["notes"][0]
        return

    deletion_id = next(
        r["suggestion_id"] for r in listing["suggestions"] if not r["post_text"]
    )
    other_ids = [sid for sid in ids if sid != deletion_id]

    accept = tool_json(
        mcp.call_tool(
            "manage_document_suggestion",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "action": "accept",
                "suggestion_id": deletion_id,
            },
        )
    )
    verification = accept["verification"]
    survivors = verification["pending_suggestion_ids"]
    collateral = verification.get("also_removed_suggestion_ids", [])
    REPORT.note(
        f"accepting the deletion {deletion_id!r} left {survivors!r} pending; "
        f"the tool reported also_removed={collateral!r}. SPEC §11.1 I2 "
        f"(accept GCs a suggestion it empties) is "
        f"{'CONFIRMED against prod' if collateral else 'NOT observed'}."
    )

    gone = [sid for sid in other_ids if sid not in survivors]
    # Whatever prod did, the tool must have REPORTED it: every id that
    # disappeared alongside our accept has to be named, or the agent is
    # back to discovering it as an unexplained error later.
    assert sorted(collateral) == sorted(gone), (
        "collateral detection disagrees with the post-write listing: "
        f"reported {collateral!r}, actually gone {gone!r}"
    )
    if collateral:
        (note,) = verification["notes"]
        assert deletion_id in note and collateral[0] in note, note


# ---------------------------------------------------------------------------
# Sad paths - each records the REAL error shape for the probe classifier
# ---------------------------------------------------------------------------


def _record_and_classify(label: str, error_text: str) -> None:
    """Record a real preview error shape + assert classifier agreement.

    All these errors come from semantically-invalid PREVIEW requests made
    by ENROLLED credentials, so the classifier must call them 'available'
    (a 400 whose message is NOT an unknown-field parse failure). If this
    fails, reality diverged from the chunk-3 patterns - fix
    gdocs_preview/preview_status.py, which is in scope for the e2e chunk.
    """
    from gdocs_preview.preview_status import classify_preview_error

    status = 400 if "400" in error_text else None
    REPORT.record_error_shape(label, status, error_text)
    if status == 400:
        availability, reason = classify_preview_error(status, error_text)
        assert availability == "available", (
            f"{label}: enrolled semantic-400 misclassified as {availability!r} "
            f"({reason}); real message: {error_text[:300]!r}"
        )


def test_double_accept_same_suggestion(preview_ready, mcp, ga_auth, base_doc):
    _suggest_insert(mcp, ga_auth.email, base_doc, " DOUBLE-ACCEPT", index=5)
    listing = _wait_for_suggestions(mcp, ga_auth.email, base_doc)
    suggestion_id = listing["suggestions"][0]["suggestion_id"]
    args = {
        "user_google_email": ga_auth.email,
        "document_id": base_doc,
        "action": "accept",
        "suggestion_id": suggestion_id,
    }
    tool_json(mcp.call_tool("manage_document_suggestion", dict(args)))

    second = mcp.call_tool_raw("manage_document_suggestion", dict(args))
    text = tool_text(second)
    if second.is_error:
        _record_and_classify("double-accept same suggestion", text)
        # The id is gone because WE removed it, and the error must say so
        # rather than leaving "does not exist" to look like a typo.
        assert "You accepted it yourself" in text, text
        assert "list_document_suggestions" in text, text
    else:
        # Preview docs: thread/suggestion updates may no-op with a
        # commentUpdateState instead of erroring.
        response = tool_json(second)
        REPORT.record_error_shape(
            "double-accept same suggestion (non-error)",
            200,
            f"accepted_suggestion_ids={response['accepted_suggestion_ids']!r}, "
            f"comment_update_state={response.get('comment_update_state')!r}",
        )
        assert response["suggestion_id"] == suggestion_id


def test_accept_nonexistent_suggestion_id(preview_ready, mcp, ga_auth, scratch_doc):
    """Feeds the probe classifier the enrolled semantic-400 shape.

    The design note documents that a nonexistent id may surface as a 400
    error OR as an HTTP 200 no-op - both branches are recorded.
    """
    result = mcp.call_tool_raw(
        "manage_document_suggestion",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "action": "accept",
            "suggestion_id": "e2e-nonexistent-suggestion-id",
        },
    )
    text = tool_text(result)
    if result.is_error:
        assert "400" in text or "404" in text, text
        _record_and_classify("accept nonexistent suggestion id", text)
    else:
        response = tool_json(result)
        REPORT.record_error_shape(
            "accept nonexistent suggestion id (non-error)",
            200,
            f"accepted_suggestion_ids={response['accepted_suggestion_ids']!r}, "
            f"comment_update_state={response.get('comment_update_state')!r}",
        )
        assert response["accepted_suggestion_ids"] == []


def test_reply_to_resolved_thread(preview_ready, mcp, ga_auth, base_doc):
    args = {"user_google_email": ga_auth.email, "document_id": base_doc}
    created = _create_anchored_comment(mcp, ga_auth.email, base_doc, "resolve me", 1, 4)
    comment_id = created["comment_id"]

    # Resolve through the Drive surface (GA path a human reviewer uses).
    resolve = mcp.call_tool_raw(
        "manage_document_comment",
        {**args, "action": "resolve", "comment_id": comment_id},
    )
    REPORT.note(
        "resolve preview thread via manage_document_comment(action=resolve): "
        + ("ERROR: " + tool_text(resolve)[:200] if resolve.is_error else "ok")
    )
    if resolve.is_error:
        pytest.skip(
            "preview thread id could not be resolved through the Drive GA "
            "surface - interop outcome recorded in the run report."
        )

    after = mcp.call_tool_raw(
        "reply_to_doc_thread",
        {**args, "reply_content": "reply after resolve", "comment_id": comment_id},
    )
    text = tool_text(after)
    if after.is_error:
        _record_and_classify("reply to resolved thread", text)
    else:
        response = tool_json(after)
        REPORT.record_error_shape(
            "reply to resolved thread (non-error)",
            200,
            f"post_id={response['post_id']!r}, "
            f"comment_update_state={response['comment_update_state']!r}",
        )


def test_reply_to_deleted_thread(preview_ready, mcp, ga_auth, base_doc):
    args = {"user_google_email": ga_auth.email, "document_id": base_doc}
    created = _create_anchored_comment(mcp, ga_auth.email, base_doc, "delete me", 1, 4)
    comment_id = created["comment_id"]

    delete = mcp.call_tool_raw(
        "manage_document_comment",
        {**args, "action": "delete", "comment_id": comment_id},
    )
    REPORT.note(
        "delete preview thread via manage_document_comment(action=delete): "
        + ("ERROR: " + tool_text(delete)[:200] if delete.is_error else "ok")
    )
    if delete.is_error:
        pytest.skip(
            "preview thread id could not be deleted through the Drive GA "
            "surface - interop outcome recorded in the run report."
        )

    after = mcp.call_tool_raw(
        "reply_to_doc_thread",
        {**args, "reply_content": "reply after delete", "comment_id": comment_id},
    )
    text = tool_text(after)
    if after.is_error:
        _record_and_classify("reply to deleted thread", text)
    else:
        response = tool_json(after)
        REPORT.record_error_shape(
            "reply to deleted thread (non-error)",
            200,
            f"post_id={response['post_id']!r}, "
            f"comment_update_state={response['comment_update_state']!r}",
        )
        # A deleted thread must not silently accept new posts.
        assert response["comment_update_state"], response


# NOTE: the old generated-surface probe for a SUGGEST-incompatible request
# type (createNamedRange + writeMode=SUGGEST via the raw batchUpdate tool)
# is DROPPED: suggest_doc_edit only ever emits insertText and
# deleteContentRange - both SUGGEST-compatible - so the shape is
# unreachable through the native surface
# (docs/plans/2026-07-14-native-integration.md section 5). Likewise the raw
# partial-failure batch probe (insertComment + bogus deleteComment): no raw
# batchUpdate tool remains, and single-request commentUpdateState
# enforcement lives in the write tools' shared helper (unit-tested).


def test_suggest_doc_edit_validation_sad_paths(mcp, ga_auth, scratch_doc):
    """Blackbox UserInputError shapes of the native suggest tool.

    Deliberately NOT gated on preview_ready: validation rejects before
    any API call, so these must hold for any token, enrolled or not.
    """
    args = {"user_google_email": ga_auth.email, "document_id": scratch_doc}

    error_text = mcp.expect_tool_error("suggest_doc_edit", {**args, "start_index": 5})
    assert (
        "Provide text (insertion), end_index (deletion), or both (replacement)."
        in error_text
    )

    error_text = mcp.expect_tool_error(
        "suggest_doc_edit", {**args, "start_index": 5, "end_index": 5, "text": "x"}
    )
    assert "must be greater than start_index" in error_text

    error_text = mcp.expect_tool_error(
        "suggest_doc_edit", {**args, "start_index": 0, "text": "x"}
    )
    assert "start_index must be >= 1" in error_text
