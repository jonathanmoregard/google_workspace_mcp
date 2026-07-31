"""Field selection, filtering and pagination for the review read tools.

Everything here is side-effect free: analysis records in (as produced by
:func:`gdocs_preview.analysis.extract_suggestions_from_tabs`), a narrowed and
projected page out, plus an accounting block that says exactly what was left
behind. The MCP tools in :mod:`gdocs_preview.curated_tools` do the API call
and then delegate the shaping here, which keeps the arithmetic testable
without a service object.

**Why this module exists** (measured, ``llmux/scenarios/stress/``): the
review read is linear in card count and had no cap. At 120 pending
suggestions ``list_document_suggestions`` returned 93,443 characters, and a
real agent run (batch ``20260730-224247``,
``stress-120-research-full__sonnet``) was answered with

    Error: result (105,187 characters) exceeds maximum allowed tokens.
    Output has been saved to /home/.../*.json

The agent never saw a single suggestion id, spent its remaining turns trying
to get at the spilled file, and ended by asking the absent user to paste it
in. A response that cannot be delivered is not a conservative default, which
is the argument for every default chosen below.

**Never silently truncate.** Every response carries ``suggestion_count`` (the
document total, unchanged meaning), ``matched_count`` (after filters),
``returned_count`` (this page) and, when there is more, a ``next_page_token``.
A page is always self-describing: an agent that reads one can tell it read
one.

**Page tokens encode a position, not an offset.** Resolving a suggestion
renumbers everything after it, so an offset-based cursor silently skips cards
across an accept/reject. The token therefore carries the *last emitted
suggestion id* as its anchor; the next page resumes after that id wherever it
now sits. When the anchor is gone -- the agent resolved it between pages,
which is the normal working pattern -- the recorded ordinal is used as a
fallback AND the response says so, because that fallback can skip or repeat a
card and the caller has to know.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Field modes
# ---------------------------------------------------------------------------

FIELDS_SUMMARY = "summary"
FIELDS_FULL = "full"
LIST_FIELD_MODES = (FIELDS_SUMMARY, FIELDS_FULL)

#: What ``fields="summary"`` keeps. Chosen against the stress corpus: every
#: predicate in all four stress tasks (author, section by index range, edit
#: size, pure-deletion-ness, overlap with another reviewer's card) is
#: decidable from these values.
#:
#: **``segment``/``segment_id``/``tab_id`` are not optional metadata; they
#: are half of the address.** A Docs index is unique only within a
#: ``(tabId, segmentId)`` pair -- :func:`gdocs_preview.analysis.extract_suggestions`
#: emits one record per segment per tab, and every index it reports is local
#: to that segment. ``suggest_doc_edit`` / ``create_anchored_doc_comment``
#: default ``tab_id=None``/``segment_id=None``, which the API reads as "the
#: body of the default tab". So a summary card that carried only
#: ``start_index``/``end_index`` would let an agent take a footnote's or a
#: header's or a second tab's local index and write it into the body,
#: silently, with nothing in the response to warn it. They cost ~45
#: characters a card in the common (body, single-tab) case; being addressable
#: is what the record is FOR.
SUMMARY_FIELDS = (
    "suggestion_id",
    "type",
    "author",
    "summary_text",
    "segment",
    "segment_id",
    "tab_id",
    "start_index",
    "end_index",
    "status",
)

#: What ``fields="summary"`` drops, reported verbatim in the response so the
#: omission is never something the caller has to infer. Nothing here is
#: needed to ADDRESS a suggestion -- only to read its text.
SUMMARY_OMITTED_FIELDS = (
    "pre_text",
    "post_text",
    "context_before",
    "context_after",
    "in_table",
    "create_time",
    "author_source",
    "replies",
)

#: Default page size per field mode. Sized in BYTES, not cards: the binding
#: constraint is what a client will deliver in one tool result, and a card
#: costs ~780 characters in ``full`` and ~232-252 in ``summary`` (measured
#: across all four stress tiers, flat per card). Both defaults land a full
#: page at roughly 31-48 KB, under the ~57 KB at which the observed client
#: began spilling tool output to a file the agent could not read.
DEFAULT_PAGE_SIZE = {FIELDS_SUMMARY: 200, FIELDS_FULL: 40}

#: Hard ceiling on an explicitly requested page size. Above this the response
#: stops being deliverable in ``full`` mode, which is the failure this whole
#: module exists to prevent.
MAX_PAGE_SIZE = 500


class PageTokenError(ValueError):
    """A ``page_token`` that cannot be honoured as given."""


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def author_display_name(author: Any) -> Optional[str]:
    """The display name out of a normalized author record, or ``None``.

    ``summary`` mode reports the author as this string rather than the
    nested People object: the object costs ~90 characters per card (the
    single largest field, 15.6% of a 120-card response) and carries
    ``me``/``anonymous``/``user`` that no review predicate needs.
    """
    if isinstance(author, dict):
        name = author.get("display_name")
        return str(name) if name else None
    if isinstance(author, str):
        return author or None
    return None


def project(record: dict[str, Any], fields: str) -> dict[str, Any]:
    """One analysis record, narrowed to ``fields``."""
    if fields == FIELDS_FULL:
        return record
    projected = {key: record.get(key) for key in SUMMARY_FIELDS}
    projected["author"] = author_display_name(record.get("author"))
    return projected


def validate_fields(
    fields: Any, allowed: Sequence[str], parameter: str = "fields"
) -> str:
    value = str(fields or "").strip().lower()
    if value not in allowed:
        raise ValueError(
            f"Invalid {parameter} {fields!r}. Must be one of: {', '.join(allowed)}."
        )
    return value


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _overlaps(record: dict[str, Any], low: Optional[int], high: Optional[int]) -> bool:
    """Does the record's UTF-16 range overlap the half-open ``[low, high)``?

    Pure index arithmetic, and therefore only ever correct for two records in
    the SAME coordinate space -- see :func:`in_range_scope`, which is what
    decides that. Half-open, because that is the convention the caller's
    numbers already come in: Docs ``endIndex`` is exclusive, so the paragraph
    map an agent reads a section boundary off reports the next paragraph's
    start as this one's end. Treating the filter as inclusive would pull in
    the paragraph on the far side of every seam -- an off-by-one that shows
    up as "why is the heading of the next section in my results".

    Either bound may be ``None``, meaning unbounded on that side. A record
    with a degenerate (zero-width) range is treated as covering one unit, so
    it can still be selected.
    """
    start, end = record.get("start_index"), record.get("end_index")
    if start is None or end is None:
        return False
    if end == start:
        end = start + 1
    if low is not None and end <= low:
        return False
    if high is not None and start >= high:
        return False
    return True


# ---------------------------------------------------------------------------
# Range scope: which coordinate space an index range is in
# ---------------------------------------------------------------------------
#
# A Docs index is unique only within a ``(tabId, segmentId)`` pair. Comparing
# raw numbers across pairs -- which is what a bare ``_overlaps`` does -- makes
# an index range match the body AND every header, footer, footnote and other
# tab whose LOCAL index happens to fall in the window, so ``matched_count``
# is wrong and the extra cards look like they are in the section under
# review. Every index range therefore names exactly one space.


def resolve_range_scope(
    records: Sequence[dict[str, Any]],
    *,
    segment_id: Optional[str],
    tab_id: Optional[str],
) -> dict[str, Any]:
    """The one ``(tab, segment)`` an index range is interpreted in.

    ``segment_id=None`` means the body: a non-body segment always carries an
    id (:func:`gdocs_preview.analysis._collect_segments`), so ``None`` is not
    ambiguous. ``tab_id=None`` is resolved from the records -- a single-tab
    or GA read has exactly one, and the caller should not have to name it --
    but a genuinely multi-tab document is REFUSED rather than guessed at,
    because each tab is numbered from its own start and picking one silently
    would answer a different question than the one asked.
    """
    if tab_id is None:
        present = sorted({r.get("tab_id") for r in records if r.get("tab_id")})
        if len(present) > 1:
            raise ValueError(
                "An index range needs a tab_id in this document: it has "
                f"{len(present)} tabs ({', '.join(present)}) and Docs numbers "
                "each tab from its own start, so [start_index, end_index) "
                "names a different place in each one. Pass tab_id together "
                "with the range (the tab id is on every suggestion record), "
                "or filter without a range."
            )
        tab_id = present[0] if present else None
    segment = (
        "body"
        if segment_id is None
        else next(
            (
                r.get("segment")
                for r in records
                if (r.get("segment_id") or None) == segment_id
            ),
            None,
        )
    )
    return {"segment": segment, "segment_id": segment_id, "tab_id": tab_id}


def in_range_scope(record: dict[str, Any], scope: dict[str, Any]) -> bool:
    """Is ``record`` numbered in the space ``scope`` names?"""
    if (record.get("segment_id") or None) != (scope.get("segment_id") or None):
        return False
    if scope.get("tab_id") is not None and (
        (record.get("tab_id") or None) != scope["tab_id"]
    ):
        return False
    return True


def filter_records(
    records: Sequence[dict[str, Any]],
    *,
    author: Optional[str] = None,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    status: Optional[str] = None,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Narrow ``records``; return them plus an accounting of what was dropped.

    All matching is case-insensitive and exact (never substring): an agent
    filtering by ``author="dana"`` that silently also got "Dana Prescott-Wu"
    and "Danielle" would resolve the wrong cards. When an ``author`` or
    ``status`` filter matches nothing, the accounting lists the values that
    ARE present so the next call is informed rather than a guess.

    ``segment_id`` and ``tab_id`` are filters in their own right AND the
    coordinate space an index range is read in: with a range and no
    ``segment_id`` the range means the BODY, and every card in a header,
    footer or footnote is excluded from it and counted in
    ``excluded_other_segments``. That default is what makes an index range
    mean one thing; see :func:`resolve_range_scope`.
    """
    wants_range = start_index is not None or end_index is not None
    if (
        start_index is not None
        and end_index is not None
        and int(end_index) <= int(start_index)
    ):
        raise ValueError(
            f"end_index ({end_index}) must be greater than start_index "
            f"({start_index}): the range is half-open [start_index, "
            "end_index), matching the Docs convention that endIndex is "
            "exclusive. For a single position, pass end_index=start_index+1."
        )
    author_key = (
        author.strip().lower() if isinstance(author, str) and author.strip() else None
    )
    status_key = (
        status.strip().upper() if isinstance(status, str) and status.strip() else None
    )
    segment_key = segment_id.strip() if isinstance(segment_id, str) else None
    tab_key = tab_id.strip() if isinstance(tab_id, str) else None
    scope = (
        resolve_range_scope(records, segment_id=segment_key, tab_id=tab_key)
        if wants_range
        else None
    )

    kept: list[dict[str, Any]] = []
    unindexed_excluded = 0
    other_segment_excluded = 0
    for record in records:
        if author_key is not None:
            name = author_display_name(record.get("author"))
            if not name or name.strip().lower() != author_key:
                continue
        if status_key is not None:
            value = record.get("status")
            if not value or str(value).strip().upper() != status_key:
                continue
        if segment_key is not None and (record.get("segment_id") or None) != segment_key:
            continue
        if tab_key is not None and (record.get("tab_id") or None) != tab_key:
            continue
        if wants_range:
            if not in_range_scope(record, scope or {}):
                other_segment_excluded += 1
                continue
            if record.get("start_index") is None or record.get("end_index") is None:
                unindexed_excluded += 1
                continue
            if not _overlaps(record, start_index, end_index):
                continue
        kept.append(record)

    applied: dict[str, Any] = {}
    if author_key is not None:
        applied["author"] = author
    if status_key is not None:
        applied["status"] = status
    if segment_key is not None:
        applied["segment_id"] = segment_key
    if tab_key is not None:
        applied["tab_id"] = tab_key
    if wants_range:
        applied["start_index"] = start_index
        applied["end_index"] = end_index
        applied["range_match"] = (
            "overlap with the half-open range [start_index, end_index), within "
            "one (tab, segment): Docs numbers each from its own start"
        )
        applied["range_scope"] = scope
    if unindexed_excluded:
        applied["excluded_without_indexes"] = unindexed_excluded
    if other_segment_excluded:
        applied["excluded_other_segments"] = other_segment_excluded

    if not kept and applied:
        if author_key is not None:
            applied["authors_present"] = sorted(
                {
                    n
                    for n in (author_display_name(r.get("author")) for r in records)
                    if n
                }
            )
        if status_key is not None:
            applied["statuses_present"] = sorted(
                {str(r.get("status")) for r in records if r.get("status")}
            )
        if segment_key is not None or wants_range:
            applied["segments_present"] = sorted(
                {
                    f"{r.get('segment')}:{r.get('segment_id')}"
                    for r in records
                    if r.get("segment")
                }
            )
    return kept, applied


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _fingerprint(document_id: str, fields: str, applied: dict[str, Any]) -> str:
    """Identity of the query a token belongs to.

    A token minted under one filter set and replayed under another would
    resume at a position that means something else entirely, so the token
    carries this and :func:`decode_page_token` refuses the mismatch.
    """
    stable = {
        k: v
        for k, v in sorted(applied.items())
        if k
        in ("author", "status", "start_index", "end_index", "segment_id", "tab_id")
    }
    return json.dumps(
        {"d": document_id, "f": fields, "q": stable}, sort_keys=True, ensure_ascii=False
    )


