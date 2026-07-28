"""Developer Preview e2e scenarios (marker: e2e_preview).

The whole block skips - with the capabilities probe's classification
evidence in the skip message - until the credentials' GCP project is
enrolled in the Workspace Developer Preview. Several tests double as
empirical probes that RECORD real payload/error shapes into
e2e/last_run.md, resolving unknowns the plan flagged:

- WHERE suggestion/comment threads surface in documents.get
- response-union member names (InsertCommentResponse / AddCommentReplyResponse)
- real error message shapes feeding preview_status.classify_preview_error
"""

from __future__ import annotations

import json

import pytest

from e2e.mcp_session import tool_json, tool_text
from e2e.run_report import REPORT
from e2e.util import find_key_paths, poll_until

pytestmark = pytest.mark.e2e_preview

BASE_TEXT = "The quick brown fox jumps over the lazy dog."


def _insert_base_text(mcp, email: str, doc_id: str, text: str = BASE_TEXT) -> None:
    tool_json(
        mcp.call_tool(
            "docs_api_insert_text",
            {
                "user_google_email": email,
                "document_id": doc_id,
                "location": {"index": 1},
                "text": text,
            },
        )
    )


def _suggest_insert(
    mcp, email: str, doc_id: str, text: str, *, index: int | None = None
):
    args = {
        "user_google_email": email,
        "document_id": doc_id,
        "text": text,
        "write_mode": "SUGGEST",
    }
    if index is None:
        args["end_of_segment_location"] = {}
    else:
        args["location"] = {"index": index}
    return tool_json(mcp.call_tool("docs_api_insert_text", args))


