"""Docs API adapter: :mod:`mockdocs.model` <-> ``documents.get`` /
``documents.batchUpdate`` payloads.

Three responsibilities:

1. **Unit conversion.** The model counts grapheme clusters; the API counts
   UTF-16 code units. Every index crossing this boundary is converted here.
   The mismatch is deliberate (spec §14): it is what exercises
   ``gdocs_preview/analysis.py``'s UTF-16 index discipline, so generators and
   fixtures include astral-plane emoji.
2. **Coordinate-space resolution.** An index is only half of an address: the
   API numbers every ``(tabId, segmentId)`` pair from its own start, so a
   request's index is meaningless until its tab and segment are known. Every
   index conversion below is therefore per-segment, against
   :meth:`mockdocs.model.MockDoc.resolve_segment`, and a request that names
   neither resolves to the default tab's body -- silently, as prod does.
3. **Request semantics.** ``writeControl.writeMode`` SUGGEST routes content
   edits through the SPEC §5 suggestion operations; EDIT mutates the base
   text. Preview-only request types are rejected with a 400-shaped
   ``HttpError`` when the backend simulates a non-enrolled caller.

Payload shapes follow ``docs/preview-api-reference.md``. Where that document
marks an item UNCERTAIN the code says so at the point of the assumption.

**proto3 omits defaults, and so does this module.** ``startIndex: 0`` is
never serialized by the real API: a header segment's only paragraph came back
as ``{"endIndex": 13, "paragraph": {...}}`` with no ``startIndex`` at all, and
its inner element likewise (verified 2026-07-31). Index 0 is only reachable in
a header, footer or footnote -- a body's first paragraph starts at 1 -- so the
absence is exactly the shape ``gdocs_preview.analysis._indexes`` has to read
as 0, and the mock must produce it or that code path is never exercised.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httplib2
from googleapiclient.errors import HttpError

from mockdocs.graphemes import split_graphemes, utf16_len
from mockdocs.model import (
    SEGMENT_CONTAINERS,
    Char,
    MockDoc,
    MockDocsError,
    Segment,
)

#: documents.get suggestionsViewMode -> model projection (brief's mapping).
VIEW_MODE_PROJECTIONS = {
    "SUGGESTIONS_INLINE": "display",
    "PREVIEW_WITHOUT_SUGGESTIONS": "original",
    "PREVIEW_SUGGESTIONS_ACCEPTED": "final",
    # The API's default when the parameter is omitted.
    "DEFAULT_FOR_CURRENT_ACCESS": "display",
}

#: Preview-only batchUpdate request types (docs/preview-api-reference.md).
#: A non-enrolled caller cannot even parse these fields.
PREVIEW_REQUEST_TYPES = frozenset(
    {
        "insertComment",
        "addCommentReply",
        "updateCommentPost",
        "deleteComment",
        "deleteCommentReply",
        "acceptSuggestion",
        "rejectSuggestion",
        "deleteSuggestion",
    }
)

#: Official "unsupported in suggest mode" list, from
#: developers.google.com/workspace/docs/api/how-tos/suggestions.
SUGGEST_UNSUPPORTED_OFFICIAL = frozenset(
    {
        "addDocumentTab",
        "createNamedRange",
        "deleteFooter",
        "deleteHeader",
        "deleteNamedRange",
        "deleteTab",
        "updateDocumentTabProperties",
        "updateTableColumnProperties",
    }
)

#: The preview thread operations act on threads, not content, so SUGGEST does
#: not apply to them. Overlay decision, NOT verified against the live API
#: (docs/preview-api-reference.md, "Additional exclusions").
SUGGEST_UNSUPPORTED = SUGGEST_UNSUPPORTED_OFFICIAL | PREVIEW_REQUEST_TYPES

#: Content request types the mock implements.
SUPPORTED_CONTENT_REQUESTS = frozenset(
    {"insertText", "deleteContentRange", "replaceAllText"}
)

#: Accepted and ignored: style suggestions are out of scope (SPEC §1/§12), but
#: GA tools emit these and must not blow up the mock.
IGNORED_REQUEST_TYPES = frozenset(
    {"updateTextStyle", "updateParagraphStyle", "createParagraphBullets"}
)

_DOCS_URI = "https://docs.googleapis.com/v1/documents/mock:batchUpdate"


# ---------------------------------------------------------------------------
# Error shapes
# ---------------------------------------------------------------------------


def http_error(status: int, message: str, reason: str = "badRequest") -> HttpError:
    """A real ``googleapiclient.errors.HttpError`` so that
    ``@handle_http_errors`` and ``preview_status.classify_preview_error``
    treat mock failures identically to live ones."""
    payload = {
        "error": {
            "code": status,
            "message": message,
            "status": "INVALID_ARGUMENT" if status == 400 else "FAILED_PRECONDITION",
            "errors": [{"message": message, "domain": "global", "reason": reason}],
        }
    }
    resp = httplib2.Response(
        {"status": status, "reason": "Bad Request", "content-type": "application/json"}
    )
    return HttpError(resp, json.dumps(payload).encode("utf-8"), uri=_DOCS_URI)


def not_enrolled_error(field: str, path: str) -> HttpError:
    """The proto-parse failure a non-enrolled caller sees: the preview field
    does not exist for them, so the request body fails to parse.

    Shape targets ``docs/preview-api-reference.md`` UNCERTAIN item #5 (error
    shapes for non-enrolled callers are undocumented) and is what
    ``preview_status._UNKNOWN_FIELD_MARKERS`` matches on.
    """
    return http_error(
        400,
        f"Invalid JSON payload received. Unknown name \"{field}\" at '{path}': "
        f"Cannot find field.",
        reason="invalid",
    )


# ---------------------------------------------------------------------------
# Index conversion (the whole point of the adapter)
# ---------------------------------------------------------------------------


def utf16_offsets(chars: list[Char], start: int = 1) -> list[int]:
    """UTF-16 offset of each char, plus a trailing end sentinel.

    The body of a real Google Doc starts at index 1 (index 0 is the leading
    section break) -- in every tab, not only the first -- hence ``start=1``.
    A header, footer or footnote is numbered from its own 0 and needs
    ``start=0``; :attr:`mockdocs.model.Segment.index_base` is the value to
    pass, and passing the wrong one is precisely the bug this mock exists to
    make visible.
    """
    offsets = [start]
    for c in chars:
        start += utf16_len(c.cp)
        offsets.append(start)
    return offsets


def segment_offsets(segment: Segment) -> list[int]:
    """:func:`utf16_offsets` for a segment, at that segment's own base."""
    return utf16_offsets(segment.chars, segment.index_base)


