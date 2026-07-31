"""Where something is in a Google Doc -- the whole answer, in one place.

**An index is only half of an address.** Docs numbers every
``(tabId, segmentId)`` pair from its own start, so ``start_index: 5`` in a
header and ``start_index: 5`` in the body are different characters in
different coordinate spaces. Every write tool in
:mod:`gdocs_preview.write_tools` defaults to ``tab_id=None``/
``segment_id=None``, which the API reads as "the body of the default tab":
an agent that reads a bare index out of one of our responses and hands it
back writes into the BODY at that number, silently, in a customer document.
Index 0 fails loud on the floor check; every other index does not.

That bug class has now been found three times -- on the read path (summary
cards), on the write path (the post-write echo and the resolution ledger),
and in the coordinate-space filters. The remedy is structural rather than
vigilant: **this module is the only place that projects an index into an
agent-facing payload**, and it cannot project one without its
``segment``/``segment_id``/``tab_id``. :func:`address_of` returns all five
fields or none; :func:`with_address` is how a payload acquires them. A
future edit that drops the pairing has to delete a field from
:data:`ADDRESS_FIELDS`, where the docstring above is looking at it.

The other half of the same idea is comparison: two indexes may only be
compared when they are numbered in the same space. :func:`resolve_range_scope`
decides which space a caller meant (resolving an omitted ``tab_id`` when the
document has only one, REFUSING to guess when it has several), and
:func:`in_range_scope` is the membership test. Read and write paths share
both, so "which suggestions are at this range" cannot mean two different
things depending on which module asked.

That refusal is only as honest as what it counts. It counted the tabs
occupied by the records the caller was holding, which is a different
question from how many tabs the document has: a three-tab document whose
cards all sit in one tab presented as single-tab, and the omitted ``tab_id``
resolved silently to that tab. :func:`resolve_range_scope` therefore takes
the document's ``tab_ids`` as a required argument -- :func:`tab_inventory`
reads them off the same ``tab_metadata`` every review response already
carries -- so all three resolvers are counting the document.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

#: The five fields that, together, locate a range in a document. ``segment``
#: is the human-readable kind (``body``/``header``/``footer``/``footnote``);
#: ``segment_id`` and ``tab_id`` are what ``suggest_doc_edit`` /
#: ``create_anchored_doc_comment`` take verbatim. They are ONE unit: emitting
#: the indexes without the other three is the bug this module exists to make
#: unrepresentable.
ADDRESS_FIELDS = (
    "segment",
    "segment_id",
    "tab_id",
    "start_index",
    "end_index",
)

#: Just the coordinate space, without the offsets into it.
SCOPE_FIELDS = ("segment", "segment_id", "tab_id")


def address_of(record: dict[str, Any]) -> dict[str, Any]:
    """The address of an analysis record: all of :data:`ADDRESS_FIELDS`.

    Missing keys come back as ``None`` rather than being omitted, so a
    consumer can always tell "the body" (``segment_id: null``) from "this
    response did not say", and the shape of the block never varies.
    """
    return {key: record.get(key) for key in ADDRESS_FIELDS}


def with_address(payload: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """``payload`` plus ``record``'s address -- the only way to emit an index.

    The address wins over anything of the same name already in ``payload``:
    the record is the authority on where it is.
    """
    return {**payload, **address_of(record)}


def tab_inventory(tab_metadata: Sequence[dict[str, Any]]) -> list[str]:
    """The document's tab ids, from a read's ``tab_metadata``.

    ``None`` entries (the GA fallback's single implicit tab) are dropped, so
    an empty list means "this read cannot see tabs", which
    :func:`resolve_range_scope` treats as the single unnamed coordinate space
    it is.
    """
    return sorted({t.get("tab_id") for t in tab_metadata if t.get("tab_id")})


def resolve_range_scope(
    records: Sequence[dict[str, Any]],
    *,
    tab_ids: Sequence[Optional[str]],
    segment_id: Optional[str],
    tab_id: Optional[str],
) -> dict[str, Any]:
    """The one ``(tab, segment)`` an index range is interpreted in.

    ``segment_id=None`` means the body: a non-body segment always carries an
    id (:func:`gdocs_preview.analysis._collect_segments`), so ``None`` is not
    ambiguous. ``tab_id=None`` is resolved from ``tab_ids`` -- a single-tab
    or GA read has exactly one, and the caller should not have to name it --
    but a genuinely multi-tab document is REFUSED rather than guessed at,
    because each tab is numbered from its own start and picking one silently
    would answer a different question than the one asked.

    **``tab_ids`` is the DOCUMENT's tab inventory, never the tabs the passed
    records happen to occupy**, and it is a required argument for that
    reason. Counting the records was the same bug wearing the refusal as a
    disguise: a three-tab document whose cards all sit in tab B has exactly
    one tab "present" among its records, so the refusal did not fire and the
    omitted ``tab_id`` silently resolved to B. A caller that meant the
    default tab was then handed tab B's cards as "the suggestion(s) at the
    edited range" -- a wrong-tab answer arrived at through the very check
    that exists to prevent one. The three callers (the listing's range
    filter, ``get_doc_review_view``'s window, and the write path's
    range echo) each see a different record subset -- pending suggestions,
    every paragraph, all suggestions -- so records could not make them agree
    even in principle. ``read.tab_metadata``
    (:class:`gdocs_preview.preview_read.ReviewRead`) can, and does.
    """
    if tab_id is None:
        present = sorted({t for t in tab_ids if t})
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
