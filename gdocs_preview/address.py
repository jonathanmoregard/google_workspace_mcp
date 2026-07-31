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

That bug class has now been found twice -- on the read path (summary cards)
and on the write path (the post-write echo and the resolution ledger). The
remedy is structural rather than vigilant: **this module is the only place
that projects an index into an agent-facing payload**, and it cannot project
one without its ``segment``/``segment_id``/``tab_id``. :func:`address_of`
returns all five fields or none; :func:`with_address` is how a payload
acquires them. A future edit that drops the pairing has to delete a field
from :data:`ADDRESS_FIELDS`, where the docstring above is looking at it.
"""

from __future__ import annotations

from typing import Any

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
