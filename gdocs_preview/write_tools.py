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

**Every write echoes a verifiable post-state.** 26 of 32 headless-agent runs
made writes and never read the document back
(``llmux/runner/reports/20260730-211540.md``, class
``no_end_state_verification``). The cause was on this side: the batchUpdate
response carries ids and nothing else, so "did my replacement do what I
meant" cost the agent a whole extra turn -- and it skipped it. Each tool now
answers that question inline, in a ``verification`` block. Where the echo is
free it is taken from the batchUpdate response; where it is not, ONE extra
read is made:

===========================  ==============================================
tool                         verification source
===========================  ==============================================
``suggest_doc_edit``         one post-write read (``verify``, default true)
``manage_document_suggestion``  one post-write read (``verify``, default true)
``reply_to_doc_thread``      free -- the response carries the whole Post
``create_anchored_doc_comment``  free -- the response carries the whole
                             CommentThread, ``plainTextQuote`` included
===========================  ==============================================

The two thread tools therefore have no ``verify`` parameter: there is no
extra call to switch off. Verification NEVER fails a landed write -- a read
that dies comes back as ``verification.source = "unavailable"``.

The same post-write read powers :mod:`gdocs_preview.suggestion_ledger`: its
before/after diff is how accept/reject reports the OTHER suggestions it
garbage-collected, and how a later "that id does not exist" error can name
the write that removed it.
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
from gdocs_preview import preview_status, suggestion_ledger
from gdocs_preview.analysis import (
    CONTEXT_WINDOW,
    extract_suggestions_from_tabs,
    render_tabs,
)
from gdocs_preview.preview_read import normalize_author, read_for_review

logger = logging.getLogger(__name__)

#: Echoed text is trimmed to this many characters. The verification block
#: lands in an LLM's context window on every single write, so it is a
#: receipt, not a debug dump.
ECHO_MAX_CHARS = 200

TRUNCATION_MARKER = "…"


