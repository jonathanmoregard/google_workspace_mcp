"""GA-surface e2e scenarios: real Google APIs through the real MCP server.

Everything here needs only an OAuth token (marker: e2e_ga) - no
Developer Preview enrollment. Under test: the upstream GA docs service
(create_doc via the scratch-doc fixtures, modify_doc_text,
list_document_comments, manage_document_comment - including the update
and delete actions added with the native-integration redirect) plus the
docs_preview read/diagnostic tools (get_doc_review_view,
check_docs_review_capabilities).
"""

from __future__ import annotations

import re

import pytest

from e2e.mcp_session import tool_json, tool_text
from e2e.run_report import REPORT
from e2e.util import poll_until

pytestmark = pytest.mark.e2e_ga

VIEW_MODES = (
    "SUGGESTIONS_INLINE",
    "PREVIEW_SUGGESTIONS_ACCEPTED",
    "PREVIEW_WITHOUT_SUGGESTIONS",
)

#: The full hand-written docs_preview tool surface
#: (docs/plans/2026-07-14-native-integration.md section 3).
REVIEW_TOOLS = {
    "list_document_suggestions",
    "get_doc_review_view",
    "check_docs_review_capabilities",
    "suggest_doc_edit",
    "manage_document_suggestion",
    "reply_to_doc_thread",
    "create_anchored_doc_comment",
}

#: The factory comment tools return human-readable confirmations; ids are
#: parsed from them. The id charset stops the match at any surrounding
#: prose (including literal backslash-n sequences in the house strings).
_COMMENT_ID_RE = re.compile(r"Comment ID: ([A-Za-z0-9._-]+)")
_REPLY_ID_RE = re.compile(r"Reply ID: ([A-Za-z0-9._-]+)")


def _poll_for_tool_error(
    mcp, name: str, arguments: dict, *, needle: str, timeout: float = 180.0
) -> str:
    """Wait for a tool call to start failing with ``needle`` in its error.

    Only for genuinely eventually-consistent server state (see the deletion
    note in test_comment_ops_on_deleted_doc_map_404). Returns the error text.
    """

    def check():
        result = mcp.call_tool_raw(name, dict(arguments))
        text = tool_text(result)
        return text if (result.is_error and needle in text) else None

    return poll_until(
        check,
        timeout=timeout,
        interval=5.0,
        description=f"{name} to fail with {needle!r}",
    )


def _comment_id_from(confirmation: str) -> str:
    match = _COMMENT_ID_RE.search(confirmation)
    assert match, f"no 'Comment ID:' in tool output: {confirmation[:300]!r}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_create_modify_read_roundtrip(mcp, ga_auth, scratch_doc):
    """create doc -> modify_doc_text insert -> get_doc_review_view."""
    sentinel = "Hello from the e2e harness."
    confirmation = tool_text(
        mcp.call_tool(
            "modify_doc_text",
            {
                "user_google_email": ga_auth.email,
                "document_id": scratch_doc,
                "start_index": 1,
                "text": sentinel,
            },
        )
    )
    assert scratch_doc in confirmation
    assert "Inserted text" in confirmation

    read = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {"user_google_email": ga_auth.email, "document_id": scratch_doc},
        )
    )
    assert read["view_mode"] == "SUGGESTIONS_INLINE"
    assert sentinel in read["body_text"]
    assert read["suggestion_ids"] == []


@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_review_view_each_view_mode(mcp, ga_auth, scratch_doc, view_mode):
    """get_doc_review_view shape sanity per view mode, no suggestions."""
    read = tool_json(
        mcp.call_tool(
            "get_doc_review_view",
            {
                "user_google_email": ga_auth.email,
                "document_id": scratch_doc,
                "view_mode": view_mode,
            },
        )
    )
    assert read["view_mode"] == view_mode
    assert isinstance(read["body_text"], str)
    assert read["suggestion_ids"] == []
    # A doc without suggestions must not carry CriticMarkup markers in
    # any mode.
    assert "{+" not in read["body_text"]
    assert "{-" not in read["body_text"]


def test_capabilities_report_is_side_effect_free(mcp, ga_auth):
    """Diagnostic capabilities tool: inventory + preview status, no API call."""
    report = tool_json(
        mcp.call_tool(
            "check_docs_review_capabilities", {"user_google_email": ga_auth.email}
        )
    )
    assert report["service"] == "docs_preview"
    assert report["probe_performed"] is False

    inventory = report["tools"]
    assert inventory["total"] == 7
    assert set(inventory["names"]) == REVIEW_TOOLS

    scopes = set(report["scopes"])
    assert "https://www.googleapis.com/auth/documents" in scopes
    assert "https://www.googleapis.com/auth/drive" in scopes

    preview = report["preview"]
    assert preview["availability"] in {"unknown", "available", "unavailable"}
    # Evidence must accompany any non-unknown verdict.
    if preview["availability"] != "unknown":
        assert preview["evidence"] is not None