def encode_page_token(
    *,
    document_id: str,
    fields: str,
    applied: dict[str, Any],
    ordinal: int,
    anchor: Optional[str],
) -> str:
    payload = {
        "v": 1,
        "k": _fingerprint(document_id, fields, applied),
        "i": int(ordinal),
        "a": anchor,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_page_token(
    token: str, *, document_id: str, fields: str, applied: dict[str, Any]
) -> tuple[int, Optional[str]]:
    """``(ordinal, anchor_suggestion_id)`` from a token minted for this query.

    Raises :class:`PageTokenError` with an actionable message on anything
    malformed or belonging to a different document/field mode/filter set --
    resuming the wrong query silently is worse than failing loudly.
    """
    text = str(token or "").strip()
    if not text:
        raise PageTokenError("page_token is empty; omit it to start at the first page.")
    padded = text + "=" * (-len(text) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise PageTokenError(
            f"page_token is not a token this tool issued ({error}). Page tokens "
            "come back in the response's `next_page_token`; they are never "
            "constructed by hand."
        ) from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise PageTokenError(
            "page_token has an unrecognised version; re-request the first page."
        )
    if payload.get("k") != _fingerprint(document_id, fields, applied):
        raise PageTokenError(
            "page_token was issued for a different query (document_id, fields "
            "or filters changed). Pagination is per query: re-request the "
            "first page with the parameters you want, or repeat the exact "
            "parameters the token was issued under."
        )
    ordinal = payload.get("i")
    if not isinstance(ordinal, int) or ordinal < 0:
        raise PageTokenError("page_token carries an invalid position.")
    anchor = payload.get("a")
    return ordinal, str(anchor) if anchor else None


def resolve_page_size(page_size: Any, fields: str) -> int:
    if page_size is None:
        return DEFAULT_PAGE_SIZE[fields]
    try:
        value = int(page_size)
    except (TypeError, ValueError) as error:
        raise ValueError(f"page_size must be an integer, got {page_size!r}.") from error
    if value < 1:
        raise ValueError(
            f"page_size must be at least 1, got {value}. There is no "
            "'unlimited' setting: an unbounded response is the failure mode "
            "this parameter exists to prevent."
        )
    return min(value, MAX_PAGE_SIZE)


def paginate(
    records: Sequence[dict[str, Any]],
    *,
    document_id: str,
    fields: str,
    applied: dict[str, Any],
    page_size: int,
    page_token: Optional[str] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One page of ``records`` plus the block describing it.

    The page block always names the total it came from, so a caller cannot
    mistake a page for the whole set even if it ignores every other field.
    """
    start = 0
    page: dict[str, Any] = {"page_size": page_size}
    if page_token:
        ordinal, anchor = decode_page_token(
            page_token, document_id=document_id, fields=fields, applied=applied
        )
        start = ordinal
        page["anchor"] = anchor
        if anchor is not None:
            located = next(
                (i for i, r in enumerate(records) if r.get("suggestion_id") == anchor),
                None,
            )
            if located is not None:
                start = located + 1
                page["anchor_found"] = True
            else:
                page["anchor_found"] = False
                page["anchor_note"] = (
                    f"the suggestion {anchor!r} this page resumes after is no "
                    "longer in the document (resolving one suggestion can "
                    "remove others), so this page resumed at the recorded "
                    f"position {ordinal} instead. That fallback can skip or "
                    "repeat a card: re-request page 1 if you need an exact "
                    "sweep."
                )
    start = max(0, min(start, len(records)))
    window = list(records[start : start + page_size])
    end = start + len(window)
    has_more = end < len(records)

    page["offset"] = start
    page["has_more"] = has_more
    if has_more:
        page["next_page_token"] = encode_page_token(
            document_id=document_id,
            fields=fields,
            applied=applied,
            ordinal=end,
            anchor=(window[-1].get("suggestion_id") if window else None),
        )
    else:
        page["next_page_token"] = None
    return window, page


# ---------------------------------------------------------------------------
# Assembling a list_document_suggestions response
# ---------------------------------------------------------------------------


def _summary_notice(read_source: str, ga_source: str) -> str:
    base = (
        "fields='summary': one line per suggestion, omitting "
        + ", ".join(SUMMARY_OMITTED_FIELDS)
        + ". Ask for fields='full' (with a small page_size) when you need the "
        "before/after text of a card."
    )
    if read_source == ga_source:
        base += (
            " This read degraded to the GA documents.get, which carries no "
            "suggestion threads, so `summary_text`, `author` and `status` are "
            "null on every record here -- fields='full' is the only way to see "
            "what these suggestions change."
        )
    return base


def build_listing(
    analysis: dict[str, Any],
    *,
    document_id: str,
    read_source: str,
    ga_source: str,
    fields: str,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    author: Optional[str] = None,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    status: Optional[str] = None,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> dict[str, Any]:
    """Filter, project and paginate an analysis result into a response body.

    ``suggestion_count`` keeps its original meaning -- every pending
    suggestion in the document -- so an existing caller reading it still
    reads the truth. ``matched_count`` and ``returned_count`` are the new,
    narrower numbers, and they are always present even when nothing was
    narrowed, so "did I see everything?" is answered by comparing three
    integers rather than by trusting a default.
    """
    records = list(analysis.get("suggestions") or [])
    fields = validate_fields(fields, LIST_FIELD_MODES)
    size = resolve_page_size(page_size, fields)
    kept, applied = filter_records(
        records,
        author=author,
        start_index=start_index,
        end_index=end_index,
        status=status,
        segment_id=segment_id,
        tab_id=tab_id,
    )
    window, page = paginate(
        kept,
        document_id=document_id,
        fields=fields,
        applied=applied,
        page_size=size,
        page_token=page_token,
    )
    result: dict[str, Any] = {
        "document_id": analysis.get("document_id") or document_id,
        "title": analysis.get("title"),
        "suggestion_count": len(records),
        "matched_count": len(kept),
        "returned_count": len(window),
        "fields": fields,
        "filters": applied,
        "page": page,
        "suggestions": [project(r, fields) for r in window],
    }
    if fields == FIELDS_SUMMARY:
        result["omitted_fields"] = list(SUMMARY_OMITTED_FIELDS)
        result["notice"] = _summary_notice(read_source, ga_source)
    if page["has_more"]:
        result["notice_page"] = (
            f"This is a PAGE, not the whole set: {len(window)} of "
            f"{len(kept)} matching suggestions ({len(records)} in the "
            "document). Pass the response's `next_page_token` back as "
            "`page_token` for the rest."
        )
    return result


# ---------------------------------------------------------------------------
# Assembling a get_doc_review_view response
# ---------------------------------------------------------------------------

VIEW_FIELDS_TEXT = "text"
VIEW_FIELDS_PARAGRAPHS = "paragraphs"
VIEW_FIELDS_FULL = "full"
VIEW_FIELD_MODES = (VIEW_FIELDS_TEXT, VIEW_FIELDS_PARAGRAPHS, VIEW_FIELDS_FULL)


def _paragraph_in_window(
    paragraph: dict[str, Any],
    low: Optional[int],
    high: Optional[int],
    scope: dict[str, Any],
) -> bool:
    return in_range_scope(paragraph, scope) and _overlaps(paragraph, low, high)


def build_review_view(
    rendered: dict[str, Any],
    *,
    fields: str,
    start_index: Optional[int] = None,
    end_index: Optional[int] = None,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> dict[str, Any]:
    """Narrow a rendered document to ``fields`` and an optional index window.

    The measured redundancy this addresses: at 120 cards the rendered
    response was 54,901 characters, of which the paragraph map was 26,269 and
    ``body_text`` 13,462 -- and ``body_text`` is exactly the concatenation of
    the body paragraphs' ``text``. Returning both spends a quarter of the
    response restating the other half of it.

    ``text`` (the default) is therefore the readable half, ``paragraphs`` the
    addressable half (same characters, plus indexes, styles and the
    suggestion ids touching each paragraph), and ``full`` is both -- the
    shape this tool always returned, kept verbatim for callers that parse it.

    The window is read in exactly one ``(tab, segment)`` -- the body of the
    document's single tab unless ``segment_id`` / ``tab_id`` say otherwise --
    for the same reason the listing's range filter is
    (:func:`resolve_range_scope`): a header paragraph numbered from 0 would
    otherwise fall inside every window taken off the body's first page.
    """
    fields = validate_fields(fields, VIEW_FIELD_MODES)
    windowed = start_index is not None or end_index is not None
    if (
        start_index is not None
        and end_index is not None
        and int(end_index) <= int(start_index)
    ):
        raise ValueError(
            f"end_index ({end_index}) must be greater than start_index "
            f"({start_index}): the window is half-open [start_index, "
            "end_index), matching the Docs convention that endIndex is "
            "exclusive."
        )
    paragraphs = list(rendered.get("paragraphs") or [])
    total_paragraphs = len(paragraphs)
    scope: dict[str, Any] = {}
    if windowed:
        scope = resolve_range_scope(
            paragraphs,
            segment_id=segment_id.strip() if isinstance(segment_id, str) else None,
            tab_id=tab_id.strip() if isinstance(tab_id, str) else None,
        )
        paragraphs = [
            p
            for p in paragraphs
            if _paragraph_in_window(p, start_index, end_index, scope)
        ]

    result: dict[str, Any] = {
        "document_id": rendered.get("document_id"),
        "title": rendered.get("title"),
        "fields": fields,
        "paragraph_count": total_paragraphs,
        "returned_paragraph_count": len(paragraphs),
    }
    omitted: list[str] = []

    if fields in (VIEW_FIELDS_TEXT, VIEW_FIELDS_FULL):
        if windowed:
            # Recomputed from the surviving body paragraphs so text and map
            # can never describe different windows. Without a window this is
            # character-identical to the renderer's own body_text.
            result["body_text"] = "".join(
                p.get("text") or "" for p in paragraphs if p.get("segment") == "body"
            )
        else:
            result["body_text"] = rendered.get("body_text", "")
        for key in ("headers", "footers", "footnotes"):
            result[key] = rendered.get(key) or {}
    else:
        omitted.append("body_text")
        omitted.extend(("headers", "footers", "footnotes"))

    if fields in (VIEW_FIELDS_PARAGRAPHS, VIEW_FIELDS_FULL):
        result["paragraphs"] = paragraphs
    else:
        omitted.append("paragraphs")

    result["suggestion_ids"] = list(rendered.get("suggestion_ids") or [])
    if windowed:
        window_ids: list[str] = []
        for paragraph in paragraphs:
            for sid in paragraph.get("suggestion_ids") or []:
                if sid not in window_ids:
                    window_ids.append(sid)
        result["suggestion_ids"] = window_ids
        result["window"] = {
            "start_index": start_index,
            "end_index": end_index,
            "match": (
                "paragraphs of one (tab, segment) overlapping the half-open "
                "range [start_index, end_index); Docs numbers each tab and "
                "each header/footer/footnote from its own start"
            ),
            "scope": scope,
            "paragraphs_outside_window": total_paragraphs - len(paragraphs),
        }
    if omitted:
        result["omitted_fields"] = omitted
        result["notice"] = (
            "fields="
            + repr(fields)
            + " omitted "
            + ", ".join(omitted)
            + ". "
            + (
                "The paragraph map carries the same characters as `body_text` "
                "plus each paragraph's indexes and the suggestion ids touching "
                "it; ask for fields='paragraphs' when you need to address a "
                "range, or fields='full' for both."
                if fields == VIEW_FIELDS_TEXT
                else "`body_text` is the concatenation of the body paragraphs' "
                "`text`, so nothing here is lost; ask for fields='full' if you "
                "want it restated."
            )
        )
    return result


def known_suggestion_ids(records: Iterable[dict[str, Any]]) -> list[str]:
    """Ids in document order -- the cheapest complete answer to 'what is here?'."""
    out: list[str] = []
    for record in records:
        sid = record.get("suggestion_id")
        if sid and sid not in out:
            out.append(str(sid))
    return out