def to_grapheme_index(chars: list[Char], utf16_index: int, start: int = 1) -> int:
    """UTF-16 index (API space) -> grapheme index (model space).

    ``start`` is the segment's index base; the caller must know which segment
    the index came from, because the same number means a different character
    in each.

    Raises ``MockDocsError`` when the index is out of range or lands inside a
    surrogate pair / grapheme cluster -- the real API rejects such indexes
    too, and a tool that computed indexes with Python ``len()`` on an
    emoji-bearing document will land here.
    """
    offsets = utf16_offsets(chars, start)
    if utf16_index < offsets[0] or utf16_index > offsets[-1]:
        raise MockDocsError(
            f"Index {utf16_index} is out of bounds "
            f"[{offsets[0]}, {offsets[-1]}] for the document."
        )
    try:
        return offsets.index(utf16_index)
    except ValueError:
        raise MockDocsError(
            f"Index {utf16_index} does not fall on a character boundary "
            f"(UTF-16 code-unit indexes must not split a character)."
        ) from None


# ---------------------------------------------------------------------------
# documents.get payload
# ---------------------------------------------------------------------------


def _project(chars: list[Char], view_mode: str) -> list[Char]:
    """SPEC §3's three projections, applied to one segment's chars."""
    projection = VIEW_MODE_PROJECTIONS.get(view_mode)
    if projection is None:
        raise MockDocsError(f"Invalid value at 'suggestions_view_mode' ({view_mode})")
    if projection == "original":
        return [c for c in chars if not c.ins]
    if projection == "final":
        return [c for c in chars if not c.dels]
    return list(chars)


def _coalesce_runs(
    chars: list[Char], marked: bool
) -> list[tuple[str, frozenset, frozenset]]:
    """Adjacent chars with identical ``(ins, del)`` become one styled run --
    §4's coalescing rule, which is also how real payloads are chunked."""
    runs: list[tuple[list[str], frozenset, frozenset]] = []
    for c in chars:
        ins = frozenset(c.ins) if marked else frozenset()
        dels = frozenset(c.dels) if marked else frozenset()
        if runs and runs[-1][1] == ins and runs[-1][2] == dels:
            runs[-1][0].append(c.cp)
        else:
            runs.append(([c.cp], ins, dels))
    return [("".join(parts), ins, dels) for parts, ins, dels in runs]