def test_comment_lifecycle_including_update_and_delete(mcp, ga_auth, scratch_doc):
    """Unanchored comment via the Drive-backed factory tool:
    create -> list -> reply -> update -> resolve -> delete.

    Every confirmation must carry an id, and the listing an author (the
    the client requirement). update and delete are the actions added by the
    native-integration redirect (R3).
    """
    args = {"user_google_email": ga_auth.email, "document_id": scratch_doc}

    # create - confirmation with id
    created = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {**args, "action": "create", "comment_content": "e2e lifecycle comment"},
        )
    )
    assert "Comment created successfully" in created
    comment_id = _comment_id_from(created)

    def _listing_with(needle: str):
        listing = tool_text(mcp.call_tool("list_document_comments", dict(args)))
        return listing if needle in listing else None

    # list - id, author, content present
    listing = poll_until(
        lambda: _listing_with(comment_id), timeout=20, description="comment listed"
    )
    assert f"Comment ID: {comment_id}" in listing
    assert "Author:" in listing
    assert "e2e lifecycle comment" in listing

    # reply - confirmation with reply id
    replied = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {
                **args,
                "action": "reply",
                "comment_id": comment_id,
                "comment_content": "e2e reply",
            },
        )
    )
    assert "Reply posted successfully" in replied
    assert _REPLY_ID_RE.search(replied), replied

    # update (NEW action) - content change visible in the listing
    updated = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {
                **args,
                "action": "update",
                "comment_id": comment_id,
                "comment_content": "e2e lifecycle comment (updated)",
            },
        )
    )
    assert "Comment updated successfully" in updated
    poll_until(
        lambda: _listing_with("e2e lifecycle comment (updated)"),
        timeout=20,
        description="updated comment content listed",
    )

    # resolve - reflected in the listing
    resolved = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {**args, "action": "resolve", "comment_id": comment_id},
        )
    )
    assert "resolved" in resolved
    poll_until(
        lambda: _listing_with("[RESOLVED]"),
        timeout=20,
        description="comment resolved in listing",
    )

    # delete (NEW action) - gone from the listing
    deleted = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {**args, "action": "delete", "comment_id": comment_id},
        )
    )
    assert f"Comment {comment_id} deleted" in deleted

    def _gone():
        listing = tool_text(mcp.call_tool("list_document_comments", dict(args)))
        return listing if comment_id not in listing else None

    poll_until(_gone, timeout=20, description="deleted comment absent from listing")


# ---------------------------------------------------------------------------
# Preview probe + classifier consistency (works enrolled or not)
# ---------------------------------------------------------------------------


def test_preview_probe_classification_matches_reality(preview_probe):
    """The live probe's verdict must agree with the offline classifier.

    This is the empirical check chunk 3 asked for: the 400-unknown-field
    vs semantic-400 distinction is message-based, so we assert the real
    error message (whatever enrollment state we're in) classifies to the
    verdict the server recorded. Fails here => fix the patterns in
    gdocs_preview/preview_status.py (in-scope for the e2e chunk).
    """
    from gdocs_preview.preview_status import classify_preview_error

    preview = preview_probe["preview"]
    assert preview["source"] == "probe"
    assert preview["availability"] in {"available", "unavailable", "unknown"}
    evidence = preview["evidence"]
    assert evidence is not None
    assert "http_status" in evidence and "reason" in evidence

    REPORT.record_error_shape(
        "capabilities-probe (acceptSuggestion, bogus id)",
        evidence.get("http_status"),
        str(evidence.get("message") or evidence.get("reason")),
        classification=preview["availability"],
    )

    if evidence.get("http_status") == 200:
        # Probe request was accepted outright: preview must be available.
        assert preview["availability"] == "available"
    else:
        availability, reason = classify_preview_error(
            evidence["http_status"], evidence.get("message") or ""
        )
        assert availability == preview["availability"], (
            "classify_preview_error disagrees with the recorded live verdict; "
            f"real message was: {evidence.get('message')!r}"
        )
        assert reason == evidence["reason"]


