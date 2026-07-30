"""Curated review tools for the docs_preview service.

Hand-written review ergonomics: suggestion diffing with computed pre/post
text, a capabilities/preview-availability report, and a reviewer-view read
tool. The native suggestion/comment write tools live alongside these in
:mod:`gdocs_preview.write_tools` -- see
docs/plans/2026-07-14-native-integration.md and, for the underlying
preview API semantics, docs/preview-api-reference.md.

The heavy lifting lives in the pure functions of
:mod:`gdocs_preview.analysis`; tools here are thin API-call wrappers around
those plus :mod:`gdocs_preview.preview_read`, which supplies the
thread-bearing (author-carrying) document read.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.scopes import DOCS_PREVIEW_SCOPES
from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdocs_preview import preview_status, suggestion_ledger
from gdocs_preview.analysis import extract_suggestions_from_tabs, render_tabs
from gdocs_preview.preview_read import (
    READ_SOURCE_GA,
    READ_SOURCE_PREVIEW,
    ReviewRead,
    read_for_review,
)

__all__ = [
    "READ_SOURCE_GA",
    "READ_SOURCE_PREVIEW",
    "REVIEW_TOOL_NAMES",
    "ReviewRead",
    "read_for_review",
]

logger = logging.getLogger(__name__)

VIEW_MODES = (
    "SUGGESTIONS_INLINE",
    "PREVIEW_SUGGESTIONS_ACCEPTED",
    "PREVIEW_WITHOUT_SUGGESTIONS",
)

#: Every hand-written tool this service registers (read/diagnostic tools
#: here, write tools in :mod:`gdocs_preview.write_tools`). The capabilities
#: report's inventory is built from this list -- keep it in sync with the
#: registered surface (docs/plans/2026-07-14-native-integration.md #3).
REVIEW_TOOL_NAMES = [
    "list_document_suggestions",
    "get_doc_review_view",
    "check_docs_review_capabilities",
    "suggest_doc_edit",
    "manage_document_suggestion",
    "reply_to_doc_thread",
    "create_anchored_doc_comment",
]

#: Deliberately unresolvable suggestion id used by the capabilities probe.
#: Suggestion ids are server-generated; this shape cannot collide with one.
_PROBE_SUGGESTION_ID = "gdocs-review-capabilities-probe-nonexistent-suggestion"


def _tool_inventory() -> dict[str, Any]:
    """Static inventory of the service's hand-written tools."""
    return {
        "total": len(REVIEW_TOOL_NAMES),
        "names": list(REVIEW_TOOL_NAMES),
    }