def _list_suggestions(mcp, email: str, doc_id: str) -> dict:
    return tool_json(
        mcp.call_tool(
            "docs_review_list_suggestions",
            {"user_google_email": email, "document_id": doc_id},
        )
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_suggest_write_creates_listable_suggestion(
    preview_ready, mcp, ga_auth, scratch_doc
):
    """SUGGEST-mode write -> docs_review_list_suggestions pre/post + author.

    Also records WHERE suggestion threads surface in documents.get
    (plan unknown): the suggested run text is guaranteed to carry
    suggestedInsertionIds in body content; thread objects' location is
    captured empirically below.
    """
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    response = _suggest_insert(mcp, ga_auth.email, scratch_doc, "very ", index=5)
    # BatchUpdateDocumentResponse.suggestionResponses is the documented
    # carrier of affected-suggestion info for SUGGEST writes - record it.
    REPORT.note(
        "SUGGEST insertText response keys: "
        + ", ".join(sorted(response.keys()))
        + (
            f"; suggestionResponses={json.dumps(response['suggestionResponses'])}"
            if "suggestionResponses" in response
            else "; suggestionResponses ABSENT"
        )
    )

    def _has_suggestion():
        listing = _list_suggestions(mcp, ga_auth.email, scratch_doc)
        return listing if listing["suggestion_count"] >= 1 else None

    listing = poll_until(
        _has_suggestion, timeout=30, description="suggestion visible in list"
    )
    record = listing["suggestions"][0]
    assert record["suggestion_id"]
    assert record["type"] == "insertion"
    assert "very" in record["post_text"]
    assert "very" not in record["pre_text"]
    # Preview exposes Post.author on SuggestionThread.headPost (chunk 2
    # finding); enrolled runs must surface a non-null author.
    assert "author" in record
    REPORT.note(
        f"list_suggestions author field (enrolled): {record['author']!r} "
        f"(source: {record.get('author_source')!r})"
    )

    # Empirically record WHERE thread objects live in documents.get.
    document = tool_json(
        mcp.call_tool(
            "docs_api_documents_get",
            {
                "user_google_email": ga_auth.email,
                "document_id": scratch_doc,
                "suggestions_view_mode": "SUGGESTIONS_INLINE",
            },
        )
    )
    raw = json.dumps(document)
    assert "suggestedInsertionIds" in raw
    thread_paths = find_key_paths(document, ("thread", "comment"))
    REPORT.note(
        "documents.get(SUGGESTIONS_INLINE) thread/comment key paths: "
        + (", ".join(sorted(set(thread_paths))[:20]) or "NONE FOUND")
    )


def test_anchored_comment_thread_lifecycle(preview_ready, mcp, ga_auth, scratch_doc):
    """insertComment(range) -> list -> reply -> update -> delete reply/comment.

    UI expectation (manual, documented): the comment appears in the Docs
    editor anchored to characters 1-6 ("The q") with the quoted text
    highlighted, exactly like a human-created comment.
    """
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    args = {"user_google_email": ga_auth.email, "document_id": scratch_doc}

    response = tool_json(
        mcp.call_tool(
            "docs_api_insert_comment",
            {
                **args,
                "content": "Anchored e2e comment",
                "range": {"startIndex": 1, "endIndex": 6},
            },
        )
    )
    replies = response.get("replies") or []
    assert replies, f"insertComment returned no replies[]: {response}"
    # Plan unknown: the response-union member name. Record reality.
    REPORT.note(
        "insertComment reply union member keys: " + ", ".join(sorted(replies[0].keys()))
    )
    thread = replies[0].get("insertComment", {}).get("commentThread")
    assert thread, (
        "InsertCommentResponse union member differs from overlay "
        f"('insertComment'->commentThread expected): {replies[0]}"
    )
    comment_id = thread["commentId"]
    assert comment_id
    head_post = thread["headPost"]
    assert head_post.get("author"), f"headPost carries no author: {head_post}"
    assert thread.get("plainTextQuote") == "The q"

    # Cross-surface check: does the preview thread show up in Drive
    # comments.list, and do the ids line up? Record the answer.
    drive_listing = tool_json(
        mcp.call_tool(
            "drive_api_comments_list",
            {"user_google_email": ga_auth.email, "file_id": scratch_doc},
        )
    )
    drive_ids = [c["id"] for c in drive_listing.get("comments", [])]
    REPORT.note(
        f"preview thread commentId={comment_id!r}; Drive comments.list "
        f"ids={drive_ids!r} (id-space overlap: {comment_id in drive_ids})"
    )

    # Reply to the thread.
    reply_response = tool_json(
        mcp.call_tool(
            "docs_api_add_comment_reply",
            {
                **args,
                "comment_id": comment_id,
                "post": {"content": "e2e thread reply"},
            },
        )
    )
    reply_replies = reply_response.get("replies") or []
    assert reply_replies, f"addCommentReply returned no replies[]: {reply_response}"
    REPORT.note(
        "addCommentReply reply union member keys: "
        + ", ".join(sorted(reply_replies[0].keys()))
    )
    reply_post = reply_replies[0].get("addCommentReply", {}).get("post")
    assert reply_post, f"AddCommentReplyResponse shape differs: {reply_replies[0]}"
    reply_post_id = reply_post["postId"]
    assert reply_post_id
    assert reply_post.get("author"), f"reply post carries no author: {reply_post}"

    # Update the head post's content.
    update_response = tool_json(
        mcp.call_tool(
            "docs_api_update_comment_post",
            {
                **args,
                "comment_id": comment_id,
                "post_id": head_post.get("postId"),
                "content": "Anchored e2e comment (updated)",
            },
        )
    )
    assert update_response.get("commentUpdateState") != "FAILED", update_response

    # Delete the reply, then the whole comment thread.
    tool_json(
        mcp.call_tool(
            "docs_api_delete_comment_reply",
            {**args, "comment_id": comment_id, "post_id": reply_post_id},
        )
    )
    tool_json(
        mcp.call_tool("docs_api_delete_comment", {**args, "comment_id": comment_id})
    )


def test_accept_and_reject_collapse_pre_post(preview_ready, mcp, ga_auth, scratch_doc):
    """Accept one suggestion, reject another; verify via suggestionResponses
    and a re-read (pre/post collapse correctly)."""
    _insert_base_text(mcp, ga_auth.email, scratch_doc, "Alpha Omega.")
    _suggest_insert(mcp, ga_auth.email, scratch_doc, " ACCEPTED-TOKEN")
    _suggest_insert(mcp, ga_auth.email, scratch_doc, " REJECTED-TOKEN")

    def _both_listed():
        listing = _list_suggestions(mcp, ga_auth.email, scratch_doc)
        return listing if listing["suggestion_count"] >= 2 else None

    listing = poll_until(
        _both_listed, timeout=30, description="both suggestions listed"
    )
    by_token = {}
    for record in listing["suggestions"]:
        for token in ("ACCEPTED-TOKEN", "REJECTED-TOKEN"):
            if token in record["post_text"] and token not in record["pre_text"]:
                by_token[token] = record["suggestion_id"]
    assert set(by_token) == {"ACCEPTED-TOKEN", "REJECTED-TOKEN"}, listing

    accept_response = tool_json(
        mcp.call_tool(
            "docs_api_accept_suggestion",
            {
                "user_google_email": ga_auth.email,
                "document_id": scratch_doc,
                "suggestion_id": by_token["ACCEPTED-TOKEN"],
            },
        )
    )
    REPORT.note("acceptSuggestion response keys: " + ", ".join(sorted(accept_response)))
    reject_response = tool_json(
        mcp.call_tool(
            "docs_api_reject_suggestion",
            {
                "user_google_email": ga_auth.email,
                "document_id": scratch_doc,
                "suggestion_id": by_token["REJECTED-TOKEN"],
            },
        )
    )
    assert reject_response.get("commentUpdateState") != "FAILED", reject_response

    def _collapsed():
        read = tool_json(
            mcp.call_tool(
                "docs_review_read_document",
                {
                    "user_google_email": ga_auth.email,
                    "document_id": scratch_doc,
                },
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
    gdocs_preview/preview_status.py, which is in scope for this chunk.
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


def test_double_accept_same_suggestion(preview_ready, mcp, ga_auth, scratch_doc):
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    _suggest_insert(mcp, ga_auth.email, scratch_doc, " DOUBLE-ACCEPT")

    def _listed():
        listing = _list_suggestions(mcp, ga_auth.email, scratch_doc)
        return listing if listing["suggestion_count"] >= 1 else None

    listing = poll_until(_listed, timeout=30, description="suggestion listed")
    suggestion_id = listing["suggestions"][0]["suggestion_id"]
    args = {
        "user_google_email": ga_auth.email,
        "document_id": scratch_doc,
        "suggestion_id": suggestion_id,
    }
    tool_json(mcp.call_tool("docs_api_accept_suggestion", dict(args)))

    second = mcp.call_tool_raw("docs_api_accept_suggestion", dict(args))
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
            f"commentUpdateState={response.get('commentUpdateState')!r}",
        )
        assert "commentUpdateState" in response, response


def test_accept_nonexistent_suggestion_id(preview_ready, mcp, ga_auth, scratch_doc):
    """Feeds the probe classifier the enrolled semantic-400 shape."""
    error_text = mcp.expect_tool_error(
        "docs_api_accept_suggestion",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "suggestion_id": "e2e-nonexistent-suggestion-id",
        },
    )
    assert "400" in error_text or "404" in error_text, error_text
    _record_and_classify("accept nonexistent suggestion id", error_text)


def test_reply_to_resolved_thread(preview_ready, mcp, ga_auth, scratch_doc):
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    args = {"user_google_email": ga_auth.email, "document_id": scratch_doc}
    response = tool_json(
        mcp.call_tool(
            "docs_api_insert_comment",
            {
                **args,
                "content": "resolve me",
                "range": {"startIndex": 1, "endIndex": 4},
            },
        )
    )
    thread = response["replies"][0]["insertComment"]["commentThread"]
    comment_id = thread["commentId"]

    # Resolve through the Drive surface (GA path a human reviewer uses).
    resolve = mcp.call_tool_raw(
        "drive_api_replies_create",
        {
            "user_google_email": ga_auth.email,
            "file_id": scratch_doc,
            "comment_id": comment_id,
            "body": {"action": "resolve"},
        },
    )
    REPORT.note(
        "resolve preview thread via Drive replies.create(action=resolve): "
        + ("ERROR: " + tool_text(resolve) if resolve.is_error else "ok")
    )

    after = mcp.call_tool_raw(
        "docs_api_add_comment_reply",
        {**args, "comment_id": comment_id, "post": {"content": "reply after resolve"}},
    )
    text = tool_text(after)
    if after.is_error:
        _record_and_classify("reply to resolved thread", text)
    else:
        response = tool_json(after)
        REPORT.record_error_shape(
            "reply to resolved thread (non-error)",
            200,
            f"commentUpdateState={response.get('commentUpdateState')!r}",
        )


def test_reply_to_deleted_thread(preview_ready, mcp, ga_auth, scratch_doc):
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    args = {"user_google_email": ga_auth.email, "document_id": scratch_doc}
    response = tool_json(
        mcp.call_tool(
            "docs_api_insert_comment",
            {**args, "content": "delete me", "range": {"startIndex": 1, "endIndex": 4}},
        )
    )
    thread = response["replies"][0]["insertComment"]["commentThread"]
    comment_id = thread["commentId"]
    tool_json(
        mcp.call_tool("docs_api_delete_comment", {**args, "comment_id": comment_id})
    )

    after = mcp.call_tool_raw(
        "docs_api_add_comment_reply",
        {**args, "comment_id": comment_id, "post": {"content": "reply after delete"}},
    )
    text = tool_text(after)
    if after.is_error:
        _record_and_classify("reply to deleted thread", text)
    else:
        response = tool_json(after)
        REPORT.record_error_shape(
            "reply to deleted thread (non-error)",
            200,
            f"commentUpdateState={response.get('commentUpdateState')!r}",
        )
        # A deleted thread must not silently accept new posts.
        assert response.get("commentUpdateState"), response


def test_suggest_mode_with_unsupported_request_type(
    preview_ready, mcp, ga_auth, scratch_doc
):
    """createNamedRange is not SUGGEST-compatible (not in the 32 write_mode
    members) - the API documents an error for suggesting such requests."""
    error_text = mcp.expect_tool_error(
        "docs_api_documents_batch_update",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "body": {
                "requests": [
                    {
                        "createNamedRange": {
                            "name": "e2e-unsupported-suggest",
                            "range": {"startIndex": 1, "endIndex": 2},
                        }
                    }
                ],
                "writeControl": {"writeMode": "SUGGEST"},
            },
        },
    )
    assert "400" in error_text, error_text
    REPORT.record_error_shape(
        "SUGGEST write_mode with unsupported request type (createNamedRange)",
        400,
        error_text,
    )


