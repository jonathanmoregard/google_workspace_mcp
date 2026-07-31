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
# Base text and structural resolution checking
# ---------------------------------------------------------------------------
#
# A resolved suggestion is verified against the document by re-running THIS
# layer over the post-write read and asking whether the base text now reads
# what the algebra predicts. It is deliberately not done against
# :func:`render_document`'s output: that is CriticMarkup-marked text
# (``{-…-}`` / ``{+…+}``) while ``pre_text``/``post_text``/``context_before``/
# ``context_after`` are base text, and every way of mixing the two is wrong in
# a different direction. Verified against the live API 2026-07-31: prod splits
# a ``textRun`` at every style boundary, so suggesting the deletion of
# "brave new" across a bold/regular seam yields TWO deletion-marked runs and
# renders ``{-brave-}{- new-}``. Substring-searching that for the base string
# "brave new" fails, and on an accept -- where the check is "is the struck
# text gone" -- failing to find it reported ``matches_expectation: true``
# with no check having occurred: fail-open verification on the one
# destructive path this package has.

_BASE_TEXT_MINT = object()


class BaseText(str):
    """One ``(tab, segment)``'s BASE text: what the document really says.

    Base text is the PREVIEW_WITHOUT_SUGGESTIONS projection of one segment --
    every pending insertion stripped, every pending deletion kept -- which is
    the exact representation ``pre_text``, ``post_text``, ``context_before``
    and ``context_after`` are computed in (see the module docstring). It is a
    distinct type, and it can only be minted by :func:`segment_base_texts`,
    because the recurring bug in this package is a comparison whose two sides
    come from different representations of the same document. Handing marked
    text to a function that wants base text is then a ``TypeError`` at the
    call, not a wrong boolean three layers later.

    Being a ``str`` subclass keeps it ergonomic: slicing, ``find`` and
    ``len`` all work and return plain ``str``/``int``, so a display window cut
    out of one is not itself a ``BaseText`` and cannot be re-fed to a check.
    """

    __slots__ = ()

    def __new__(cls, text: str, _mint: Any = None) -> "BaseText":
        if _mint is not _BASE_TEXT_MINT:
            raise TypeError(
                "BaseText is minted only by gdocs_preview.analysis."
                "segment_base_texts(): pass the value that function returned, "
                "never a string you rendered. render_document() produces "
                "CriticMarkup-marked text ({-deletion-} / {+insertion+}) while "
                "pre_text/post_text/context_before/context_after are base text, "
                "and comparing the two representations is the fail-open "
                "verification bug this type exists to prevent."
            )
        return super().__new__(cls, text)


def segment_base_texts(
    document: dict[str, Any], *, tab_id: Optional[str] = None
) -> dict[tuple[Optional[str], Optional[str]], BaseText]:
    """Every segment's base text, keyed by ``(tab_id, segment_id)``.

    The key is the coordinate space, not the document: a header's indexes and
    context windows mean nothing against the body's text, and two tabs number
    their bodies from the same start. ``segment_id`` is ``None`` for a body,
    matching what the write tools pass to the API.
    """
    return {
        (tab_id, seg.segment_id or None): BaseText(seg.base_text, _BASE_TEXT_MINT)
        for seg in _collect_segments(document)
    }


#: :func:`check_resolution` could not reach a verdict because the resolved
#: range's anchor -- the base text immediately before it, which accepting or
#: rejecting does not touch -- is no longer in that segment.
ANCHOR_NOT_FOUND = "anchor_not_found"

#: :func:`check_resolution` found the anchor but the document reads as BOTH
#: the resolved and the unresolved outcome (the two predictions overlap
#: because the text repeats). No verdict, rather than a coin toss.
AMBIGUOUS_ANCHOR = "ambiguous_anchor"


@dataclass(frozen=True)
class ResolutionCheck:
    """Did the document end up reading what resolving a suggestion promised?

    ``matches`` is ``None`` only when no check could run, and ``reason`` then
    names which of :data:`ANCHOR_NOT_FOUND` / :data:`AMBIGUOUS_ANCHOR` it
    was. ``window`` is the base text around the located range, for display.
    """

    matches: Optional[bool]
    window: Optional[str]
    reason: Optional[str] = None