@server.tool(
    title="List Document Suggestions",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("list_document_suggestions", is_read_only=True, service_type="docs")
@require_google_service("docs", "docs_read")
async def list_document_suggestions(
    service: Any,
    user_google_email: str,
    document_id: str,
) -> str:
    """List every pending edit suggestion in a document, with its author and
    computed pre/post text.

    Reads the document in SUGGESTIONS_INLINE view and returns one record per
    suggestion id: type (insertion/deletion/replacement/style/mixed),
    pre_text (base text of the affected range: all insertions stripped, all
    deletions kept), post_text (the range with this suggestion -- and only
    this one -- applied), ~40-char context windows computed on the base
    text, segment location (body/header/footer/footnote incl. segment id),
    tab id, table flag, and start/end indexes.

    Indexes are UTF-16 code units relative to the SUGGESTIONS_INLINE view,
    passed through from the API verbatim -- exactly what batchUpdate
    requests computed against that view expect.

    Each record also carries the suggestion thread joined on its id:
    ``author`` (display_name/me/anonymous/user), ``status``,
    ``create_time``, Google's own ``summary_text`` (e.g. ``Replace: "x" with
    "y"``) and the thread's ``replies`` (each with post_id and author).
    Threads come from the Developer Preview read; if that is unavailable the
    tool still returns every suggestion, with ``author: null`` and
    ``author_source: "unavailable"`` (never guessed) and ``read_source:
    "ga_documents_get"``.

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to analyse.

    Returns:
        str: JSON with document_id, title, suggestion_count, read_source,
            tabs and the per-suggestion records described above.
    """
    read = await read_for_review(service, document_id, "SUGGESTIONS_INLINE")
    result = extract_suggestions_from_tabs(read.tabs, read.threads)
    # Feed the ledger: this listing is the "before" picture that lets a later
    # accept/reject name the suggestions its own resolution took with it
    # (gdocs_preview/suggestion_ledger.py).
    suggestion_ledger.observe(
        user_google_email, document_id, result.get("suggestions") or []
    )
    result["read_source"] = read.source
    result["tabs"] = read.tab_metadata
    if read.degraded_reason:
        result["degraded_reason"] = read.degraded_reason
    return json.dumps(result, indent=2, ensure_ascii=False)


@server.tool(
    title="Check Docs Review Capabilities",
    annotations=ToolAnnotations(
        readOnlyHint=False,  # probe=true POSTs a content-safe batchUpdate
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("check_docs_review_capabilities", service_type="docs")
@require_google_service("docs", "docs_write")
async def check_docs_review_capabilities(
    service: Any,
    user_google_email: str,
    document_id: Optional[str] = None,
    probe: bool = False,
) -> str:
    """Report what the docs_preview service can do with the current
    credentials, including Developer Preview availability.

    Side-effect free by default: reports configured scopes, the service's
    tool inventory, and the cached (last-known) preview availability
    verdict. No API call is made unless ``probe=true``.

    With ``probe=true`` (requires ``document_id``) it performs the cheapest
    real preview check that cannot mutate user-visible content: a
    batchUpdate containing a single acceptSuggestion request for a
    deliberately nonexistent suggestion id. Accepting a nonexistent
    suggestion cannot alter the document. Outcome classification:
      - 400 unknown-field error -> preview ``unavailable`` (not enrolled;
        the request type was not even parsed);
      - other 400 -> ``available`` (request type recognized, failed only on
        the bogus suggestion id);
      - 200 -> ``available`` (per preview docs, suggestion/comment updates
        can no-op with a commentUpdateState instead of erroring);
      - 403/404 -> ``unknown`` (permission/scope or missing document --
        proves nothing about enrollment).
    The verdict is cached process-wide, so later probe-free calls report it.

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): Document used for the live probe. Only needed
            with probe=true.
        probe (bool): Perform the live preview-availability check described
            above. Defaults to False (no API call).

    Returns:
        str: JSON report: scopes, tools {total, names},
            preview {availability, evidence, source, checked_at},
            probe_performed.
    """
    if probe:
        if not document_id:
            raise UserInputError(
                "probe=true requires document_id (the live preview check "
                "runs against a real document)."
            )
        body = {
            "requests": [{"acceptSuggestion": {"suggestionId": _PROBE_SUGGESTION_ID}}]
        }
        try:
            api_call = service.documents().batchUpdate(
                documentId=document_id, body=body
            )
            response = await asyncio.to_thread(api_call.execute)
            preview_status.record(
                "available",
                {
                    "http_status": 200,
                    "reason": "probe_request_accepted",
                    "comment_update_state": (response or {}).get("commentUpdateState"),
                },
                source="probe",
            )
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            content = getattr(error, "content", b"") or b""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            message = f"{error} {content}"
            availability, reason = preview_status.classify_preview_error(
                status, message
            )
            preview_status.record(
                availability,
                {
                    "http_status": status,
                    "reason": reason,
                    "message": message[:500],
                },
                source="probe",
            )

    report = {
        "service": "docs_preview",
        "scopes": list(DOCS_PREVIEW_SCOPES),
        "tools": _tool_inventory(),
        "preview": preview_status.get_status(),
        "probe_performed": bool(probe),
    }
    return json.dumps(report, indent=2, ensure_ascii=False)


@server.tool(
    title="Get Doc Review View",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("get_doc_review_view", is_read_only=True, service_type="docs")
@require_google_service("docs", "docs_read")
async def get_doc_review_view(
    service: Any,
    user_google_email: str,
    document_id: str,
    view_mode: str = "SUGGESTIONS_INLINE",
) -> str:
    """Read a document the way a reviewer sees it: plain text with inline
    suggestion markers, a paragraph map, and the comment threads.

    In SUGGESTIONS_INLINE view (default), pending insertions render as
    ``{+text+}`` and pending deletions as ``{-text-}`` (CriticMarkup
    style); each paragraph entry lists the suggestion ids touching it.
    PREVIEW_SUGGESTIONS_ACCEPTED / PREVIEW_WITHOUT_SUGGESTIONS return the
    respective clean text.

    ``comments`` carries the Docs-side comment threads: comment_id,
    anchor_id, status, quoted_text, the head post's author/content/times and
    every reply with its own post_id and author. That is richer than the
    Drive comment surface (``list_document_comments``), which has no anchor
    id, no per-post ids and no People resource names -- use this for review,
    Drive for cross-surface management. Comment threads need the Developer
    Preview read; without it ``comments`` is empty and ``read_source`` says
    ``ga_documents_get``.

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to read.
        view_mode (str): One of SUGGESTIONS_INLINE,
            PREVIEW_SUGGESTIONS_ACCEPTED, PREVIEW_WITHOUT_SUGGESTIONS.

    Returns:
        str: JSON with view_mode, read_source, tabs, body_text, per-paragraph
            map (segment/tab/indexes/text/style/list/table/suggestion_ids),
            header/footer/footnote texts, all suggestion ids, and comments.
    """
    if view_mode not in VIEW_MODES:
        raise UserInputError(
            f"Invalid view_mode '{view_mode}'. Must be one of: {', '.join(VIEW_MODES)}."
        )
    read = await read_for_review(service, document_id, view_mode)
    rendered = render_tabs(read.tabs)
    if view_mode == "SUGGESTIONS_INLINE":
        # Same "before" picture as list_document_suggestions; only the inline
        # view carries the pending suggestions, so the other two modes must
        # not be mistaken for "the document has no suggestions".
        suggestion_ledger.observe(
            user_google_email,
            document_id,
            extract_suggestions_from_tabs(read.tabs, read.threads)["suggestions"],
        )
    result = {
        "view_mode": view_mode,
        "read_source": read.source,
        "tabs": read.tab_metadata,
        **rendered,
        "comments": read.comments,
    }
    if read.degraded_reason:
        result["degraded_reason"] = read.degraded_reason
    return json.dumps(result, indent=2, ensure_ascii=False)
