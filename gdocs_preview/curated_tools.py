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
from gdocs_preview import preview_status, review_page, suggestion_ledger
from gdocs_preview.analysis import extract_suggestions_from_tabs, render_tabs
from gdocs_preview.preview_read import (
    READ_SOURCE_GA,
    READ_SOURCE_PREVIEW,
    ReviewRead,
    read_for_review,
)
from gdocs_preview.review_page import PageTokenError

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
    fields: str = "summary",
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    author: Optional[str] = None,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    status: Optional[str] = None,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> str:
    """List the pending edit suggestions in a document, one record each, with
    filtering, field selection and pagination.

    Reads the document in SUGGESTIONS_INLINE view. Indexes are UTF-16 code
    units relative to that view, passed through from the API verbatim --
    exactly what batchUpdate requests computed against that view expect.

    **fields='summary' is the default, deliberately.** A review set is
    linear in card count and a document has no cap on it; the full record is
    ~780 characters per card, so 120 pending suggestions is ~93,000
    characters and a real client answered that with "result exceeds maximum
    allowed tokens" and spilled it to a file the agent could not open -- the
    agent never saw a single suggestion id. A default response that cannot be
    delivered is not the conservative choice. ``summary`` costs ~232-252
    characters per card (measured across all four stress tiers,
    ``llmux/scenarios/stressgen/measure.py``; budgeted at 260) and keeps
    everything a decision needs:
    ``suggestion_id``, ``type``, ``author`` (the display name as a plain
    string), ``summary_text`` (Google's own label, e.g. ``Replace: "x" with
    "y"``), ``segment``, ``segment_id``, ``tab_id``, ``start_index``,
    ``end_index`` and ``status``. What it omits is listed in the response's
    ``omitted_fields``.

    **An index is only meaningful together with its segment and tab.** Docs
    counts indexes per ``(tabId, segmentId)``, so ``start_index`` 412 in a
    footnote and 412 in the body are different places. Every record therefore
    carries ``segment`` (``body``/``header``/``footer``/``footnote``),
    ``segment_id`` (null for the body) and ``tab_id`` (null on a single-tab
    or GA read), in BOTH field modes. Pass those same values back to
    ``suggest_doc_edit`` / ``create_anchored_doc_comment``: those tools
    default to the body of the default tab, so a header or footnote index
    used without them lands in the wrong segment.

    **fields='full'** restores every field: ``pre_text`` (base text of the
    affected range: all insertions stripped, all deletions kept),
    ``post_text`` (that range with this suggestion -- and only this one --
    applied), the two ~40-char context windows computed on the base text,
    table flag, ``create_time``, ``author`` as the full
    display_name/me/anonymous/user object, ``author_source`` and the thread's
    ``replies``. Use it with a small ``page_size`` when you need the
    before/after text of specific cards.

    **Nothing is ever truncated silently.** Every response reports
    ``suggestion_count`` (pending suggestions in the whole document),
    ``matched_count`` (after filters) and ``returned_count`` (this page), and
    a page that is not the last one carries ``page.next_page_token`` plus a
    ``notice_page`` saying so in words. Compare the three integers and you
    know whether you have seen everything.

    Thread-derived fields (``author``, ``status``, ``create_time``,
    ``summary_text``, ``replies``) come from the Developer Preview read. If
    that is unavailable the tool still returns every suggestion, with those
    fields null, ``author_source: "unavailable"`` (never guessed) and
    ``read_source: "ga_documents_get"``. Such a response carries
    ``degraded_notice`` and ``null_fields`` in BOTH field modes -- the nulls
    are a property of the read, not of the document -- and an ``author`` or
    ``status`` filter is REFUSED on it rather than answered with an empty
    page, because ``matched_count: 0`` there means "this read cannot see
    authors", not "there are none".

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to analyse.
        fields (str): "summary" (default) or "full". See above.
        page_size (int): Records per page. The bound is BYTES, not cards: a
            record costs ~232-252 characters in "summary" and ~780-792 in
            "full" (measured; budgeted at 260 and 800), so one number for
            both modes would be wrong for one of them.
            Defaults to about 35 KB of records (134 summary / 43 full) and is
            capped at about 50 KB (192 summary / 62 full) -- past roughly
            57 KB the observed client stops delivering tool output and writes
            it to a file the agent cannot open. A request above the ceiling
            is reduced AND says so, in page.page_size_requested and
            page.page_size_note; it is never silently trimmed.
        page_token (str): Resume after a previous page. Pass back the
            ``page.next_page_token`` from the response, with the SAME
            fields/filters; a token from a different query is refused rather
            than silently reinterpreted.
        author (str): Only suggestions by this author display name
            (case-insensitive, exact -- not a substring match). When nothing
            matches, the response lists the authors that are present.
        start_index (int): Only suggestions whose UTF-16 range overlaps the
            half-open [start_index, end_index) -- endIndex is exclusive, as
            everywhere else in the Docs API. Either bound may be given alone.
            This is how you review one section. The range is read in ONE
            (tab, segment): the body of the document's only tab unless
            segment_id/tab_id say otherwise, because Docs numbers each tab
            and each header/footer/footnote from its own start. Cards outside
            that space are excluded and counted in
            filters.excluded_other_segments, and the space actually used is
            echoed as filters.range_scope.
        end_index (int): See start_index.
        status (str): Only suggestions with this thread status, e.g. "OPEN"
            (case-insensitive, exact).
        segment_id (str): Only suggestions in this header/footer/footnote
            segment (take the value from a record's own segment_id); also the
            coordinate space start_index/end_index are read in. Omit for the
            document body.
        tab_id (str): Only suggestions in this tab, and the tab an index
            range is read in. Omit for a single-tab document -- it is
            resolved from the records; a document with more than one tab
            REFUSES an index range without it rather than guessing. When it
            matches nothing the response lists filters.tabs_present, the
            same way author and status list theirs.

    Returns:
        str: JSON with document_id, title, suggestion_count, matched_count,
            returned_count, fields, filters, page (page_size, offset,
            has_more, next_page_token), read_source, tabs and the
            per-suggestion records described above.
    """
    read = await read_for_review(service, document_id, "SUGGESTIONS_INLINE")
    analysis = extract_suggestions_from_tabs(read.tabs, read.threads)
    # Feed the ledger the WHOLE set, never the page: it is the "before"
    # picture that lets a later accept/reject name the suggestions its own
    # resolution took with it (gdocs_preview/suggestion_ledger.py), and a
    # ledger that only knew about page 2 would explain nothing about page 1.
    suggestion_ledger.observe(
        user_google_email, document_id, analysis.get("suggestions") or []
    )
    try:
        result = review_page.build_listing(
            analysis,
            document_id=document_id,
            read_source=read.source,
            ga_source=READ_SOURCE_GA,
            fields=fields,
            page_size=page_size,
            page_token=page_token,
            author=author,
            start_index=start_index,
            end_index=end_index,
            status=status,
            segment_id=segment_id,
            tab_id=tab_id,
        )
    except (PageTokenError, ValueError) as error:
        raise UserInputError(str(error)) from error
    result["read_source"] = read.source
    result["tabs"] = read.tab_metadata
    if read.degraded_reason:
        result["degraded_reason"] = read.degraded_reason
    # ``summary`` exists to be small, so it is serialized compactly; ``full``
    # keeps the indented form callers already parse. Both are the same JSON.
    if result["fields"] == review_page.FIELDS_SUMMARY:
        return json.dumps(result, separators=(",", ":"), ensure_ascii=False)
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
    fields: str = "text",
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    include_comments: bool = True,
) -> str:
    """Read a document the way a reviewer sees it: plain text with inline
    suggestion markers, optionally a paragraph map, and the comment threads.

    In SUGGESTIONS_INLINE view (default), pending insertions render as
    ``{+text+}`` and pending deletions as ``{-text-}`` (CriticMarkup style).
    PREVIEW_SUGGESTIONS_ACCEPTED / PREVIEW_WITHOUT_SUGGESTIONS return the
    respective clean text.

    **fields='text' is the default, deliberately.** The paragraph map's
    ``text`` values concatenate to exactly ``body_text``, so returning both
    restates a quarter of the response: measured on a 1,626-word article with
    120 pending suggestions, the response was 54,901 characters of which the
    paragraph map was 26,269 and ``body_text`` 13,462, and a real client
    spilled that response to a file rather than delivering it. So the default
    returns the readable half.

    - ``fields='text'``: ``body_text`` plus header/footer/footnote texts.
    - ``fields='paragraphs'``: the addressable half -- one entry per
      paragraph with segment, tab id, start/end index, text, named style,
      list flag, table flag and the suggestion ids touching it. Same
      characters, no ``body_text``. This is what you read to locate a section
      by index.
    - ``fields='full'``: both, the shape this tool has always returned.

    ``start_index``/``end_index`` narrow the response to the paragraphs
    overlapping that half-open UTF-16 range ``[start_index, end_index)`` --
    the way to read one section of a long document. ``body_text`` AND the
    header/footer/footnote texts are then recomputed from exactly the
    paragraphs returned, so no part of the response describes a different
    window than another, and ``suggestion_ids`` lists only the ids inside it.
    A body window therefore reports ``headers: {}`` (they are outside it),
    and a header-scoped window reports ``body_text: ""`` beside the header
    text it selected. The window is read in ONE (tab, segment) -- the body of
    the document's only tab unless ``segment_id``/``tab_id`` say otherwise --
    because Docs numbers each tab and each header/footer/footnote from its
    own start, so a header paragraph numbered from 0 would otherwise fall
    inside every window taken off the body's first page. ``window.scope``
    reports the space that was used.

    ``segment_id``/``tab_id`` name that space and nothing else: passing
    either WITHOUT a start_index/end_index window does not filter anything,
    and the response says so in ``scope_note`` rather than looking scoped.
    Use ``list_document_suggestions`` when you want them as filters.

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
        fields (str): "text" (default), "paragraphs" or "full". See above.
        start_index (int): Only paragraphs overlapping the half-open
            [start_index, end_index) -- endIndex is exclusive, as everywhere
            else in the Docs API. Either bound may be given alone.
        end_index (int): See start_index.
        segment_id (str): Read the window in this header/footer/footnote
            segment instead of the body (take the value from a paragraph's
            own segment_id). Only meaningful with start_index/end_index;
            without them it is reported as ignored, not silently applied.
        tab_id (str): Read the window in this tab. Omit for a single-tab
            document -- it is resolved from the paragraphs; a document with
            more than one tab REFUSES a window without it rather than
            guessing. Only meaningful with start_index/end_index.
        include_comments (bool): Return the comment threads. Default True;
            pass False when you only want the prose.

    Returns:
        str: JSON with view_mode, read_source, tabs, fields, paragraph_count,
            returned_paragraph_count, the requested body_text and/or
            paragraph map, suggestion_ids, window (when narrowed),
            scope_note (when segment_id/tab_id was passed without a window),
            omitted_fields + notice (when narrowed), and comments.
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
    try:
        shaped = review_page.build_review_view(
            rendered,
            fields=fields,
            start_index=start_index,
            end_index=end_index,
            segment_id=segment_id,
            tab_id=tab_id,
        )
    except ValueError as error:
        raise UserInputError(str(error)) from error
    result = {
        "view_mode": view_mode,
        "read_source": read.source,
        "tabs": read.tab_metadata,
        **shaped,
        "comments": read.comments if include_comments else [],
    }
    if not include_comments:
        result["comments_omitted"] = len(read.comments)
    if read.degraded_reason:
        result["degraded_reason"] = read.degraded_reason
    return json.dumps(result, indent=2, ensure_ascii=False)