def _anchor_positions(base: str, context_before: str) -> list[int]:
    """Where the resolved range can start, given its preceding base text.

    ``context_before`` is ``seg.base_text[:range_start][-CONTEXT_WINDOW:]``,
    so a value SHORTER than :data:`CONTEXT_WINDOW` means the anchor ran out of
    document: the range starts at offset ``len(context_before)`` and nowhere
    else. Only a full-width anchor can repeat, and then every occurrence is a
    candidate rather than ``find``'s first one -- a document whose boilerplate
    repeats would otherwise have its verdict decided by the first paragraph
    that happened to match.
    """
    if len(context_before) < CONTEXT_WINDOW:
        return [len(context_before)] if base.startswith(context_before) else []
    return [
        position + len(context_before)
        for position in range(len(base) - len(context_before) + 1)
        if base.startswith(context_before, position)
    ]


def _reads_as(base: str, start: int, text: str, context_after: str) -> bool:
    """Does ``base`` read ``text`` at ``start``, followed by ``context_after``?

    Positional, never a substring search: the range is bounded on the left by
    the anchor and on the right by the untouched following base text, so
    "the same words somewhere else in the segment" cannot answer for it. A
    ``context_after`` shorter than :data:`CONTEXT_WINDOW` ran to the end of
    the segment, and is therefore required to consume the rest of it -- which
    is what stops an empty ``text`` (accepting a pure deletion at the end of a
    segment) from matching vacuously.
    """
    if not base.startswith(text + context_after, start):
        return False
    if len(context_after) < CONTEXT_WINDOW:
        return start + len(text) + len(context_after) == len(base)
    return True


def check_resolution(
    base: BaseText,
    *,
    context_before: str,
    context_after: str,
    expected_text: str,
    removed_text: str,
) -> ResolutionCheck:
    """Verify one accept/reject STRUCTURALLY against post-write base text.

    ``expected_text`` is what the resolution promised the range would read
    (``post_text`` for an accept, ``pre_text`` for a reject) and
    ``removed_text`` is the other half -- what it should no longer read. Both
    come from :func:`extract_suggestions`, and ``base`` must come from
    :func:`segment_base_texts`: one representation, both sides.

    The verdict is positional and two-sided. The range is located by its
    anchor, and the segment must read ``expected_text`` there AND the
    untouched ``context_after`` immediately behind it. Only that produces
    ``True``. Reading ``removed_text`` there instead produces ``False`` --
    the write did not land, or landed somewhere else. Anything else is also
    ``False``: the range does not read what the card promised, whatever it
    does read, and ``window`` shows the caller what that is.

    There is no substring search anywhere in here, and no branch in which the
    absence of a string is taken as evidence that something was removed. Both
    were how the previous two versions of this check reported success on a
    destructive write it had not verified.
    """
    if not isinstance(base, BaseText):
        raise TypeError(
            "check_resolution needs the BASE text of the resolved suggestion's "
            "segment (gdocs_preview.analysis.segment_base_texts), not "
            f"{type(base).__name__}. render_document()'s text carries "
            "CriticMarkup markers and pre_text/post_text do not, so comparing "
            "them decides the verdict by the representation."
        )
    widest = max(expected_text, removed_text, key=len)
    verdicts: set[bool] = set()
    window: Optional[str] = None
    for start in _anchor_positions(base, context_before):
        landed = _reads_as(base, start, expected_text, context_after)
        unresolved = _reads_as(base, start, removed_text, context_after)
        if window is None or landed or unresolved:
            window = _window(base, start, context_before, widest)
        if landed and unresolved and expected_text != removed_text:
            # The two predictions both fit here (the text repeats in exactly
            # the wrong way). Refuse rather than pick one.
            return ResolutionCheck(None, window, AMBIGUOUS_ANCHOR)
        verdicts.add(landed)
    if not verdicts:
        return ResolutionCheck(None, None, ANCHOR_NOT_FOUND)
    if len(verdicts) > 1:
        # A repeated full-width anchor whose occurrences disagree: one of them
        # reads as resolved and another does not, and nothing here can say
        # which one the write happened at.
        return ResolutionCheck(None, window, AMBIGUOUS_ANCHOR)
    return ResolutionCheck(verdicts.pop(), window)


