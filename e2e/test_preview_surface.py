"""Developer Preview e2e scenarios (marker: e2e_preview).

Tests gated on ``preview_ready`` skip - with the capabilities probe's
classification evidence in the skip message - until the credentials' GCP
project is enrolled in the Workspace Developer Preview. Several tests
double as empirical probes that RECORD real payload/error shapes into
e2e/last_run.md, resolving unknowns the plan flagged:

- the response-union extraction paths R3 guessed for the native tools
  (``replies[0].insertComment.commentThread`` and
  ``replies[0].addCommentReply.post``) - surfaced through the tools'
  ``comment_id`` / ``post_id`` JSON fields
- whether Docs preview thread ids interoperate with the Drive GA comment
  surface (list/update/delete/resolve)
- how many suggestion ids a SUGGEST replacement (delete+insert) yields
- real error message shapes feeding preview_status.classify_preview_error
"""

from __future__ import annotations

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


def _list_suggestions(mcp, email: str, doc_id: str) -> dict:
    return tool_json(
        mcp.call_tool(
            "list_document_suggestions",
            {"user_google_email": email, "document_id": doc_id},
        )
    )


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
        "create_anchored_doc_comment extraction (guessed path "
        "replies[0].insertComment.commentThread): "
        f"comment_id={created['comment_id']!r}, "
        f"anchor_id={created['anchor_id']!r}, "
        f"quoted_text={created['quoted_text']!r}, "
        f"comment_update_state={created['comment_update_state']!r}"
    )
    assert created["comment_id"], (
        "comment_id is null: the InsertCommentResponse union member differs "
        "from the guessed 'insertComment.commentThread' path - fix the "
        f"extraction in gdocs_preview/write_tools.py. Full response: {created}"
    )
    return created


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_suggest_edit_creates_listable_suggestion(
    preview_ready, mcp, ga_auth, base_doc
):
    """suggest_doc_edit insertion -> list_document_suggestions pre/post +
    author."""
    response = _suggest_insert(mcp, ga_auth.email, base_doc, "very ", index=5)
    assert response["mode"] == "insertion"
    assert response["requests_applied"] == 1
    REPORT.note(
        "suggest_doc_edit(insertion) created_suggestion_ids="
        f"{response['created_suggestion_ids']!r} (empty means the API "
        "omitted the ids in suggestionResponses - recorded reality)"
    )

    listing = _wait_for_suggestions(mcp, ga_auth.email, base_doc)
    record = listing["suggestions"][0]
    assert record["suggestion_id"]
    assert record["type"] == "insertion"
    assert "very" in record["post_text"]
    assert "very" not in record["pre_text"]
    if response["created_suggestion_ids"]:
        assert record["suggestion_id"] in response["created_suggestion_ids"]
    # Preview exposes Post.author on SuggestionThread.headPost (chunk 2
    # finding); enrolled runs record what actually surfaces.
    assert "author" in record
    REPORT.note(
        f"list_document_suggestions author field (enrolled): {record['author']!r} "
        f"(source: {record.get('author_source')!r})"
    )


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
        "reply_to_doc_thread extraction (guessed path "
        "replies[0].addCommentReply.post.postId): "
        f"post_id={reply['post_id']!r}, "
        f"comment_update_state={reply['comment_update_state']!r}"
    )
    assert reply["post_id"], (
        "post_id is null: the AddCommentReplyResponse union member differs "
        "from the guessed 'addCommentReply.post' path - fix the extraction "
        f"in gdocs_preview/write_tools.py. Full response: {reply}"
    )

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
        f"post_id={reply['post_id']!r}, "
        f"comment_update_state={reply['comment_update_state']!r}"
    )
    assert reply["post_id"], (
        "post_id is null on a suggestion-thread reply - either the "
        "AddCommentReplyResponse union member differs from the guessed "
        f"path or suggestion replies omit the post. Full response: {reply}"
    )


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
        f"{accept['accepted_suggestion_ids']!r}"
    )
    if accept["accepted_suggestion_ids"]:
        assert by_token["ACCEPTED-TOKEN"] in accept["accepted_suggestion_ids"]

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
        f"{reject['rejected_suggestion_ids']!r}"
    )
    if reject["rejected_suggestion_ids"]:
        assert by_token["REJECTED-TOKEN"] in reject["rejected_suggestion_ids"]

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