def test_not_enrolled_write_tool_error_is_actionable(
    preview_probe, mcp, ga_auth, scratch_doc
):
    """Not-enrolled classification evidence: a preview write tool must
    fail with the uniform, actionable enrollment error.

    Only reproducible while the credentials are NOT enrolled - enrolled
    (or unclassifiable) runs skip with the probe verdict on record.
    """
    preview = preview_probe["preview"]
    if preview.get("availability") != "unavailable":
        pytest.skip(
            "preview availability is "
            f"{preview.get('availability')!r} - the not-enrolled error shape "
            "is only reproducible for unenrolled credentials."
        )
    error_text = mcp.expect_tool_error(
        "suggest_doc_edit",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "start_index": 1,
            "text": "not-enrolled probe",
        },
    )
    assert "Developer Preview" in error_text
    assert "check_docs_review_capabilities" in error_text
    REPORT.record_error_shape(
        "suggest_doc_edit while not enrolled",
        None,
        error_text,
        classification="unavailable",
    )


# ---------------------------------------------------------------------------
# Sad paths
# ---------------------------------------------------------------------------


def test_read_nonexistent_document_maps_404(mcp, ga_auth):
    error_text = mcp.expect_tool_error(
        "get_doc_review_view",
        {
            "user_google_email": ga_auth.email,
            "document_id": "e2e-nonexistent-document-id",
        },
    )
    assert "API error in get_doc_review_view" in error_text
    assert "404" in error_text


def test_review_view_invalid_view_mode_is_input_error(mcp, ga_auth, scratch_doc):
    error_text = mcp.expect_tool_error(
        "get_doc_review_view",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "view_mode": "NOT_A_VIEW_MODE",
        },
    )
    assert "Invalid view_mode" in error_text


def test_modify_text_invalid_range_maps_400(mcp, ga_auth, scratch_doc):
    error_text = mcp.expect_tool_error(
        "modify_doc_text",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "start_index": 999_999,
            "text": "out of range",
        },
    )
    assert "API error in modify_doc_text" in error_text
    assert "400" in error_text


def test_comment_ops_on_trashed_doc(mcp, ga_auth, make_scratch_doc, doc_tracker):
    """Comment operations against a trashed (not deleted) document.

    Drive's documented behavior for trashed files is soft: reads keep
    working. We assert list still succeeds and RECORD what create does -
    the first real run pins the create expectation down (close-out
    reviews the recorded shape).
    """
    doc_id = make_scratch_doc("-trashed")
    args = {"user_google_email": ga_auth.email, "document_id": doc_id}

    pre_trash = tool_text(
        mcp.call_tool(
            "manage_document_comment",
            {**args, "action": "create", "comment_content": "comment before trash"},
        )
    )
    pre_trash_id = _comment_id_from(pre_trash)
    doc_tracker.cleanup(doc_id)  # trash it NOW, mid-test

    listing = tool_text(mcp.call_tool("list_document_comments", dict(args)))
    assert pre_trash_id in listing

    create_result = mcp.call_tool_raw(
        "manage_document_comment",
        {**args, "action": "create", "comment_content": "comment after trash"},
    )
    outcome_text = tool_text(create_result)
    if create_result.is_error:
        assert "403" in outcome_text or "404" in outcome_text
        REPORT.record_error_shape("comments.create on trashed doc", None, outcome_text)
    else:
        assert "Comment created successfully" in outcome_text
        REPORT.note(
            "comments.create on a TRASHED doc succeeds "
            f"(id {_comment_id_from(outcome_text)}) - trash is soft for "
            "comment ops."
        )


def test_comment_ops_on_deleted_doc_map_404(
    mcp, ga_auth, make_scratch_doc, doc_tracker, harness_drive
):
    """Permanently deleted doc: comment ops must surface 404."""
    doc_id = make_scratch_doc("-deleted")
    harness_drive.files().delete(fileId=doc_id).execute()
    doc_tracker.mark_cleaned(doc_id, "delete")

    args = {"user_google_email": ga_auth.email, "document_id": doc_id}

    # Deletion is EVENTUALLY consistent, empirically (2026-07-30, first real
    # run). Immediately after files.delete returns, reads still succeed:
    # drive.comments.list yields an empty list and docs.documents.get serves
    # the full document, while drive.files.get already 404s. All three settle
    # on 404 within ~90s. So poll for the 404 rather than demanding it at
    # once - and note that an agent which deletes and then immediately
    # verifies will observe a stale success.
    error_text = _poll_for_tool_error(
        mcp, "list_document_comments", dict(args), needle="404"
    )
    assert "404" in error_text

    error_text = mcp.expect_tool_error(
        "manage_document_comment",
        {**args, "action": "create", "comment_content": "should not land"},
    )
    assert "404" in error_text


def test_delete_nonexistent_comment_maps_404(mcp, ga_auth, scratch_doc):
    error_text = mcp.expect_tool_error(
        "manage_document_comment",
        {
            "user_google_email": ga_auth.email,
            "document_id": scratch_doc,
            "action": "delete",
            "comment_id": "AAAA-e2e-nonexistent-comment",
        },
    )
    assert "API error in manage_document_comment" in error_text
    assert "404" in error_text
