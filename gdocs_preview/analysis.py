"""Pure analysis of Google Docs Document JSON for the curated review layer.

Everything in this module is side-effect free: Document payloads (as returned
by ``documents.get`` with ``suggestionsViewMode=SUGGESTIONS_INLINE``) in,
review-ready structures out. The MCP tools in
:mod:`gdocs_preview.curated_tools` are thin API-call wrappers around these
functions, which keeps the tricky logic testable on fixtures.

Index discipline (the classic Docs API bug source): the API counts indexes
in UTF-16 code units, while Python strings count code points. This module
therefore NEVER computes document indexes from Python string lengths -- all
``start_index``/``end_index`` values are passed through verbatim from the
payload (already UTF-16, already relative to the SUGGESTIONS_INLINE view,
i.e. directly usable for batchUpdate requests computed against that view).
Context windows are sliced on Python strings for display only and are never
fed back to the API.

One exception, and it is not a computation: **an absent ``startIndex`` means
0.** The API serializes proto3, which omits default values, so index 0 is
never written out. Verified against the live API 2026-07-31: a header
segment's only paragraph came back as ``{"endIndex": 13, "paragraph": ...}``
with no ``startIndex`` at all, and its element likewise. Index 0 is only
reachable in a header, footer or footnote (a body's first paragraph starts
at 1), so reading the absence as "no index" silently made every suggestion
at the start of one of those segments unaddressable -- ``start_index: null``,
excluded from every index-range filter, and nothing for a caller to hand
back to ``suggest_doc_edit``. The default is applied only where ``endIndex``
is present, i.e. only to elements the payload did index.

Pre/post semantics for a suggestion S:
  - ``pre_text``  = the base text of the affected range: ALL pending
    insertions stripped, ALL pending deletions kept (what
    PREVIEW_WITHOUT_SUGGESTIONS would show).
  - ``post_text`` = the base text with S (and only S) applied: S's
    insertions kept, S's deletions stripped; other suggestions remain
    pending (their insertions stay stripped, their deletions stay kept).
Context windows are computed on the base text, so neighbouring suggestions'
pending insertions never leak into context.

Known limitations (kept deliberately out of scope for the review MVP):
  - Row/column-level table structure suggestions (``suggestedInsertionIds``
    on TableRow) are not reported; text suggestions inside cells are.
  - Paragraph-level style suggestions (``suggestedParagraphStyleChanges``)
    are not reported; text-run style suggestions are.
  - A suggestion spanning multiple segments (theoretical) is analysed
    within the segment where it first appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from gdocs_preview.address import with_address

CONTEXT_WINDOW = 40

#: Placeholder for non-text inline content (images, footnote refs, ...),
#: mirroring the Unicode object-replacement character convention.
OBJECT_PLACEHOLDER = "￼"

_NON_TEXT_ELEMENT_KEYS = (
    "inlineObjectElement",
    "footnoteReference",
    "pageBreak",
    "columnBreak",
    "horizontalRule",
    "person",
    "richLink",
    "dateElement",
    "equation",
)


def utf16_len(s: str) -> int:
    """Length of ``s`` in UTF-16 code units (the Docs API index unit)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)