def test_comment_update_state_on_partial_failure_batch(
    preview_ready, mcp, ga_auth, scratch_doc
):
    """Batch mixing a valid insertComment with a bogus deleteComment:
    observe commentUpdateState (preview docs say thread updates can
    partially fail while the batch succeeds)."""
    _insert_base_text(mcp, ga_auth.email, scratch_doc)
    result = mcp.call_tool_raw(
        "docs_api_documents_batch_update",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "body": {
                "requests": [
                    {
                        "insertComment": {
                            "content": "partial-failure batch comment",
                            "range": {"startIndex": 1, "endIndex": 4},
                        }
                    },
                    {"deleteComment": {"commentId": "e2e-bogus-comment-id"}},
                ]
            },
        },
    )
    text = tool_text(result)
    if result.is_error:
        REPORT.record_error_shape(
            "partial-failure batch (insertComment + bogus deleteComment)",
            400 if "400" in text else None,
            text,
        )
        assert "400" in text or "404" in text, text
    else:
        response = tool_json(result)
        REPORT.record_error_shape(
            "partial-failure batch (non-error)",
            200,
            f"commentUpdateState={response.get('commentUpdateState')!r}, "
            f"replies={json.dumps(response.get('replies'))[:200]}",
        )
        assert "commentUpdateState" in response, (
            "expected commentUpdateState on a partially-failing comment batch: "
            f"{response}"
        )