def _doc_link(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _clip(text: Optional[str], limit: int = ECHO_MAX_CHARS) -> Optional[str]:
    """Trim for display, marking the cut. ``None`` stays ``None``."""
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + TRUNCATION_MARKER


def _echo_suggestion(record: dict[str, Any]) -> dict[str, Any]:
    """One analysis record, trimmed to what "did my edit land?" needs.

    ``pre_text``/``post_text`` carry :mod:`gdocs_preview.analysis`'s
    semantics unchanged -- the base text of the affected range, and that
    range with this suggestion (and only this one) applied -- which is
    exactly the before/after an agent would otherwise have to re-read for.
    """
    return {
        "suggestion_id": record.get("suggestion_id"),
        "type": record.get("type"),
        "pre_text": _clip(record.get("pre_text")),
        "post_text": _clip(record.get("post_text")),
        "context_before": record.get("context_before"),
        "context_after": record.get("context_after"),
        "start_index": record.get("start_index"),
        "end_index": record.get("end_index"),
        "summary_text": record.get("summary_text"),
        "status": record.get("status"),
    }


class _PostWriteRead:
    """The one read a write tool makes to verify itself.

    Not a dataclass because ``records`` and ``body_text`` are derived from
    the same payload and must not drift apart.
    """

    def __init__(self, read: Any) -> None:
        self.source: str = read.source
        analysed = extract_suggestions_from_tabs(read.tabs, read.threads)
        self.records: dict[str, dict[str, Any]] = {
            r["suggestion_id"]: r for r in analysed["suggestions"]
        }
        self.body_text: str = render_tabs(read.tabs)["body_text"]

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(self.records)


async def _post_write_read(
    service: Any, document_id: str
) -> tuple[Optional[_PostWriteRead], Optional[str]]:
    """Read the document back once, in SUGGESTIONS_INLINE view.

    Returns ``(read, None)`` or ``(None, reason)``. Failures are RETURNED,
    never raised: the write already landed, and a verification problem must
    not turn a successful mutation into an error the agent will try to
    "fix" by writing again. The broad ``except`` is deliberate for the same
    reason -- there is no failure mode here worth failing the tool over.
    """
    try:
        read = await read_for_review(service, document_id, "SUGGESTIONS_INLINE")
        return _PostWriteRead(read), None
    except Exception as error:  # noqa: BLE001 - see docstring
        logger.info(
            f"[docs_preview] post-write verification read failed for "
            f"{document_id}: {error}"
        )
        return None, f"{type(error).__name__}: {error}"[:200]


def _locate(body_text: str, anchor: Optional[str], expected: str) -> Optional[str]:
    """The post-write text around a resolved range, located by its context.

    ``anchor`` is the suggestion's ``context_before`` -- base text, so
    accepting or rejecting leaves it untouched and it still identifies the
    spot after the write. Returns a window starting at that anchor, or
    ``None`` when the anchor cannot be found (a concurrent edit, or a range
    at the very start of a segment with no preceding text).
    """
    if not body_text:
        return None
    if not anchor:
        return _clip(body_text[: CONTEXT_WINDOW + len(expected) + CONTEXT_WINDOW])
    position = body_text.find(anchor)
    if position < 0:
        return None
    end = position + len(anchor) + len(expected) + CONTEXT_WINDOW
    return _clip(body_text[position:end])


def _overlaps(record: dict[str, Any], edit_range: tuple[int, int]) -> bool:
    """Does a suggestion's index range touch the range an edit targeted?

    Bounds are inclusive on both sides so an insertion (a zero-width edit)
    at the seam of a suggestion still counts -- that is precisely the case
    where the API merges instead of creating a new suggestion.
    """
    start, end = record.get("start_index"), record.get("end_index")
    if start is None or end is None:
        return False
    edit_start, edit_end = edit_range
    return start <= edit_end and end >= edit_start


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


#: Message fragments (lowercased) that mean "the API resolved the request
#: type and could not find that suggestion". Both observed shapes are here:
#: the real API answers HTTP 404 ``Suggestion with ID <id> does not exist.``
#: (e2e/last_run.md, 2026-07-30), the mock a 400 ``the suggestion ID <id> is
#: invalid or the suggestion no longer exists.`` -- and neither says WHY.
_MISSING_SUGGESTION_MARKERS = ("suggestion with id", "suggestion id")


def _http_error_message(error: HttpError) -> tuple[Optional[int], str]:
    status = getattr(getattr(error, "resp", None), "status", None)
    content = getattr(error, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return status, f"{error} {content}"


def _missing_suggestion_error(
    error: HttpError,
    *,
    tool_name: str,
    user_google_email: str,
    document_id: str,
    suggestion_id: str,
) -> Optional[UserInputError]:
    """Turn "that suggestion does not exist" into "and here is why".

    Returns ``None`` for any other failure, so nothing else is swallowed.
    The cause comes from :func:`gdocs_preview.suggestion_ledger.explain_missing`,
    which only claims what it observed.
    """
    status, message = _http_error_message(error)
    if status not in (400, 404):
        return None
    lowered = message.lower()
    if not any(marker in lowered for marker in _MISSING_SUGGESTION_MARKERS):
        return None
    if suggestion_id and suggestion_id.lower() not in lowered:
        return None
    cause = suggestion_ledger.explain_missing(
        user_google_email, document_id, suggestion_id
    )
    return UserInputError(
        f"{tool_name}: suggestion {suggestion_id!r} no longer exists in "
        f"document {document_id}. {cause} (API said: "
        f"{' '.join(message.split())[:200]})"
    )


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
    verify: bool = True,
) -> str:
    """Create a suggested insertion, deletion, or replacement as a pending
    suggestion (SUGGEST write mode), and report the suggestion it created.

    The mode is inferred from the params: text only -> insertion at
    start_index; end_index only -> deletion of [start_index, end_index);
    both -> replacement (delete, then insert at start_index, in one
    batch). Indexes are UTF-16 code units in the SUGGESTIONS_INLINE
    coordinate space -- take them verbatim from
    list_document_suggestions / get_doc_review_view output.

    **An index is only half of an address.** Docs numbers each
    ``(tabId, segmentId)`` pair from its own start, so index 412 in a
    footnote and index 412 in the body are different places, and this tool
    defaults to ``segment_id=None``/``tab_id=None``, which means the body of
    the default tab. Every record from ``list_document_suggestions`` and
    every paragraph from ``get_doc_review_view`` carries ``segment``,
    ``segment_id`` and ``tab_id`` alongside its indexes: pass them back here
    unchanged. Taking a header's or footnote's index without its
    ``segment_id`` writes into the body at that number, silently.

    The edit lands
    as a *pending suggestion*: nothing is applied to the document until it
    is accepted; it is visible to list_document_suggestions and in the
    Docs UI.

    With verify=true (the default) the tool makes ONE extra read after the
    write and returns the created suggestion's computed pre/post text, its
    context windows and its resulting index range -- so "did my replacement
    do what I meant, at the place I meant?" is answerable from this
    response, without a follow-up list_document_suggestions call. Stale
    indexes surface here: a replacement whose pre_text is not the text you
    aimed at landed in the wrong place.

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
        verify (bool): Read the document back once and echo the created
            suggestion. Defaults to True; set False only to save the extra
            read in a batch of edits you will verify at the end.

    Returns:
        str: JSON with document_id, mode (insertion|deletion|replacement),
            created_suggestion_ids, requests_applied, link, and
            verification {source, read_source, created_suggestions
            [suggestion_id, type, pre_text, post_text, context_before,
            context_after, start_index, end_index, summary_text, status],
            pending_suggestion_count, and -- only when they apply --
            suggestions_at_edit_range (when the API reported no new id,
            which happens when the edit merged into an existing same-author
            suggestion), also_removed_suggestion_ids, and notes}.
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
    known_before = suggestion_ledger.known_ids(user_google_email, document_id)
    response = await _execute_preview_batch_update(
        service, "suggest_doc_edit", document_id, requests, write_mode="SUGGEST"
    )

    created_ids: list[str] = []
    for suggestion_response in response.get("suggestionResponses") or []:
        for sid in (suggestion_response or {}).get("createdSuggestionIds") or []:
            if sid not in created_ids:
                created_ids.append(sid)

    result: dict[str, Any] = {
        "document_id": document_id,
        "mode": mode,
        "created_suggestion_ids": created_ids,
        "requests_applied": len(requests),
        "verification": await _verify_suggest(
            service,
            user_google_email=user_google_email,
            document_id=document_id,
            created_ids=created_ids,
            known_before=known_before,
            edit_range=(
                start_index,
                end_index if end_index is not None else start_index,
            ),
            verify=verify,
        ),
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


async def _verify_suggest(
    service: Any,
    *,
    user_google_email: str,
    document_id: str,
    created_ids: list[str],
    known_before: Optional[frozenset[str]],
    edit_range: tuple[int, int],
    verify: bool,
) -> dict[str, Any]:
    """Post-write echo for :func:`suggest_doc_edit`.

    ``createdSuggestionIds`` is the primary join key, but it is not
    trustworthy on its own, so three sources are used in order:

    1. the reported created ids, intersected with what the read actually
       found (an id the response named may already be retired);
    2. anything new since the last read -- the diff answers even when the
       response says nothing;
    3. the suggestions overlapping the edited range, reported separately as
       ``suggestions_at_edit_range``.

    (3) is not a nicety. Verified against the real API 2026-07-30: editing
    inside an existing same-author suggestion MERGES into it (SPEC §6,
    previously UNCERTAIN) and the response then carries **no** created id at
    all. Without the range fallback that edit would echo nothing -- the
    exact "the write tool returns almost nothing to verify with" problem
    this module exists to fix. It is reported under its own key because
    overlap is a weaker claim than authorship: the range says where the
    suggestion is, not that this call made it.
    """
    if not verify:
        return {"source": "skipped", "reason": "verify=false"}
    read, failure = await _post_write_read(service, document_id)
    if read is None:
        return {"source": "unavailable", "reason": failure}

    echoed_ids = [sid for sid in created_ids if sid in read.records]
    if known_before is not None:
        for sid in read.records:
            if sid not in known_before and sid not in echoed_ids:
                echoed_ids.append(sid)

    verification: dict[str, Any] = {
        "source": "post_write_read",
        "read_source": read.source,
        "created_suggestions": [_echo_suggestion(read.records[s]) for s in echoed_ids],
        "pending_suggestion_count": len(read.records),
    }
    if not echoed_ids:
        overlapping = [
            _echo_suggestion(record)
            for record in read.records.values()
            if _overlaps(record, edit_range)
        ]
        if overlapping:
            verification["suggestions_at_edit_range"] = overlapping
            verification["notes"] = [
                "the API reported no new suggestion id for this edit; the "
                "suggestion(s) now covering the edited range are echoed "
                "instead -- editing inside an existing same-author "
                "suggestion merges into it rather than creating a new one."
            ]
    vanished = sorted(known_before - read.live_ids) if known_before is not None else []
    if vanished:
        resolutions = suggestion_ledger.record_resolution(
            user_google_email,
            document_id,
            "suggest_doc_edit",
            echoed_ids[0] if echoed_ids else "",
            vanished,
        )
        verification["also_removed_suggestion_ids"] = vanished
        verification.setdefault("notes", []).extend(
            suggestion_ledger.collateral_note(r) for r in resolutions if not r.direct
        )
    suggestion_ledger.observe(user_google_email, document_id, read.records.values())
    return verification


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
    verify: bool = True,
) -> str:
    """Accept or reject a pending suggestion by id, and report what changed
    -- including the OTHER suggestions the resolution removed.

    Resolving a suggestion deletes any other suggestion whose last marked
    character disappears with it (and that suggestion's comment thread).
    With verify=true (the default) the tool makes ONE extra read after the
    write and names those in
    ``verification.also_removed_suggestion_ids``, so the next call never has
    to discover them as an unexplained "that id does not exist" error. It
    also reports whether the target is really gone, the text the range now
    reads, and the ids still pending.

    Permission rules (preview API): accept requires edit access to the
    document; reject requires edit access OR being the suggestion's
    author. A nonexistent suggestion id may surface as an error OR as an
    HTTP 200 no-op carrying a commentUpdateState -- when the API sends
    that state it is included in the response JSON. When it errors, the
    message says whether one of your own earlier writes removed the id.

    Requires Google Workspace Developer Preview enrollment (verify with
    check_docs_review_capabilities probe=true).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document.
        action (str): One of "accept" or "reject".
        suggestion_id (str): The suggestion to act on (from
            list_document_suggestions).
        verify (bool): Read the document back once and echo the resulting
            state. Defaults to True; set False only to save the extra read
            when resolving a batch you will verify at the end -- collateral
            removals then go unreported.

    Returns:
        str: JSON with document_id, action, suggestion_id,
            accepted_suggestion_ids or rejected_suggestion_ids,
            comment_update_state (when sent), link, and verification
            {source, read_source, still_pending, resolved_suggestion,
            expected_text, resulting_text, matches_expectation,
            pending_suggestion_count, pending_suggestion_ids, and -- only
            when non-empty -- also_removed_suggestion_ids + notes}.
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
    # Snapshot BEFORE the write: the diff against the post-write read is the
    # only evidence that this resolution took other suggestions with it.
    known_before = suggestion_ledger.known_ids(user_google_email, document_id)
    resolved_record = suggestion_ledger.record_of(
        user_google_email, document_id, suggestion_id
    )
    try:
        response = await _execute_preview_batch_update(
            service, "manage_document_suggestion", document_id, requests
        )
    except HttpError as error:
        explained = _missing_suggestion_error(
            error,
            tool_name="manage_document_suggestion",
            user_google_email=user_google_email,
            document_id=document_id,
            suggestion_id=suggestion_id,
        )
        if explained is not None:
            raise explained from error
        raise

    affected_ids: list[str] = []
    for suggestion_response in response.get("suggestionResponses") or []:
        for sid in (suggestion_response or {}).get(response_field) or []:
            if sid not in affected_ids:
                affected_ids.append(sid)

    result: dict[str, Any] = {
        "document_id": document_id,
        "action": action_normalized,
        "suggestion_id": suggestion_id,
        result_key: affected_ids,
    }
    comment_update_state = response.get("commentUpdateState")
    if comment_update_state is not None:
        result["comment_update_state"] = comment_update_state
    result["verification"] = await _verify_resolution(
        service,
        user_google_email=user_google_email,
        document_id=document_id,
        action=action_normalized,
        suggestion_id=suggestion_id,
        resolved_record=resolved_record,
        known_before=known_before,
        verify=verify,
    )
    result["link"] = _doc_link(document_id)
    return json.dumps(result, indent=2, ensure_ascii=False)


async def _verify_resolution(
    service: Any,
    *,
    user_google_email: str,
    document_id: str,
    action: str,
    suggestion_id: str,
    resolved_record: Optional[dict[str, Any]],
    known_before: Optional[frozenset[str]],
    verify: bool,
) -> dict[str, Any]:
    """Post-write echo for :func:`manage_document_suggestion`.

    Three questions, answered from one read: is the target gone, does the
    document now read the way the suggestion promised, and what else
    disappeared. ``expected_text`` is the analysis layer's ``post_text``
    for an accept and ``pre_text`` for a reject -- the definition of what
    resolving that suggestion means -- taken from the last listing, so it is
    ``None`` when the caller resolved an id it never listed.

    ``matches_expectation`` reads the OTHER half when ``expected_text`` is
    empty: accepting a pure deletion (or rejecting a pure insertion) leaves
    nothing to find, so the check becomes "is the text that should be gone
    actually gone from that window". Both are scoped to the located window,
    never the whole document, so an identical word elsewhere cannot fake a
    verdict.
    """
    if not verify:
        # Still remember the resolution: a later "does not exist" for this id
        # must be explainable even when the caller opted out of the read.
        suggestion_ledger.record_resolution(
            user_google_email, document_id, action, suggestion_id
        )
        return {"source": "skipped", "reason": "verify=false"}

    read, failure = await _post_write_read(service, document_id)
    if read is None:
        suggestion_ledger.record_resolution(
            user_google_email, document_id, action, suggestion_id
        )
        return {"source": "unavailable", "reason": failure}

    expected_text: Optional[str] = None
    resulting_text: Optional[str] = None
    matches: Optional[bool] = None
    if resolved_record is not None:
        kept, dropped = (
            ("post_text", "pre_text")
            if action == "accept"
            else (
                "pre_text",
                "post_text",
            )
        )
        expected_text = resolved_record.get(kept)
        removed_text = resolved_record.get(dropped)
        resulting_text = _locate(
            read.body_text,
            resolved_record.get("context_before"),
            expected_text or removed_text or "",
        )
        if resulting_text is not None:
            if expected_text:
                matches = expected_text in resulting_text
            elif removed_text:
                matches = removed_text not in resulting_text

    verification: dict[str, Any] = {
        "source": "post_write_read",
        "read_source": read.source,
        "still_pending": suggestion_id in read.records,
        "resolved_suggestion": (
            _echo_suggestion(resolved_record) if resolved_record else None
        ),
        "expected_text": _clip(expected_text),
        "resulting_text": resulting_text,
        "matches_expectation": matches,
        "pending_suggestion_count": len(read.records),
        "pending_suggestion_ids": sorted(read.records),
    }

    collateral = (
        sorted((known_before - read.live_ids) - {suggestion_id})
        if known_before is not None
        else []
    )
    resolutions = suggestion_ledger.record_resolution(
        user_google_email, document_id, action, suggestion_id, collateral
    )
    if collateral:
        verification["also_removed_suggestion_ids"] = collateral
        verification["notes"] = [
            suggestion_ledger.collateral_note(r) for r in resolutions if not r.direct
        ]
    suggestion_ledger.observe(user_google_email, document_id, read.records.values())
    return verification


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

    Self-verifying at no extra cost: the batchUpdate response carries the
    whole stored Post, so the return echoes the content the API actually
    saved, its author and its post id, plus commentUpdateState -- no
    follow-up read is needed to confirm the reply landed. (An HTTP 200 that
    reports ALL_FAILED_UNKNOWN_REASON is raised as an error, never returned
    as success.)

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
            the API recorded it), content (as stored), create_time,
            comment_update_state, link, and verification {source:
            "batch_update_response", saved, stored_content,
            matches_request}.
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
    try:
        response = await _execute_preview_batch_update(
            service,
            "reply_to_doc_thread",
            document_id,
            requests,
            enforce_comment_update=True,
        )
    except HttpError as error:
        explained = (
            _missing_suggestion_error(
                error,
                tool_name="reply_to_doc_thread",
                user_google_email=user_google_email,
                document_id=document_id,
                suggestion_id=suggestion_id,
            )
            if suggestion_id is not None
            else None
        )
        if explained is not None:
            raise explained from error
        raise

    # Verified 2026-07-30 against the real API: the batchUpdate Response
    # union does carry an ``addCommentReply`` member holding the new Post,
    # author included (docs/preview-api-reference.md).
    post: dict[str, Any] = {}
    replies = response.get("replies") or []
    if replies:
        reply_payload = (replies[0] or {}).get("addCommentReply") or {}
        post = reply_payload.get("post") or {}

    stored_content = post.get("content")
    comment_update_state = response.get("commentUpdateState")
    result = {
        "document_id": document_id,
        "thread_type": thread_type,
        thread_id_key: thread_id,
        "post_id": post.get("postId"),
        "author": normalize_author(post.get("author")),
        "content": _clip(stored_content),
        "create_time": post.get("createTime"),
        "comment_update_state": comment_update_state,
        "verification": {
            # No extra read: the batchUpdate response already carries the
            # stored Post, so the echo costs nothing.
            "source": "batch_update_response",
            "saved": comment_update_state == "ALL_SAVED",
            "stored_content": _clip(stored_content),
            "matches_request": (
                stored_content == reply_content if stored_content is not None else None
            ),
        },
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

    Self-verifying at no extra cost: the batchUpdate response carries the
    whole stored CommentThread, so the return echoes quoted_text -- the text
    the comment ACTUALLY anchored to. Compare it with the text you meant to
    comment on; an off-by-one range shows up there immediately, without a
    follow-up read.

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
            thread head post's PostAuthor as the API recorded it), content
            (as stored), create_time, anchor_id, quoted_text, status,
            comment_update_state, link, and verification {source:
            "batch_update_response", saved, anchored_range, anchored_text,
            stored_content, matches_request}.
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

    quoted_text = thread.get("plainTextQuote")
    stored_content = head_post.get("content")
    comment_update_state = response.get("commentUpdateState")
    result = {
        "document_id": document_id,
        "comment_id": thread.get("commentId"),
        "post_id": head_post.get("postId"),
        "author": normalize_author(head_post.get("author")),
        "content": _clip(stored_content),
        "create_time": head_post.get("createTime"),
        "anchor_id": thread.get("anchorId"),
        "quoted_text": _clip(quoted_text),
        "status": thread.get("status"),
        "comment_update_state": comment_update_state,
        "verification": {
            # No extra read: InsertCommentResponse carries the CommentThread,
            # plainTextQuote included -- the anchored text for free.
            "source": "batch_update_response",
            "saved": comment_update_state == "ALL_SAVED",
            "anchored_range": {
                "start_index": start_index,
                "end_index": end_index,
                "segment_id": segment_id,
                "tab_id": tab_id,
            },
            "anchored_text": _clip(quoted_text),
            "stored_content": _clip(stored_content),
            "matches_request": (
                stored_content == content if stored_content is not None else None
            ),
        },
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