def _paragraphs(chars: list[Char]) -> list[list[Char]]:
    """Split on ``'\\n'``, which belongs to the paragraph it terminates (as in
    real Docs payloads)."""
    paras: list[list[Char]] = []
    current: list[Char] = []
    for c in chars:
        current.append(c)
        if c.cp == "\n":
            paras.append(current)
            current = []
    if current:
        paras.append(current)
    return paras


def _author_block(author: str, me: Optional[str]) -> dict[str, Any]:
    return {
        "displayName": author,
        "me": author == me,
        "anonymous": False,
        "user": f"users/{author}",
    }


def suggestion_threads(doc: MockDoc, me: Optional[str] = None) -> list[dict[str, Any]]:
    """``SuggestionThread`` objects, one per live suggestion.

    Shape verified against the live API 2026-07-30: a suggestion's
    ``headPost`` carries ``postId``/``author``/``createTime``/``updateTime``
    and a ``suggestionAction`` but NO ``content`` (unlike a comment head
    post), and the thread carries ``status`` plus ``summaryText``. These
    objects appear ONLY in the tabs+comments read (see
    :func:`tabs_document_payload`) -- never in a plain ``documents.get``.
    """
    threads = []
    for sid in sorted(doc.registry):
        sug = doc.registry[sid]
        label = doc.label(sid)
        thread: dict[str, Any] = {
            "suggestionId": sid,
            "headPost": {
                "postId": f"{sid}.head",
                "author": _author_block(sug.author, me),
                "createTime": f"2026-07-30T00:00:{sug.created_at:02d}Z",
                "updateTime": f"2026-07-30T00:00:{sug.touched_at:02d}Z",
                "suggestionAction": "NO_SUGGESTION_ACTION_CHANGE",
            },
            "status": "OPEN",
            "summaryText": label["text"],
        }
        if sug.thread:
            thread["replies"] = [
                {
                    "postId": p.post_id,
                    "content": p.content,
                    "contentHtml": p.content,
                    "author": _author_block(p.author, me),
                    "createTime": f"2026-07-30T00:00:{p.created_at:02d}Z",
                    "updateTime": f"2026-07-30T00:00:{p.created_at:02d}Z",
                    "suggestionAction": "NO_SUGGESTION_ACTION_CHANGE",
                }
                for p in sug.thread
            ]
        threads.append(thread)
    return threads


