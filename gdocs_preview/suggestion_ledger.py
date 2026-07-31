"""Process-wide memory of what our own writes did to a document's suggestions.

Why this exists (empirical, ``llmux/runner/reports/20260730-211540.md``):
accepting or rejecting a suggestion garbage-collects **others**. SPEC §7 plus
invariant §11.1 I2: a suggestion whose last marked character disappears must
leave the registry, and accepting a deletion removes exactly such characters.
The next call naming a collaterally removed id then fails with

    the suggestion ID sug.bob.1 is invalid or the suggestion no longer exists
    Suggestion with ID sug.bob.1 does not exist.

which is indistinguishable from a typo'd id. The agent gets no signal that its
OWN prior action caused the invalidation, so it cannot learn the rule.

This module is the missing memory. Every review read feeds it the live
suggestion records (:func:`observe`); every resolution records what it removed
and -- by diffing the last observation against the read taken immediately
after the write -- which OTHER ids vanished alongside
(:func:`record_resolution`). :func:`explain_missing` then turns a bare "does
not exist" into a cause, and the write tools additionally report the collateral
in the accept/reject response so the error never has to happen at all.

**Honesty ladder.** :func:`explain_missing` answers with the strongest
evidence it actually has, and never more:

1. *resolved directly* -- we called accept/reject on that very id. Proven.
2. *collateral* -- the id was in the read taken before our resolution and
   absent from the read taken immediately after it. **Observed, not proven**:
   a concurrent editor could have removed it inside that window, so the
   wording states the observation first and offers the GC rule as the
   explanation.
3. *may have been removed* -- never seen to disappear, but we did resolve
   something on this document and resolving can remove others.
4. *never seen* -- no record at all **and the last read covered the whole
   document**, so most likely a wrong id. A degraded read cannot support even
   that: it returns one unnamed body and no tab ids, so an id it did not list
   may simply live in a tab it could not see, and the answer says so instead
   of diagnosing the caller's id.

State is keyed by ``(user_google_email, document_id)`` so a multi-user HTTP
deployment never attributes one caller's resolution to another, and is bounded
to :data:`MAX_DOCUMENTS` entries (oldest-touched evicted first) so a long-lived
server cannot grow without limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from gdocs_preview.address import ADDRESS_FIELDS

#: Documents tracked at once, per process. Eviction is oldest-touched-first.
MAX_DOCUMENTS = 64

#: Resolutions remembered per document. Older entries are dropped; their ids
#: then answer with the weaker "may have been removed" wording rather than a
#: fabricated cause.
MAX_RESOLUTIONS = 128

#: Record fields kept for the echo. Everything else the analysis layer
#: produces (replies, author blocks) is dropped here -- this cache exists to
#: answer "what did the suggestion I just resolved say?", and it is copied
#: straight into an LLM's context window.
#:
#: The ``(tab, segment)`` ids are NOT droppable, which is why they come from
#: :data:`gdocs_preview.address.ADDRESS_FIELDS` rather than being listed by
#: hand. They used to be dropped as metadata, and
#: ``manage_document_suggestion``'s ``resolved_suggestion`` echo -- built
#: straight from this record -- then handed an agent a bare index it would
#: aim a follow-up write at, in the body of the default tab.
_KEPT_FIELDS = (
    "suggestion_id",
    "type",
    "pre_text",
    "post_text",
    "context_before",
    "context_after",
    *ADDRESS_FIELDS,
    "summary_text",
    "status",
)


@dataclass
class Resolution:
    """How one suggestion id left the document, as far as we can tell."""

    suggestion_id: str
    #: The tool action that removed it: ``accept``, ``reject`` or
    #: ``suggest_doc_edit`` (a new same-author edit can absorb a neighbour
    #: by merge).
    action: str
    at: str
    #: The id WE acted on, when it is known. Only meaningful for collateral.
    cause: Optional[str] = None
    #: True: this id IS the one we acted on (proven). False: it disappeared
    #: alongside our write (observed) -- ``cause`` names that write's id when
    #: the API reported one.
    direct: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "action": self.action,
            "at": self.at,
            "cause": self.cause,
            "direct": self.direct,
        }


@dataclass(frozen=True)
class Snapshot:
    """What the last read saw -- and whether that was the whole document.

    The two travel together because every use of ``ids`` is a set difference
    whose meaning depends on ``complete``. "In the ledger and not in the read"
    is a removal only if the read could see where the id lives; "in the read
    and not in the ledger" is a NEW card only if the ledger's read could see
    where it lives. A bare ``frozenset`` invited both diffs to be taken
    against a snapshot that had never covered the tabs being differenced, and
    the answers were reported as observations either way.
    """

    ids: frozenset[str]
    #: Did the read behind these ids enumerate every tab and segment?
    complete: bool


@dataclass
class _Entry:
    #: suggestion id -> compact record, as of the most recent read.
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: suggestion id -> how it went away.
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    #: True once any read has been observed, so "we have never looked" is
    #: distinguishable from "we looked and it was not there".
    observed: bool = False
    #: Did the most recent observation see the whole document? A degraded
    #: read REPLACES ``records`` with only what it could see, so an id that
    #: silently left the ledger this way must not later be answered with
    #: "most likely the id is wrong".
    complete: bool = False


_entries: dict[tuple[str, str], _Entry] = {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _key(user_google_email: str, document_id: str) -> tuple[str, str]:
    return (user_google_email or "", document_id or "")


def _entry(user_google_email: str, document_id: str) -> _Entry:
    key = _key(user_google_email, document_id)
    entry = _entries.pop(key, None)
    if entry is None:
        entry = _Entry()
        while len(_entries) >= MAX_DOCUMENTS:
            _entries.pop(next(iter(_entries)))
    _entries[key] = entry  # re-insert: dict order is the LRU order
    return entry


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in _KEPT_FIELDS if k in record}


def observe(
    user_google_email: str,
    document_id: str,
    suggestions: Iterable[dict[str, Any]],
    *,
    complete: bool,
) -> None:
    """Record the live suggestions a read just returned (replaces the set).

    Every read tool and every post-write verification read calls this, which
    is what keeps the "before" picture fresh enough for the diff in
    :func:`record_resolution` to mean something.

    ``complete`` is
    :attr:`gdocs_preview.preview_read.ReviewRead.complete` -- did that read
    enumerate the whole document? It has no default: a call site that does not
    know how much of the document it just saw cannot record a snapshot other
    code will take differences against, and every consumer of
    :func:`snapshot` needs the answer.
    """
    entry = _entry(user_google_email, document_id)
    records: dict[str, dict[str, Any]] = {}
    for record in suggestions or []:
        sid = (record or {}).get("suggestion_id")
        if sid:
            records[str(sid)] = _compact(record)
    entry.records = records
    entry.observed = True
    entry.complete = complete


def snapshot(user_google_email: str, document_id: str) -> Optional[Snapshot]:
    """The last read's ids WITH its coverage, or ``None`` if we never looked."""
    entry = _entries.get(_key(user_google_email, document_id))
    if entry is None or not entry.observed:
        return None
    return Snapshot(ids=frozenset(entry.records), complete=entry.complete)