def _indexes(node: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """``(startIndex, endIndex)`` as the payload means them, not as it spells
    them.

    proto3 omits default values, so ``startIndex: 0`` is never serialized --
    see the module docstring. An indexed node (one with an ``endIndex``) that
    has no ``startIndex`` starts at 0; a node with neither is genuinely
    unindexed and stays that way.
    """
    end = node.get("endIndex")
    start = node.get("startIndex")
    if start is None and end is not None:
        start = 0
    return start, end


@dataclass
class _Run:
    """One suggestion-relevant leaf element in document order."""

    segment: str  # body | header | footer | footnote
    segment_id: Optional[str]
    start: Optional[int]
    end: Optional[int]
    text: str
    ins_ids: tuple[str, ...]
    del_ids: tuple[str, ...]
    style_ids: tuple[str, ...]
    in_table: bool
    base_start: int = 0  # Python-codepoint offset into the segment base text

    @property
    def in_base(self) -> bool:
        """Whether this run is part of the base (pre-suggestion) text."""
        return not self.ins_ids

    @property
    def base_len(self) -> int:
        return len(self.text) if self.in_base else 0


@dataclass
class _Paragraph:
    segment: str
    segment_id: Optional[str]
    start: Optional[int]
    end: Optional[int]
    named_style: Optional[str]
    is_list_item: bool
    in_table: bool
    runs: list[_Run]


@dataclass
class _Segment:
    segment: str
    segment_id: Optional[str]
    paragraphs: list[_Paragraph]
    runs: list[_Run]
    base_text: str


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------


def _run_from_paragraph_element(
    element: dict[str, Any], segment: str, segment_id: Optional[str], in_table: bool
) -> Optional[_Run]:
    payload = None
    text = None
    if "textRun" in element:
        payload = element["textRun"]
        text = payload.get("content", "")
    else:
        for key in _NON_TEXT_ELEMENT_KEYS:
            if key in element:
                payload = element[key]
                text = OBJECT_PLACEHOLDER
                break
    if payload is None:
        return None
    start, end = _indexes(element)
    return _Run(
        segment=segment,
        segment_id=segment_id,
        start=start,
        end=end,
        text=text or "",
        ins_ids=tuple(payload.get("suggestedInsertionIds") or ()),
        del_ids=tuple(payload.get("suggestedDeletionIds") or ()),
        style_ids=tuple(payload.get("suggestedTextStyleChanges") or ()),
        in_table=in_table,
    )


def _collect_paragraphs(
    content: list[dict[str, Any]],
    segment: str,
    segment_id: Optional[str],
    in_table: bool = False,
) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    for structural in content or []:
        if "paragraph" in structural:
            para = structural["paragraph"]
            runs = []
            for element in para.get("elements", []):
                r = _run_from_paragraph_element(element, segment, segment_id, in_table)
                if r is not None:
                    runs.append(r)
            style = para.get("paragraphStyle") or {}
            para_start, para_end = _indexes(structural)
            paragraphs.append(
                _Paragraph(
                    segment=segment,
                    segment_id=segment_id,
                    start=para_start,
                    end=para_end,
                    named_style=style.get("namedStyleType"),
                    is_list_item="bullet" in para,
                    in_table=in_table,
                    runs=runs,
                )
            )
        elif "table" in structural:
            for row in structural["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    paragraphs.extend(
                        _collect_paragraphs(
                            cell.get("content", []), segment, segment_id, in_table=True
                        )
                    )
        elif "tableOfContents" in structural:
            paragraphs.extend(
                _collect_paragraphs(
                    structural["tableOfContents"].get("content", []),
                    segment,
                    segment_id,
                    in_table,
                )
            )
        # sectionBreak: no suggestion-relevant content
    return paragraphs


def _collect_segments(document: dict[str, Any]) -> list[_Segment]:
    """All segments in stable order: body, then headers/footers/footnotes
    (each sorted by segment id)."""
    ordered: list[tuple[str, Optional[str], list[dict[str, Any]]]] = [
        ("body", None, (document.get("body") or {}).get("content", []))
    ]
    for field_name, segment_kind in (
        ("headers", "header"),
        ("footers", "footer"),
        ("footnotes", "footnote"),
    ):
        for seg_id in sorted(document.get(field_name) or {}):
            content = (document[field_name][seg_id] or {}).get("content", [])
            ordered.append((segment_kind, seg_id, content))

    segments = []
    for segment_kind, seg_id, content in ordered:
        paragraphs = _collect_paragraphs(content, segment_kind, seg_id)
        runs = [r for p in paragraphs for r in p.runs]
        offset = 0
        for r in runs:
            r.base_start = offset
            offset += r.base_len
        base_text = "".join(r.text for r in runs if r.in_base)
        segments.append(_Segment(segment_kind, seg_id, paragraphs, runs, base_text))
    return segments


# ---------------------------------------------------------------------------
# Suggestion extraction
# ---------------------------------------------------------------------------

_TYPE_BY_KINDS = {
    frozenset({"insertion"}): "insertion",
    frozenset({"deletion"}): "deletion",
    frozenset({"style"}): "style",
    frozenset({"insertion", "deletion"}): "replacement",
}


def _suggestion_type(kinds: frozenset[str]) -> str:
    return _TYPE_BY_KINDS.get(kinds, "mixed")


def extract_suggestions(
    document: dict[str, Any],
    *,
    threads: Optional[dict[str, dict[str, Any]]] = None,
    tab_id: Optional[str] = None,
) -> dict[str, Any]:
    """Analyse a SUGGESTIONS_INLINE Document payload into per-suggestion
    review records (see module docstring for pre/post semantics).

    ``threads`` is the suggestion-thread map from
    :func:`gdocs_preview.preview_read.suggestion_threads_by_id`, joined on
    ``suggestionId``. It carries what the document content cannot: author,
    status, create time, Google's own ``summaryText`` and the thread's
    replies. Without it (a GA read, or a caller not enrolled in the
    Developer Preview) every record reports ``author: null`` /
    ``author_source: "unavailable"`` -- never a guess.

    ``tab_id`` tags the records with the tab they were found in; ``None``
    for a single-tab or GA read.
    """
    segments = _collect_segments(document)
    threads = threads or {}

    order: list[str] = []
    kinds: dict[str, set[str]] = {}
    home_segment: dict[str, _Segment] = {}

    for seg in segments:
        for r in seg.runs:
            for sid_list, kind in (
                (r.ins_ids, "insertion"),
                (r.del_ids, "deletion"),
                (r.style_ids, "style"),
            ):
                for sid in sid_list:
                    if sid not in kinds:
                        kinds[sid] = set()
                        order.append(sid)
                        home_segment[sid] = seg
                    kinds[sid].add(kind)

    records = []
    for sid in order:
        seg = home_segment[sid]
        own_runs = [
            r
            for r in seg.runs
            if sid in r.ins_ids or sid in r.del_ids or sid in r.style_ids
        ]
        starts = [r.start for r in own_runs if r.start is not None]
        ends = [r.end for r in own_runs if r.end is not None]
        range_start = min(starts) if starts else None
        range_end = max(ends) if ends else None

        range_runs = [
            r
            for r in seg.runs
            if r.start is not None
            and range_start is not None
            and range_start <= r.start < range_end
        ]
        pre_text = "".join(r.text for r in range_runs if r.in_base)
        post_text = "".join(
            r.text
            for r in range_runs
            if (sid in r.ins_ids) or (r.in_base and sid not in r.del_ids)
        )

        if range_runs:
            first, last = range_runs[0], range_runs[-1]
            context_before = seg.base_text[: first.base_start][-CONTEXT_WINDOW:]
            after_offset = last.base_start + last.base_len
            context_after = seg.base_text[after_offset:][:CONTEXT_WINDOW]
        else:  # pragma: no cover - defensive: runs without indexes
            context_before = context_after = ""

        thread = threads.get(sid) or {}
        author = thread.get("author")
        # Through :func:`~gdocs_preview.address.with_address`, not as five
        # more dict keys. These records are returned VERBATIM by
        # ``review_page.project`` under ``fields="full"``, so this literal is
        # an agent-facing index payload, and an agent that reads a bare index
        # out of one hands it back to a tool defaulting to the body of the
        # default tab. Building the address here means the block cannot lose
        # a field silently: ``ADDRESS_FIELDS`` decides its shape, and a
        # dropped source below arrives as an explicit ``None``.
        records.append(
            with_address(
                {
                    "suggestion_id": sid,
                    "type": _suggestion_type(frozenset(kinds[sid])),
                    "pre_text": pre_text,
                    "post_text": post_text,
                    "context_before": context_before,
                    "context_after": context_after,
                    "in_table": any(r.in_table for r in own_runs),
                    "author": author,
                    "author_source": "suggestion_thread" if author else "unavailable",
                    "status": thread.get("status"),
                    "create_time": thread.get("create_time"),
                    "summary_text": thread.get("summary_text"),
                    "replies": thread.get("replies") or [],
                },
                {
                    "segment": seg.segment,
                    "segment_id": seg.segment_id,
                    "tab_id": tab_id,
                    "start_index": range_start,
                    "end_index": range_end,
                },
            )
        )

    return {
        "document_id": document.get("documentId"),
        "title": document.get("title"),
        "suggestion_count": len(records),
        "suggestions": records,
    }


def extract_suggestions_from_tabs(
    tabs: list[tuple[Optional[str], dict[str, Any]]],
    threads: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Run :func:`extract_suggestions` per tab and concatenate the records.

    Each tab is analysed on its own (indexes and segment ids are per-tab in
    the Docs data model), and every record carries its ``tab_id``. A
    single-element list reproduces the single-tab output exactly.
    """
    records: list[dict[str, Any]] = []
    document_id = None
    title = None
    for tab_id, document in tabs:
        result = extract_suggestions(document, threads=threads, tab_id=tab_id)
        if document_id is None:
            document_id = result["document_id"]
            title = result["title"]
        records.extend(result["suggestions"])
    return {
        "document_id": document_id,
        "title": title,
        "suggestion_count": len(records),
        "suggestions": records,
    }


# ---------------------------------------------------------------------------
# Reviewer-view rendering
# ---------------------------------------------------------------------------


def _marked_text(run: _Run) -> str:
    """CriticMarkup-style suggestion markers: {+insertion+} / {-deletion-}.

    Style-only suggestions render unmarked (the text is unchanged); their
    ids still appear in the paragraph's ``suggestion_ids``.
    """
    if run.ins_ids:
        return "{+" + run.text + "+}"
    if run.del_ids:
        return "{-" + run.text + "-}"
    return run.text


def render_document(
    document: dict[str, Any], *, tab_id: Optional[str] = None
) -> dict[str, Any]:
    """Render a Document payload the way a reviewer sees it: plain text with
    inline suggestion markers, plus a paragraph map. Lean by design -- no
    formatting fidelity beyond what review needs.

    ``tab_id`` tags every paragraph with the tab it came from (``None`` for
    a single-tab or GA read).
    """
    segments = _collect_segments(document)

    paragraphs = []
    suggestion_ids: list[str] = []
    seen: set[str] = set()
    segment_texts: dict[str, dict[str, str]] = {
        "header": {},
        "footer": {},
        "footnote": {},
    }
    body_text = ""

    for seg in segments:
        seg_text_parts: list[str] = []
        for para in seg.paragraphs:
            para_ids: list[str] = []
            text_parts = []
            for r in para.runs:
                text_parts.append(_marked_text(r))
                for sid in (*r.ins_ids, *r.del_ids, *r.style_ids):
                    if sid not in para_ids:
                        para_ids.append(sid)
                    if sid not in seen:
                        seen.add(sid)
                        suggestion_ids.append(sid)
            text = "".join(text_parts)
            seg_text_parts.append(text)
            # The paragraph map is how an agent locates a range to write to,
            # so it is an agent-facing index payload and gets its address the
            # same way a suggestion record does -- see the note there.
            paragraphs.append(
                with_address(
                    {
                        "text": text,
                        "named_style": para.named_style,
                        "is_list_item": para.is_list_item,
                        "in_table": para.in_table,
                        "suggestion_ids": para_ids,
                    },
                    {
                        "segment": seg.segment,
                        "segment_id": seg.segment_id,
                        "tab_id": tab_id,
                        "start_index": para.start,
                        "end_index": para.end,
                    },
                )
            )
        seg_text = "".join(seg_text_parts)
        if seg.segment == "body":
            body_text = seg_text
        else:
            segment_texts[seg.segment][seg.segment_id or ""] = seg_text

    return {
        "document_id": document.get("documentId"),
        "title": document.get("title"),
        "body_text": body_text,
        "paragraphs": paragraphs,
        "headers": segment_texts["header"],
        "footers": segment_texts["footer"],
        "footnotes": segment_texts["footnote"],
        "suggestion_ids": suggestion_ids,
    }


def render_tabs(
    tabs: list[tuple[Optional[str], dict[str, Any]]],
) -> dict[str, Any]:
    """Render each tab with :func:`render_document` and merge the results.

    ``body_text`` concatenates the tabs in document order, ``paragraphs``
    carry their ``tab_id``, and header/footer/footnote texts stay keyed by
    segment id (Docs segment ids are document-wide, not per-tab). A
    single-element list reproduces the single-tab output exactly.
    """
    merged: dict[str, Any] = {
        "document_id": None,
        "title": None,
        "body_text": "",
        "paragraphs": [],
        "headers": {},
        "footers": {},
        "footnotes": {},
        "suggestion_ids": [],
    }
    body_parts: list[str] = []
    for tab_id, document in tabs:
        rendered = render_document(document, tab_id=tab_id)
        if merged["document_id"] is None:
            merged["document_id"] = rendered["document_id"]
            merged["title"] = rendered["title"]
        body_parts.append(rendered["body_text"])
        merged["paragraphs"].extend(rendered["paragraphs"])
        for key in ("headers", "footers", "footnotes"):
            merged[key].update(rendered[key])
        for sid in rendered["suggestion_ids"]:
            if sid not in merged["suggestion_ids"]:
                merged["suggestion_ids"].append(sid)
    merged["body_text"] = "".join(body_parts)
    return merged