def _indexed(node: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """``node`` with its index pair attached, **omitting a zero start**.

    proto3 does not serialize default values, so the real API never writes
    ``startIndex: 0``: a header's first paragraph arrives as
    ``{"endIndex": 13, "paragraph": …}`` (verified 2026-07-31). Emitting the
    absence rather than an explicit zero is what makes
    ``gdocs_preview.analysis._indexes`` -- which reads a missing start on an
    indexed node as 0 -- a code path the mock actually exercises.
    """
    out: dict[str, Any] = {}
    if start:
        out["startIndex"] = start
    out["endIndex"] = end
    out.update(node)
    return out


def _segment_content(
    segment: Segment, view_mode: str, chars: Optional[list[Char]] = None
) -> list[dict[str, Any]]:
    """One segment's ``content`` array for one view mode.

    All ``startIndex``/``endIndex`` values are UTF-16 code units, converted
    from the grapheme model here and nowhere else, and they start at the
    segment's own base: **1 for a body** (index 0 is the leading section
    break, in every tab) and **0 for a header, footer or footnote**. Two
    segments therefore hand out the same numbers for different characters,
    which is the whole reason this function takes a segment rather than a
    document.

    A body opens with the leading ``sectionBreak``; a non-body segment does
    not have one -- verified 2026-07-31, where a header's content was a bare
    one-paragraph array starting at index 0.
    """
    if chars is None:
        chars = segment.chars
    chars = _project(chars, view_mode)
    marked = VIEW_MODE_PROJECTIONS[view_mode] == "display"

    content: list[dict[str, Any]] = []
    index = segment.index_base
    if segment.is_body:
        # Real body payloads open with a sectionBreak carrying no startIndex.
        content.append({"endIndex": 1, "sectionBreak": {"sectionStyle": {}}})
    for para_chars in _paragraphs(chars):
        elements = []
        para_start = index
        for text, ins, dels in _coalesce_runs(para_chars, marked):
            end = index + utf16_len(text)
            text_run: dict[str, Any] = {"content": text, "textStyle": {}}
            if ins:
                text_run["suggestedInsertionIds"] = sorted(ins)
            if dels:
                text_run["suggestedDeletionIds"] = sorted(dels)
            elements.append(_indexed({"textRun": text_run}, index, end))
            index = end
        content.append(
            _indexed(
                {
                    "paragraph": {
                        "elements": elements,
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    }
                },
                para_start,
                index,
            )
        )
    return content


def _non_body_segments(doc: MockDoc, tab_id: str, view_mode: str) -> dict[str, Any]:
    """``headers``/``footers``/``footnotes`` for one tab.

    Shape per ``docs/preview-api-reference.md``: each is a dict keyed by
    segment id whose value repeats the id under ``headerId``/``footerId``/
    ``footnoteId`` alongside ``content``. Absent keys rather than empty dicts,
    because proto3 omits empty maps and so does the real payload -- a document
    with no header has no ``headers`` key at all.
    """
    out: dict[str, Any] = {}
    for kind, (container, id_field) in SEGMENT_CONTAINERS.items():
        segments = doc.tab_segments(tab_id, kind)
        if not segments:
            continue
        out[container] = {
            segment.segment_id: {
                id_field: segment.segment_id,
                "content": _segment_content(segment, view_mode),
            }
            for segment in segments
        }
    return out


def document_payload(
    doc: MockDoc,
    view_mode: str = "SUGGESTIONS_INLINE",
    me: Optional[str] = None,
) -> dict[str, Any]:
    """Render a plain ``documents.get`` Document payload for one view mode.

    **The default tab only.** ``includeTabsContent=false`` is Google's
    backwards-compatibility read: the first tab's content is flattened onto
    the top level as ``body``/``headers``/``footers``/``footnotes`` and every
    other tab is simply not in the response. That is a lossy read, and the
    degraded-read path in ``gdocs_preview`` depends on it being lossy in
    exactly this way -- a mock that quietly returned all tabs here would make
    the fallback look safe.

    Deliberately carries NO thread objects: verified 2026-07-30 that the
    real plain read returns only
    ``body``/``documentStyle``/``namedStyles``/``title``/``revisionId``/
    ``documentId``/``suggestionsViewMode``/``commentsViewMode``, with
    ``commentsViewMode = COMMENTS_VIEW_MODE_OMITTED``. Comment and
    suggestion threads live in :func:`tabs_document_payload`.

    ``me`` is unused here and kept for signature symmetry with the tabs
    payload (authors only exist on threads).
    """
    tab_id = doc.default_tab_id
    body = doc.segment((tab_id, None))
    return {
        "documentId": doc.document_id,
        "title": doc.title,
        "body": {"content": _segment_content(body, view_mode)},
        **_non_body_segments(doc, tab_id, view_mode),
        "documentStyle": {},
        "namedStyles": {"styles": []},
        "revisionId": f"rev-{doc._clock}",
        "suggestionsViewMode": view_mode,
        "commentsViewMode": "COMMENTS_VIEW_MODE_OMITTED",
    }


def tabs_document_payload(
    doc: MockDoc,
    view_mode: str = "SUGGESTIONS_INLINE",
    me: Optional[str] = None,
    comments: Optional[list[dict[str, Any]]] = None,
    include_comments: bool = True,
) -> dict[str, Any]:
    """Render the ``includeTabsContent=true`` Document payload.

    Verified against the live API 2026-07-30/31: requesting tabs content moves
    the content out of the top-level ``body`` into
    ``tabs[i].documentTab.body`` (byte-identical, same indexes), and the
    top level gains ``tabs``. Each entry carries ``tabProperties``
    ``{tabId, title, index}`` and non-body segments under
    ``tabs[i].documentTab.headers`` / ``.footers`` / ``.footnotes``.
    ``suggestions`` and ``comments`` appear only when
    ``commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED`` is also sent -- with
    ``includeTabsContent`` alone the response says
    ``commentsViewMode: COMMENTS_VIEW_MODE_OMITTED`` and carries neither.

    ``suggestions``/``comments`` stay at the TOP level even in a multi-tab
    document: suggestion ids and comment threads are document-wide, not
    per-tab (verified 2026-07-31).

    **Every tab's body starts at index 1, and the numbers repeat.** A two-tab
    document with a suggestion near the top of each reports
    ``start_index: 1`` for both, which is why nothing downstream may compare
    or dedupe indexes without their tab.
    """
    payload: dict[str, Any] = {
        "documentId": doc.document_id,
        "title": doc.title,
        "revisionId": f"rev-{doc._clock}",
        "suggestionsViewMode": view_mode,
        "commentsViewMode": (
            "COMMENTS_VIEW_MODE_INCLUDED"
            if include_comments
            else "COMMENTS_VIEW_MODE_OMITTED"
        ),
        "tabs": [
            {
                "tabProperties": {
                    "tabId": tab.tab_id,
                    "title": tab.title,
                    "index": tab.index,
                },
                "documentTab": {
                    "body": {
                        "content": _segment_content(
                            doc.segment((tab.tab_id, None)), view_mode
                        )
                    },
                    **_non_body_segments(doc, tab.tab_id, view_mode),
                    "documentStyle": {},
                    "namedStyles": {"styles": []},
                },
            }
            for tab in doc.tabs
        ],
    }
    if include_comments:
        # Proto3 JSON omits empty repeated fields, and so does the real API:
        # a document with suggestions but no comments comes back with no
        # ``comments`` key at all.
        threads = suggestion_threads(doc, me)
        if threads:
            payload["suggestions"] = threads
        if comments:
            payload["comments"] = list(comments)
    return payload


# ---------------------------------------------------------------------------
# batchUpdate
# ---------------------------------------------------------------------------

_EMPTY_SUGGESTION_RESPONSE = {
    "createdSuggestionIds": [],
    "updatedSummarySuggestionIds": [],
    "deletedSuggestionIds": [],
    "acceptedSuggestionIds": [],
    "rejectedSuggestionIds": [],
}


def _suggestion_response(**kwargs: list[str]) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in _EMPTY_SUGGESTION_RESPONSE.items()}
    out.update({k: list(v) for k, v in kwargs.items()})
    return out