def record_of(
    user_google_email: str, document_id: str, suggestion_id: str
) -> Optional[dict[str, Any]]:
    """The compact record of one suggestion as of the last read."""
    entry = _entries.get(_key(user_google_email, document_id))
    if entry is None:
        return None
    record = entry.records.get(suggestion_id)
    return dict(record) if record else None


def record_resolution(
    user_google_email: str,
    document_id: str,
    action: str,
    suggestion_id: str,
    collateral: Iterable[str] = (),
) -> list[Resolution]:
    """Remember that ``action`` on ``suggestion_id`` removed those ids.

    ``collateral`` is the caller's before/after diff (ids listed before the
    write and missing from the read right after it), minus ``suggestion_id``
    itself. An empty ``suggestion_id`` records the collateral without naming
    a cause -- an edit whose own id the API never reported still removed
    something, and inventing an id for it would be a lie.

    Returns every :class:`Resolution` recorded by this call, the directly
    resolved one first, so the caller can render the same facts into its
    response.
    """
    entry = _entry(user_google_email, document_id)
    at = _now()
    recorded = [Resolution(suggestion_id, action, at)] if suggestion_id else []
    for sid in collateral:
        if sid and sid != suggestion_id:
            recorded.append(
                Resolution(sid, action, at, cause=suggestion_id or None, direct=False)
            )
    for resolution in recorded:
        entry.resolutions[resolution.suggestion_id] = resolution
        entry.records.pop(resolution.suggestion_id, None)
    while len(entry.resolutions) > MAX_RESOLUTIONS:
        entry.resolutions.pop(next(iter(entry.resolutions)))
    return recorded


