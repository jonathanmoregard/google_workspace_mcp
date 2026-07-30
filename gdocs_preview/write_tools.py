"""Native Developer Preview write tools for the docs_preview service.

Hand-written suggestion/comment write tools built on the Docs API
Developer Preview batchUpdate surface: SUGGEST-mode edits, suggestion
accept/reject, thread replies, and range-anchored comments. Design:
docs/plans/2026-07-14-native-integration.md; API semantics:
docs/preview-api-reference.md.

Every write goes through :func:`_execute_preview_batch_update`, the single
choke point that classifies not-enrolled failures into an actionable
``UserInputError``, feeds :mod:`gdocs_preview.preview_status`, and (for
thread operations) enforces ``commentUpdateState`` -- a batch can return
HTTP 200 while the thread update silently fails.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdocs_preview import preview_status
from gdocs_preview.preview_read import normalize_author

logger = logging.getLogger(__name__)


def _doc_link(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _location(
    index: int, segment_id: Optional[str], tab_id: Optional[str]
) -> dict[str, Any]:
    """Build a Location dict, including segmentId/tabId only when non-None.

    An empty segmentId means the document body, so None must omit the key
    entirely rather than send an empty string.
    """
    location: dict[str, Any] = {"index": index}
    if segment_id is not None:
        location["segmentId"] = segment_id
    if tab_id is not None:
        location["tabId"] = tab_id
    return location


def _range(
    start_index: int,
    end_index: int,
    segment_id: Optional[str],
    tab_id: Optional[str],
) -> dict[str, Any]:
    """Build a Range dict, including segmentId/tabId only when non-None."""
    range_: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
    if segment_id is not None:
        range_["segmentId"] = segment_id
    if tab_id is not None:
        range_["tabId"] = tab_id
    return range_


async def _execute_preview_batch_update(
    service: Any,
    tool_name: str,
    document_id: str,
    requests: list[dict],
    *,
    write_mode: Optional[str] = None,
    enforce_comment_update: bool = False,
) -> dict:
    """Execute a preview batchUpdate: the single choke point for writes.

    - Success records ``available`` evidence in
      :mod:`gdocs_preview.preview_status` (source ``tool_call``).
    - ``HttpError`` is classified via
      :func:`preview_status.classify_preview_error` and recorded; a
      not-enrolled verdict raises a uniform, actionable ``UserInputError``
      (surfaced verbatim to the client), anything else re-raises the
      ``HttpError`` for ``handle_http_errors`` to wrap.
    - With ``enforce_comment_update=True`` (thread operations), an HTTP 200
      carrying ``commentUpdateState=ALL_FAILED_UNKNOWN_REASON`` raises --
      partial failure must never look like success.
    """
    body: dict[str, Any] = {"requests": requests}
    if write_mode is not None:
        body["writeControl"] = {"writeMode": write_mode}
    try:
        api_call = service.documents().batchUpdate(documentId=document_id, body=body)
        response = await asyncio.to_thread(api_call.execute)
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", None)
        content = getattr(error, "content", b"") or b""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        message = f"{error} {content}"
        availability, reason = preview_status.classify_preview_error(status, message)
        preview_status.record(
            availability,
            {
                "http_status": status,
                "reason": reason,
                "message": message[:500],
            },
            source="tool_call",
        )
        if (availability, reason) == ("unavailable", "not_enrolled"):
            raise UserInputError(
                f"{tool_name} requires Google Workspace Developer Preview "
                f"enrollment for the authenticated project. Enrollment steps: "
                f"pending_for_human.md. Verify with "
                f"check_docs_review_capabilities(probe=true)."
            ) from error
        raise
    preview_status.record(
        "available",
        {"http_status": 200, "reason": "preview_request_succeeded"},
        source="tool_call",
    )
    response = response or {}
    if (
        enforce_comment_update
        and response.get("commentUpdateState") == "ALL_FAILED_UNKNOWN_REASON"
    ):
        raise UserInputError(
            f"{tool_name}: the API returned HTTP 200 but reported "
            "commentUpdateState=ALL_FAILED_UNKNOWN_REASON - the thread operation "
            "was NOT saved. Retry; if it persists, check enrollment and document "
            "permissions."
        )
    return response


@server.tool(
    title="Suggest Doc Edit",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,  # creates a pending suggestion; nothing applied
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("suggest_doc_edit", service_type="docs")
@require_google_service("docs", "docs_write")
async def suggest_doc_edit(
    service: Any,
    user_google_email: str,
    document_id: str,
    start_index: int,
    end_index: Optional[int] = None,
    text: Optional[str] = None,
    tab_id: Optional[str] = None,
    segment_id: Optional[str] = None,
) -> str:
    """Create a suggested insertion, deletion, or replacement as a pending
    suggestion (SUGGEST write mode).

    The mode is inferred from the params: text only -> insertion at
    start_index; end_index only -> deletion of [start_index, end_index);
    both -> replacement (delete, then insert at start_index, in one
    batch). Indexes are UTF-16 code units in the SUGGESTIONS_INLINE
    coordinate space -- take them verbatim from
    list_document_suggestions / get_doc_review_view output. The edit lands
    as a *pending suggestion*: nothing is applied to the document until it
    is accepted; it is visible to list_document_suggestions and in the
    Docs UI.

    Requires Google Workspace Developer Preview enrollment (verify with
    check_docs_review_capabilities probe=true).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to suggest an edit in.
        start_index (int): UTF-16 start index of the edit. Must be >= 1.
        end_index (int): UTF-16 end index (exclusive) of the range to
            delete. Omit for a pure insertion.
        text (str): Text to insert. Omit for a pure deletion.
        tab_id (str): Optional document tab ID to target.
        segment_id (str): Optional header/footer/footnote segment ID;
            omitted means the document body.

    Returns:
        str: JSON with document_id, mode (insertion|deletion|replacement),
            created_suggestion_ids, requests_applied, and link.
    """
    if start_index < 1:
        raise UserInputError(
            "start_index must be >= 1. Take indexes verbatim from "
            "list_document_suggestions or get_doc_review_view output."
        )
    if text is None and end_index is None:
        raise UserInputError(
            "Provide text (insertion), end_index (deletion), or both (replacement)."
        )
    if end_index is not None and end_index <= start_index:
        raise UserInputError(
            f"end_index ({end_index}) must be greater than start_index ({start_index})."
        )

    requests: list[dict] = []
    if end_index is None:
        mode = "insertion"
        requests.append(
            {
                "insertText": {
                    "location": _location(start_index, segment_id, tab_id),
                    "text": text,
                }
            }
        )
    elif text is None:
        mode = "deletion"
        requests.append(
            {
                "deleteContentRange": {
                    "range": _range(start_index, end_index, segment_id, tab_id)
                }
            }
        )
    else:
        # Replacement: delete then insert at start_index, mirroring
        # modify_doc_text's replacement path. In SUGGEST mode the deleted
        # text stays in the document (marked), so no index shifting.
        # UNCERTAIN (pending enrollment): EDIT-mode batches resolve indexes
        # against the pre-batch document; whether SUGGEST-mode shares that
        # semantics is transcribed-not-verified. The preview e2e replacement
        # scenario pins reality on the first enrolled run.
        mode = "replacement"
        requests.append(
            {
                "deleteContentRange": {
                    "range": _range(start_index, end_index, segment_id, tab_id)
                }
            }
        )
        requests.append(
            {
                "insertText": {
                    "location": _location(start_index, segment_id, tab_id),
                    "text": text,
                }
            }
        )

    logger.info(
        f"[suggest_doc_edit] Doc={document_id}, mode={mode}, "
        f"start={start_index}, end={end_index}"
    )
    response = await _execute_preview_batch_update(
        service, "suggest_doc_edit", document_id, requests, write_mode="SUGGEST"
    )

    created_ids: list[str] = []
    for suggestion_response in response.get("suggestionResponses") or []:
        for sid in (suggestion_response or {}).get("createdSuggestionIds") or []:
            if sid not in created_ids:
                created_ids.append(sid)

    result = {
        "document_id": document_id,
        "mode": mode,
        "created_suggestion_ids": created_ids,
        "requests_applied": len(requests),
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@server.tool(
    title="Manage Document Suggestion",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,  # accept applies deletions; reject discards the suggestion
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_document_suggestion", service_type="docs")
@require_google_service("docs", "docs_write")
async def manage_document_suggestion(
    service: Any,
    user_google_email: str,
    document_id: str,
    action: str,
    suggestion_id: str,
) -> str:
    """Accept or reject a pending suggestion by id.

    Permission rules (preview API): accept requires edit access to the
    document; reject requires edit access OR being the suggestion's
    author. A nonexistent suggestion id may surface as a 400 error OR as
    an HTTP 200 no-op carrying a commentUpdateState -- when the API sends
    that state it is included in the response JSON.

    Requires Google Workspace Developer Preview enrollment (verify with
    check_docs_review_capabilities probe=true).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document.
        action (str): One of "accept" or "reject".
        suggestion_id (str): The suggestion to act on (from
            list_document_suggestions).

    Returns:
        str: JSON with document_id, action, suggestion_id,
            accepted_suggestion_ids or rejected_suggestion_ids, and link.
    """
    action_normalized = action.lower().strip()
    if action_normalized == "accept":
        request_key = "acceptSuggestion"
        response_field = "acceptedSuggestionIds"
        result_key = "accepted_suggestion_ids"
    elif action_normalized == "reject":
        request_key = "rejectSuggestion"
        response_field = "rejectedSuggestionIds"
        result_key = "rejected_suggestion_ids"
    else:
        raise UserInputError(
            f"Invalid action '{action_normalized}'. Must be 'accept' or 'reject'."
        )

    logger.info(
        f"[manage_document_suggestion] Doc={document_id}, "
        f"action={action_normalized}, suggestion={suggestion_id}"
    )
    requests = [{request_key: {"suggestionId": suggestion_id}}]
    response = await _execute_preview_batch_update(
        service, "manage_document_suggestion", document_id, requests
    )

    affected_ids: list[str] = []
    for suggestion_response in response.get("suggestionResponses") or []:
        for sid in (suggestion_response or {}).get(response_field) or []:
            if sid not in affected_ids:
                affected_ids.append(sid)

    result = {
        "document_id": document_id,
        "action": action_normalized,
        "suggestion_id": suggestion_id,
        result_key: affected_ids,
        "link": _doc_link(document_id),
    }
    comment_update_state = response.get("commentUpdateState")
    if comment_update_state is not None:
        result["comment_update_state"] = comment_update_state
    return json.dumps(result, indent=2, ensure_ascii=False)


@server.tool(
    title="Reply to Doc Thread",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("reply_to_doc_thread", service_type="docs")
@require_google_service("docs", "docs_write")
async def reply_to_doc_thread(
    service: Any,
    user_google_email: str,
    document_id: str,
    reply_content: str,
    comment_id: Optional[str] = None,
    suggestion_id: Optional[str] = None,
) -> str:
    """Reply to a comment thread OR a suggestion thread.

    Replies are authored as the authenticated user. Suggestion threads
    exist only after a SUGGEST-mode edit (suggest_doc_edit);
    comment-thread ids come from create_anchored_doc_comment /
    list_document_comments. The reply content must be non-empty (the API
    additionally caps it at 2048 UTF-8 code units).

    Requires Google Workspace Developer Preview enrollment (verify with
    check_docs_review_capabilities probe=true).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document.
        reply_content (str): The reply text. Must be non-empty.
        comment_id (str): Target comment thread. Provide exactly one of
            comment_id / suggestion_id.
        suggestion_id (str): Target suggestion thread. Provide exactly one
            of comment_id / suggestion_id.

    Returns:
        str: JSON with document_id, thread_type (comment|suggestion), the
            target thread id, post_id, author (the reply's PostAuthor as
            the API recorded it), comment_update_state, and link.
    """
    if (comment_id is None) == (suggestion_id is None):
        raise UserInputError("Provide exactly one of comment_id or suggestion_id.")
    if not reply_content or not reply_content.strip():
        raise UserInputError("reply_content must be non-empty.")

    add_comment_reply: dict[str, Any] = {"post": {"content": reply_content}}
    if comment_id is not None:
        thread_type = "comment"
        thread_id_key = "comment_id"
        thread_id = comment_id
        add_comment_reply["commentId"] = comment_id
    else:
        thread_type = "suggestion"
        thread_id_key = "suggestion_id"
        thread_id = suggestion_id
        add_comment_reply["suggestionId"] = suggestion_id

    logger.info(
        f"[reply_to_doc_thread] Doc={document_id}, {thread_type} thread={thread_id}"
    )
    requests = [{"addCommentReply": add_comment_reply}]
    response = await _execute_preview_batch_update(
        service,
        "reply_to_doc_thread",
        document_id,
        requests,
        enforce_comment_update=True,
    )

    # Verified 2026-07-30 against the real API: the batchUpdate Response
    # union does carry an ``addCommentReply`` member holding the new Post,
    # author included (docs/preview-api-reference.md).
    post: dict[str, Any] = {}
    replies = response.get("replies") or []
    if replies:
        reply_payload = (replies[0] or {}).get("addCommentReply") or {}
        post = reply_payload.get("post") or {}

    result = {
        "document_id": document_id,
        "thread_type": thread_type,
        thread_id_key: thread_id,
        "post_id": post.get("postId"),
        "author": normalize_author(post.get("author")),
        "create_time": post.get("createTime"),
        "comment_update_state": response.get("commentUpdateState"),
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@server.tool(
    title="Create Anchored Doc Comment",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_anchored_doc_comment", service_type="docs")
@require_google_service("docs", "docs_write")
async def create_anchored_doc_comment(
    service: Any,
    user_google_email: str,
    document_id: str,
    content: str,
    start_index: int,
    end_index: int,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    assignee_email: Optional[str] = None,
) -> str:
    """Create a comment anchored to a text range, exactly like a human
    comment in the Docs UI.

    The comment appears anchored to [start_index, end_index); indexes are
    UTF-16 code units per the current document. A range is required --
    for unanchored document-level comments use
    manage_document_comment action="create" (Drive API). Use
    list_document_comments to enumerate comments afterwards.

    Requires Google Workspace Developer Preview enrollment (verify with
    check_docs_review_capabilities probe=true).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to comment on.
        content (str): Comment text. Must be non-empty (API cap: 2048
            UTF-8 code units).
        start_index (int): UTF-16 start index of the anchored range.
            Must be >= 1.
        end_index (int): UTF-16 end index (exclusive). Must be greater
            than start_index.
        segment_id (str): Optional header/footer/footnote segment ID;
            omitted means the document body.
        tab_id (str): Optional document tab ID to target.
        assignee_email (str): Optional email address to assign the
            comment to.

    Returns:
        str: JSON with document_id, comment_id, post_id, author (the
            thread head post's PostAuthor as the API recorded it),
            anchor_id, quoted_text, status, comment_update_state, and link.
    """
    if not content or not content.strip():
        raise UserInputError("content must be non-empty.")
    if start_index < 1:
        raise UserInputError("start_index must be >= 1.")
    if end_index <= start_index:
        raise UserInputError(
            f"end_index ({end_index}) must be greater than start_index ({start_index})."
        )

    insert_comment: dict[str, Any] = {"content": content}
    if assignee_email is not None:
        insert_comment["assigneeEmailAddress"] = assignee_email
    insert_comment["range"] = _range(start_index, end_index, segment_id, tab_id)

    logger.info(
        f"[create_anchored_doc_comment] Doc={document_id}, "
        f"range=[{start_index}, {end_index})"
    )
    requests = [{"insertComment": insert_comment}]
    response = await _execute_preview_batch_update(
        service,
        "create_anchored_doc_comment",
        document_id,
        requests,
        enforce_comment_update=True,
    )

    # Verified 2026-07-30 against the real API: the batchUpdate Response
    # union carries an ``insertComment`` member holding the whole
    # CommentThread, headPost.author included -- so the write path can
    # report the author it just created without a follow-up read.
    thread: dict[str, Any] = {}
    replies = response.get("replies") or []
    if replies:
        thread = ((replies[0] or {}).get("insertComment") or {}).get(
            "commentThread"
        ) or {}
    head_post = thread.get("headPost") or {}

    result = {
        "document_id": document_id,
        "comment_id": thread.get("commentId"),
        "post_id": head_post.get("postId"),
        "author": normalize_author(head_post.get("author")),
        "create_time": head_post.get("createTime"),
        "anchor_id": thread.get("anchorId"),
        "quoted_text": thread.get("plainTextQuote"),
        "status": thread.get("status"),
        "comment_update_state": response.get("commentUpdateState"),
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