def _plain_quote(segment: Segment, start: int, end: int) -> str:
    return MockDoc.text_of(segment.chars[start:end])


class BatchUpdateApplier:
    """Applies one ``documents.batchUpdate`` body to a :class:`MockDoc`.

    Holds the per-batch response accumulators: ``suggestionResponses`` maps
    1:1 with the request list, ``replies`` carries the per-request response
    union members, and ``commentUpdateState`` summarises thread updates.
    """

    def __init__(self, backend: Any, doc: MockDoc) -> None:
        self.backend = backend
        self.doc = doc
        self.author = backend.me
        self.suggestion_responses: list[dict[str, list[str]]] = []
        self.replies: list[dict[str, Any]] = []
        self.comment_updates = 0
        self._merge_watermark = len(doc.merge_log)

    def _resolve_merges(self) -> None:
        """Rewrite ids that a later request in the same batch merged away.

        A SUGGEST-mode replacement is two requests (delete, then insert); §6
        merges the two suggestions, so request 0's ``createdSuggestionIds``
        would otherwise name an id that no longer exists by the time the
        caller reads the response -- and a tool that fed it straight back to
        acceptSuggestion would get a 400.

        UNCERTAIN: whether the live API renames or reports the pre-merge id
        is unverified (it is downstream of the also-unverified question of
        whether it merges at all -- spec §14). Reporting only live ids is the
        defensible reading of "suggestions affected by each update", and it
        avoids the mock manufacturing an ergonomics bug that may not exist.
        """
        rename: dict[str, str] = {}
        for survivor, absorbed in self.doc.merge_log[self._merge_watermark :]:
            rename[absorbed] = survivor
        if not rename:
            return

        def resolve(sid: str) -> str:
            seen = {sid}
            while sid in rename:
                sid = rename[sid]
                if sid in seen:  # pragma: no cover - defensive
                    break
                seen.add(sid)
            return sid

        for response in self.suggestion_responses:
            for key, ids in response.items():
                deduped: list[str] = []
                for sid in (resolve(s) for s in ids):
                    if sid not in deduped:
                        deduped.append(sid)
                response[key] = deduped

    # -- entry point -----------------------------------------------------
    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        requests = body.get("requests") or []
        write_control = body.get("writeControl") or {}
        write_mode = write_control.get("writeMode") or "WRITE_MODE_UNSPECIFIED"
        suggest = write_mode == "SUGGEST"

        if self.backend.not_enrolled:
            if suggest:
                raise not_enrolled_error("writeMode", "write_control")
            for i, request in enumerate(requests):
                for key in request:
                    if key in PREVIEW_REQUEST_TYPES:
                        raise not_enrolled_error(key, f"requests[{i}]")

        if write_mode not in ("SUGGEST", "EDIT", "WRITE_MODE_UNSPECIFIED"):
            raise http_error(
                400, f"Invalid value at 'write_control.write_mode' ({write_mode})"
            )

        for i, request in enumerate(requests):
            if len(request) != 1:
                raise http_error(
                    400, f"Invalid requests[{i}]: exactly one request type is required."
                )
            ((kind, payload),) = request.items()
            if suggest and kind in SUGGEST_UNSUPPORTED:
                raise http_error(
                    400,
                    f"Invalid requests[{i}].{kind}: request type {kind} is not "
                    f"supported in SUGGEST write mode.",
                )
            self._dispatch(i, kind, payload, suggest)

        self._resolve_merges()

        if self.comment_updates == 0:
            comment_update_state = "NO_UPDATES_REQUESTED"
        elif self.backend.fail_comment_updates:
            # Preview docs: thread updates can fail to save even when text
            # mutations in the same batch commit (partial failure).
            comment_update_state = "ALL_FAILED_UNKNOWN_REASON"
        else:
            comment_update_state = "ALL_SAVED"

        return {
            "documentId": self.doc.document_id,
            "replies": self.replies,
            "suggestionResponses": self.suggestion_responses,
            "commentUpdateState": comment_update_state,
            "writeControl": {"requiredRevisionId": f"rev-{self.doc._clock}"},
        }

    def _dispatch(self, i: int, kind: str, payload: dict, suggest: bool) -> None:
        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            if kind in IGNORED_REQUEST_TYPES:
                self._record(None, {})
                return
            raise http_error(
                400,
                f'Invalid JSON payload received. Unknown name "{kind}" at '
                f"'requests[{i}]': Cannot find field.",
                reason="invalid",
            )
        handler(i, payload, suggest)

    def _record(
        self,
        suggestion_response: Optional[dict[str, list[str]]],
        reply: dict[str, Any],
    ) -> None:
        self.suggestion_responses.append(
            suggestion_response
            if suggestion_response is not None
            else _suggestion_response()
        )
        self.replies.append(reply)

    # -- content requests ------------------------------------------------
    def _do_insertText(self, i: int, payload: dict, suggest: bool) -> None:
        text = payload.get("text")
        if not text:
            raise http_error(
                400, f"Invalid requests[{i}].insertText: text is required."
            )
        location = payload.get("location") or payload.get("endOfSegmentLocation") or {}
        segment = self._segment(location)
        if "index" in location:
            index = self._grapheme_index(segment, location["index"])
        else:  # endOfSegmentLocation: the end of THAT segment, not the body's
            index = len(segment.chars)
        if suggest:
            sid = self.doc.insert(index, text, self.author, segment.key)
            self._record(
                _suggestion_response(createdSuggestionIds=[sid] if sid else []), {}
            )
        else:
            self._edit_insert(segment, index, text)
            self._record(None, {})

    def _do_deleteContentRange(self, i: int, payload: dict, suggest: bool) -> None:
        range_ = payload.get("range") or {}
        segment = self._segment(range_)
        start, end = self._grapheme_range(segment, range_, i)
        if suggest:
            sid = self.doc.delete(start, end, self.author, segment.key)
            self._record(
                _suggestion_response(createdSuggestionIds=[sid] if sid else []), {}
            )
        else:
            self._edit_delete(segment, start, end)
            self._record(None, {})

    def _do_replaceAllText(self, i: int, payload: dict, suggest: bool) -> None:
        contains = (payload.get("containsText") or {}).get("text") or ""
        replacement = payload.get("replaceText", "")
        if not contains:
            raise http_error(
                400, f"Invalid requests[{i}].replaceAllText: containsText is required."
            )
        # ``replaceAllText`` names no index and no segment: it is the one
        # content request whose scope is the whole document, so it sweeps
        # every segment of every tab. (The real request narrows with
        # ``tabsCriteria``; unset means all tabs, which is what this is.)
        needle = split_graphemes(contains)
        created: list[str] = []
        for segment in self.doc.ordered_segments():
            # Search the display text (the SUGGESTIONS_INLINE coordinate
            # space), right to left so earlier match offsets stay valid. Both
            # sides are grapheme-clustered so a match never splits a cluster.
            haystack = [c.cp for c in segment.chars]
            hits = [
                n
                for n in range(len(haystack) - len(needle), -1, -1)
                if haystack[n : n + len(needle)] == needle
            ]
            for n in hits:
                if suggest:
                    sid = self.doc.replace(
                        n, n + len(needle), replacement, self.author, segment.key
                    )
                    if sid:
                        created.append(sid)
                else:
                    self._edit_delete(segment, n, n + len(needle))
                    if replacement:
                        self._edit_insert(segment, n, replacement)
        self._record(_suggestion_response(createdSuggestionIds=created), {})

    def _edit_insert(self, segment: Segment, index: int, text: str) -> None:
        segment.chars[index:index] = [Char(cp) for cp in split_graphemes(text)]

    def _edit_delete(self, segment: Segment, start: int, end: int) -> None:
        del segment.chars[start:end]
        self.doc._gc()

    # -- suggestion resolution -------------------------------------------
    def _do_acceptSuggestion(self, i: int, payload: dict, suggest: bool) -> None:
        sid = payload.get("suggestionId")
        self._require_suggestion(i, "acceptSuggestion", sid)
        self.doc.accept(sid)
        self._record(_suggestion_response(acceptedSuggestionIds=[sid]), {})

    def _do_rejectSuggestion(self, i: int, payload: dict, suggest: bool) -> None:
        sid = payload.get("suggestionId")
        self._require_suggestion(i, "rejectSuggestion", sid)
        self.doc.reject(sid)
        self._record(_suggestion_response(rejectedSuggestionIds=[sid]), {})

    def _do_deleteSuggestion(self, i: int, payload: dict, suggest: bool) -> None:
        sid = payload.get("suggestionId")
        self._require_suggestion(i, "deleteSuggestion", sid)
        # 403 if the requester is not the suggestion's author.
        if self.doc.registry[sid].author != self.author:
            raise http_error(
                403,
                f"The caller does not own suggestion {sid} and cannot delete it.",
                reason="forbidden",
            )
        # Deleting a suggestion discards the proposal: same effect as reject.
        self.doc.reject(sid)
        self._record(_suggestion_response(deletedSuggestionIds=[sid]), {})

    def _require_suggestion(self, i: int, kind: str, sid: Optional[str]) -> None:
        if not sid:
            raise http_error(
                400, f"Invalid requests[{i}].{kind}: suggestionId is required."
            )
        if sid not in self.doc.registry:
            raise http_error(
                400,
                f"Invalid requests[{i}].{kind}: the suggestion ID {sid} is invalid "
                f"or the suggestion no longer exists.",
            )

    # -- comment / suggestion threads ------------------------------------
    def _do_insertComment(self, i: int, payload: dict, suggest: bool) -> None:
        content = payload.get("content") or ""
        if not content:
            raise http_error(
                400, f"Invalid requests[{i}].insertComment: content is required."
            )
        anchor = payload.get("range")
        quote = ""
        if anchor:
            segment = self._segment(anchor)
            start, end = self._grapheme_range(segment, anchor, i)
            quote = _plain_quote(segment, start, end)
        thread = self.backend.create_comment_thread(
            self.doc.document_id,
            content=content,
            quote=quote,
            assignee=payload.get("assigneeEmailAddress"),
        )
        self.comment_updates += 1
        # UNCERTAIN #3: whether the Response union actually gains an
        # insertComment member, and its exact name, was not transcribed.
        # write_tools.py reads replies[i].insertComment.commentThread, so the
        # mock emits that shape; e2e records reality once enrolled.
        self._record(None, {"insertComment": {"commentThread": thread}})

    def _do_addCommentReply(self, i: int, payload: dict, suggest: bool) -> None:
        post = payload.get("post") or {}
        comment_id = payload.get("commentId")
        suggestion_id = payload.get("suggestionId")
        if (comment_id is None) == (suggestion_id is None):
            raise http_error(
                400,
                f"Invalid requests[{i}].addCommentReply: exactly one of commentId "
                f"or suggestionId must be set.",
            )
        content = post.get("content") or ""
        comment_action = post.get("commentAction")
        suggestion_action = post.get("suggestionAction")
        if not content and comment_action not in ("RESOLVE", "REOPEN"):
            raise http_error(
                400,
                f"Invalid requests[{i}].addCommentReply: post.content may be empty "
                f"only when commentAction is RESOLVE or REOPEN.",
            )

        accepted: list[str] = []
        rejected: list[str] = []
        if suggestion_id is not None:
            self._require_suggestion(i, "addCommentReply", suggestion_id)
            new_post = self.backend.add_suggestion_reply(
                self.doc, suggestion_id, content
            )
            if suggestion_action == "ACCEPT":
                self.doc.accept(suggestion_id)
                accepted.append(suggestion_id)
            elif suggestion_action == "REJECT":
                self.doc.reject(suggestion_id)
                rejected.append(suggestion_id)
        else:
            new_post = self.backend.add_comment_reply(
                self.doc.document_id,
                comment_id,
                content,
                comment_action,
                request_index=i,
            )
        self.comment_updates += 1
        self._record(
            _suggestion_response(
                acceptedSuggestionIds=accepted, rejectedSuggestionIds=rejected
            ),
            {"addCommentReply": {"post": new_post}},
        )

    def _do_updateCommentPost(self, i: int, payload: dict, suggest: bool) -> None:
        post_id = payload.get("postId")
        content = payload.get("content") or ""
        comment_id = payload.get("commentId")
        suggestion_id = payload.get("suggestionId")
        if (comment_id is None) == (suggestion_id is None):
            raise http_error(
                400,
                f"Invalid requests[{i}].updateCommentPost: exactly one of commentId "
                f"or suggestionId must be set.",
            )
        if not content:
            raise http_error(
                400, f"Invalid requests[{i}].updateCommentPost: content is required."
            )
        self.backend.update_post(
            self.doc, comment_id, suggestion_id, post_id, content, request_index=i
        )
        self.comment_updates += 1
        self._record(None, {})

    def _do_deleteComment(self, i: int, payload: dict, suggest: bool) -> None:
        comment_id = payload.get("commentId")
        self.backend.delete_comment_thread(
            self.doc.document_id, comment_id, request_index=i
        )
        self.comment_updates += 1
        self._record(None, {})

    def _do_deleteCommentReply(self, i: int, payload: dict, suggest: bool) -> None:
        self.backend.delete_reply(
            self.doc,
            payload.get("commentId"),
            payload.get("suggestionId"),
            payload.get("postId"),
            request_index=i,
        )
        self.comment_updates += 1
        self._record(None, {})

    # -- index helpers ---------------------------------------------------
    def _segment(self, located: dict) -> Segment:
        """The segment a ``Location``/``Range``/``EndOfSegmentLocation`` names.

        **Both fields are optional, and both default silently.** An omitted
        ``segmentId`` means the tab's body; an omitted ``tabId`` means the
        default tab. That is the API's own behaviour and the exact shape of
        the bug this mock was extended to catch: a caller that forgot to carry
        the tab and segment alongside an index gets a *successful* write into
        the default tab's body, at a numerically valid index, silently
        corrupting a document it never read. Nothing 400s.
        """
        try:
            return self.doc.resolve_segment(
                tab_id=located.get("tabId"), segment_id=located.get("segmentId")
            )
        except MockDocsError as exc:
            raise http_error(400, str(exc)) from None

    def _grapheme_index(self, segment: Segment, utf16_index: int) -> int:
        try:
            return to_grapheme_index(segment.chars, utf16_index, segment.index_base)
        except MockDocsError as exc:
            raise http_error(400, str(exc)) from None

    def _grapheme_range(
        self, segment: Segment, range_: dict, i: int
    ) -> tuple[int, int]:
        start = range_.get("startIndex")
        end = range_.get("endIndex")
        # proto3 omits a zero, so a Range over a header's first characters
        # arrives as ``{"endIndex": 4, "segmentId": …}`` with no startIndex.
        # Reading that as "missing" would 400 on a request the API accepts.
        # The same reading against a body yields start 0, which is the section
        # break and out of bounds -- rejected below, by the bounds check that
        # already knows the segment, rather than by a special case here.
        if start is None and end is not None:
            start = 0
        if start is None or end is None:
            raise http_error(
                400, f"Invalid requests[{i}]: range requires startIndex and endIndex."
            )
        if end <= start:
            raise http_error(
                400,
                f"Invalid requests[{i}]: endIndex ({end}) must be greater than "
                f"startIndex ({start}).",
            )
        return (
            self._grapheme_index(segment, start),
            self._grapheme_index(segment, end),
        )


def apply_batch_update(
    backend: Any, doc: MockDoc, body: dict[str, Any]
) -> dict[str, Any]:
    """Apply a ``documents.batchUpdate`` body; see :class:`BatchUpdateApplier`."""
    return BatchUpdateApplier(backend, doc).apply(body)