def _window(base: str, start: int, context_before: str, text: str) -> str:
    """The base text around a located range, for the response echo.

    Returned UNCLIPPED: the caller clips what it shows, never what it
    compared -- and the comparison above never looks at this value at all.
    """
    return base[start - len(context_before) : start + len(text) + CONTEXT_WINDOW]


# ---------------------------------------------------------------------------
# Reviewer-view rendering
# ---------------------------------------------------------------------------


def _marked_text(run: _Run) -> str:
    """CriticMarkup-style suggestion markers: {+insertion+} / {-deletion-}.

    Style-only suggestions render unmarked (the text is unchanged); their
    ids still appear in the paragraph's ``suggestion_ids``.

    **Marked text is for a human to read, never for a machine to compare.**
    The markers are not in the document; ``pre_text``, ``post_text`` and the
    context windows are all base text, so a comparison that puts one of each
    on the two sides of ``in`` or ``==`` is comparing two different
    representations of the document. :class:`BaseText` exists so that
    comparison cannot be written -- see :func:`segment_base_texts`.
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


#: Written ahead of each tab's body in a MULTI-TAB ``body_text``. The tab id
#: is spelled out verbatim so it can be passed straight back as ``tab_id``.
TAB_MARKER_PREFIX = "===== tab_id: "
TAB_MARKER_SUFFIX = " =====\n"


def tab_marker(tab_id: Optional[str]) -> str:
    """The boundary line introducing one tab's body text."""
    return f"{TAB_MARKER_PREFIX}{tab_id}{TAB_MARKER_SUFFIX}"


def render_tabs(
    tabs: list[tuple[Optional[str], dict[str, Any]]],
) -> dict[str, Any]:
    """Render each tab with :func:`render_document` and merge the results.

    ``body_text`` concatenates the tabs in document order, ``paragraphs``
    carry their ``tab_id``, and header/footer/footnote texts stay keyed by
    segment id (Docs segment ids are document-wide, not per-tab). A
    single-element list reproduces the single-tab output exactly.

    **A multi-tab body is separated by :func:`tab_marker`.** This string is
    ``get_doc_review_view``'s DEFAULT output, and joining the tabs with
    nothing at all fused the last line of one tab to the first line of the
    next -- a sentence that exists in neither tab, presented to a reviewer as
    the document's prose. Nothing in the response said where the seam was, so
    an agent asked to "review the introduction" could read across a tab
    boundary without any signal that it had left the tab it started in, and
    every index it then quoted would be numbered from the wrong start.

    The marker names the tab it introduces, so the text after it is
    attributable and the id is ready to hand back as ``tab_id``. A single-tab
    document (and a GA read, which has one implicit tab) gets no marker at
    all: there is no seam, and the marker would be noise on the common case.
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
    multi_tab = len(tabs) > 1
    body_parts: list[str] = []
    for tab_id, document in tabs:
        rendered = render_document(document, tab_id=tab_id)
        if merged["document_id"] is None:
            merged["document_id"] = rendered["document_id"]
            merged["title"] = rendered["title"]
        if multi_tab:
            body_parts.append(tab_marker(tab_id))
        body_parts.append(rendered["body_text"])
        merged["paragraphs"].extend(rendered["paragraphs"])
        for key in ("headers", "footers", "footnotes"):
            merged[key].update(rendered[key])
        for sid in rendered["suggestion_ids"]:
            if sid not in merged["suggestion_ids"]:
                merged["suggestion_ids"].append(sid)
    merged["body_text"] = "".join(body_parts)
    return merged