def collateral_note(resolution: Resolution) -> str:
    """One sentence naming a collaterally removed suggestion, for a response."""
    if resolution.action == "suggest_doc_edit":
        merged = f" into {resolution.cause!r}" if resolution.cause else ""
        return (
            f"suggestion {resolution.suggestion_id!r} is gone: it was listed "
            f"before this edit and absent right after it -- an adjacent "
            f"same-author suggestion merges{merged}."
        )
    cause = (
        repr(resolution.cause) if resolution.cause else "the suggestion you resolved"
    )
    return (
        f"suggestion {resolution.suggestion_id!r} is gone: {resolution.action}ing "
        f"{cause} also removed it, because that removed the last character it "
        f"marked. Its comment thread went with it."
    )


def explain_missing(
    user_google_email: str, document_id: str, suggestion_id: str
) -> str:
    """Why ``suggestion_id`` no longer exists -- with the evidence we have.

    Always returns a sentence: the module docstring's honesty ladder decides
    which one. Causation is only claimed where it was observed, and the
    weaker branches say so in words.
    """
    entry = _entries.get(_key(user_google_email, document_id))
    reread = "Re-read the current ids with list_document_suggestions."

    if entry is None or not entry.observed:
        return (
            "This session has not read this document, so there is no record "
            "of the id. It may never have existed, or another editor (or an "
            f"earlier session) may have resolved it. {reread}"
        )

    resolution = entry.resolutions.get(suggestion_id)
    if resolution is not None and resolution.direct:
        return (
            f"You {resolution.action}ed it yourself at {resolution.at}; "
            f"resolving a suggestion removes it. {reread}"
        )
    if resolution is not None:
        if resolution.action == "suggest_doc_edit":
            merged = f", which created {resolution.cause!r}" if resolution.cause else ""
            return (
                f"It was still listed before your suggest_doc_edit at "
                f"{resolution.at}{merged} and gone from the read right after -- "
                f"an adjacent same-author suggestion merges into the new one. "
                f"{reread}"
            )
        cause = repr(resolution.cause) if resolution.cause else "another suggestion"
        return (
            f"It was still listed before you {resolution.action}ed {cause} at "
            f"{resolution.at} and gone from the read right after, so that "
            f"{resolution.action} removed it: {resolution.action}ing a "
            f"suggestion also deletes any other suggestion whose last marked "
            f"character disappears with it. {reread}"
        )

    if entry.resolutions:
        others = ", ".join(
            f"{r.suggestion_id!r} ({r.action}, {r.at})"
            for r in list(entry.resolutions.values())[-3:]
            if r.direct
        )
        if others:
            return (
                "No record of it being removed, so this is not proven -- but "
                f"you resolved {others} on this document in this session, and "
                "resolving a suggestion also deletes any other suggestion "
                "whose last marked character disappears with it. One of those "
                f"MAY have removed it. {reread}"
            )

    if suggestion_id in entry.records:
        return (
            "It WAS present in the last read of this document, so it was "
            "removed between that read and this call -- most likely by another "
            f"editor. {reread}"
        )
    if not entry.complete:
        # The last read was the GA fallback: one unnamed body, no tab ids, so
        # an id in another tab is missing from ``records`` because that read
        # could not see it. "Most likely the id is wrong" is a diagnosis of
        # the caller, drawn from a read that never looked where the id lives.
        return (
            "The last read of this document was degraded and could not see "
            "every tab, so the id's absence from it is not evidence about the "
            "id. No write of ours is recorded as removing it either. "
            f"{reread}"
        )
    return (
        "It was not in the last read of this document either, and no write of "
        "ours removed it: most likely the id is wrong. Ids come from "
        f"list_document_suggestions. {reread}"
    )


def reset() -> None:
    """Forget everything (used by tests)."""
    _entries.clear()
