"""Docs API adapter: :mod:`mockdocs.model` <-> ``documents.get`` /
``documents.batchUpdate`` payloads.

Two responsibilities:

1. **Unit conversion.** The model counts grapheme clusters; the API counts
   UTF-16 code units. Every index crossing this boundary is converted here.
   The mismatch is deliberate (spec §14): it is what exercises
   ``gdocs_preview/analysis.py``'s UTF-16 index discipline, so generators and
   fixtures include astral-plane emoji.
2. **Request semantics.** ``writeControl.writeMode`` SUGGEST routes content
   edits through the SPEC §5 suggestion operations; EDIT mutates the base
   text. Preview-only request types are rejected with a 400-shaped
   ``HttpError`` when the backend simulates a non-enrolled caller.

Payload shapes follow ``docs/preview-api-reference.md``. Where that document
marks an item UNCERTAIN the code says so at the point of the assumption.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httplib2
from googleapiclient.errors import HttpError

from mockdocs.graphemes import split_graphemes, utf16_len
from mockdocs.model import Char, MockDoc, MockDocsError

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
    section break), hence ``start=1``.
    """
    offsets = [start]
    for c in chars:
        start += utf16_len(c.cp)
        offsets.append(start)
    return offsets


def to_grapheme_index(chars: list[Char], utf16_index: int, start: int = 1) -> int:
    """UTF-16 index (API space) -> grapheme index (model space).

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


def _project(doc: MockDoc, view_mode: str) -> list[Char]:
    projection = VIEW_MODE_PROJECTIONS.get(view_mode)
    if projection is None:
        raise MockDocsError(f"Invalid value at 'suggestions_view_mode' ({view_mode})")
    return {
        "display": doc.display,
        "original": doc.original,
        "final": doc.final,
    }[projection]()


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


def _body_content(doc: MockDoc, view_mode: str) -> list[dict[str, Any]]:
    """The body ``content`` array for one view mode.

    All ``startIndex``/``endIndex`` values are UTF-16 code units, converted
    from the grapheme model here and nowhere else.
    """
    chars = _project(doc, view_mode)
    marked = VIEW_MODE_PROJECTIONS[view_mode] == "display"

    content: list[dict[str, Any]] = [
        # Real body payloads open with a sectionBreak carrying no startIndex.
        {"endIndex": 1, "sectionBreak": {"sectionStyle": {}}}
    ]
    index = 1
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
            elements.append({"startIndex": index, "endIndex": end, "textRun": text_run})
            index = end
        content.append(
            {
                "startIndex": para_start,
                "endIndex": index,
                "paragraph": {
                    "elements": elements,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            }
        )
    return content


def document_payload(
    doc: MockDoc,
    view_mode: str = "SUGGESTIONS_INLINE",
    me: Optional[str] = None,
) -> dict[str, Any]:
    """Render a plain ``documents.get`` Document payload for one view mode.

    Deliberately carries NO thread objects: verified 2026-07-30 that the
    real plain read returns only
    ``body``/``documentStyle``/``namedStyles``/``title``/``revisionId``/
    ``documentId``/``suggestionsViewMode``/``commentsViewMode``, with
    ``commentsViewMode = COMMENTS_VIEW_MODE_OMITTED``. Comment and
    suggestion threads live in :func:`tabs_document_payload`.

    ``me`` is unused here and kept for signature symmetry with the tabs
    payload (authors only exist on threads).
    """
    return {
        "documentId": doc.document_id,
        "title": doc.title,
        "body": {"content": _body_content(doc, view_mode)},
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

    Verified against the live API 2026-07-30: requesting tabs content moves
    the content out of the top-level ``body`` into
    ``tabs[i].documentTab.body`` (byte-identical, same indexes), and the
    top level gains ``tabs``. ``suggestions`` and ``comments`` appear only
    when ``commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED`` is also sent --
    with ``includeTabsContent`` alone the response says
    ``commentsViewMode: COMMENTS_VIEW_MODE_OMITTED`` and carries neither.

    The mock is single-tab (``t.0``): the model has no tab concept, and the
    multi-tab code path is covered by unit fixtures instead.
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
                "tabProperties": {"tabId": "t.0", "title": "Tab 1", "index": 0},
                "documentTab": {
                    "body": {"content": _body_content(doc, view_mode)},
                    "documentStyle": {},
                    "namedStyles": {"styles": []},
                },
            }
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


def _plain_quote(doc: MockDoc, start: int, end: int) -> str:
    return MockDoc.text_of(doc.chars[start:end])


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
        if "index" in location:
            index = self._grapheme_index(location["index"])
        else:  # endOfSegmentLocation
            index = len(self.doc.chars)
        if suggest:
            sid = self.doc.insert(index, text, self.author)
            self._record(
                _suggestion_response(createdSuggestionIds=[sid] if sid else []), {}
            )
        else:
            self._edit_insert(index, text)
            self._record(None, {})

    def _do_deleteContentRange(self, i: int, payload: dict, suggest: bool) -> None:
        start, end = self._grapheme_range(payload.get("range") or {}, i)
        if suggest:
            sid = self.doc.delete(start, end, self.author)
            self._record(
                _suggestion_response(createdSuggestionIds=[sid] if sid else []), {}
            )
        else:
            self._edit_delete(start, end)
            self._record(None, {})

    def _do_replaceAllText(self, i: int, payload: dict, suggest: bool) -> None:
        contains = (payload.get("containsText") or {}).get("text") or ""
        replacement = payload.get("replaceText", "")
        if not contains:
            raise http_error(
                400, f"Invalid requests[{i}].replaceAllText: containsText is required."
            )
        # Search the display text (the SUGGESTIONS_INLINE coordinate space),
        # right to left so earlier match offsets stay valid. Both sides are
        # grapheme-clustered so a match never splits a cluster.
        haystack = [c.cp for c in self.doc.chars]
        needle = split_graphemes(contains)
        hits = [
            n
            for n in range(len(haystack) - len(needle), -1, -1)
            if haystack[n : n + len(needle)] == needle
        ]
        created: list[str] = []
        for n in hits:
            if suggest:
                sid = self.doc.replace(n, n + len(needle), replacement, self.author)
                if sid:
                    created.append(sid)
            else:
                self._edit_delete(n, n + len(needle))
                if replacement:
                    self._edit_insert(n, replacement)
        self._record(_suggestion_response(createdSuggestionIds=created), {})

    def _edit_insert(self, index: int, text: str) -> None:
        self.doc.chars[index:index] = [Char(cp) for cp in split_graphemes(text)]

    def _edit_delete(self, start: int, end: int) -> None:
        del self.doc.chars[start:end]
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
            start, end = self._grapheme_range(anchor, i)
            quote = _plain_quote(self.doc, start, end)
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
    def _grapheme_index(self, utf16_index: int) -> int:
        try:
            return to_grapheme_index(self.doc.chars, utf16_index)
        except MockDocsError as exc:
            raise http_error(400, str(exc)) from None

    def _grapheme_range(self, range_: dict, i: int) -> tuple[int, int]:
        start = range_.get("startIndex")
        end = range_.get("endIndex")
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
        return self._grapheme_index(start), self._grapheme_index(end)


def apply_batch_update(
    backend: Any, doc: MockDoc, body: dict[str, Any]
) -> dict[str, Any]:
    """Apply a ``documents.batchUpdate`` body; see :class:`BatchUpdateApplier`."""
    return BatchUpdateApplier(backend, doc).apply(body)
