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
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.account_directory import HINT_PREVIEW_UNAVAILABLE, candidate_account_hint
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdocs_preview import preview_status, review_page, suggestion_ledger
from gdocs_preview.address import (
    in_range_scope,
    resolve_range_scope,
    with_address,
)
from gdocs_preview.analysis import (
    AMBIGUOUS_ANCHOR,
    ANCHOR_NOT_FOUND,
    BaseText,
    check_resolution,
    extract_suggestions_from_tabs,
    segment_base_texts,
)
from gdocs_preview.preview_read import (
    normalize_author,
    pending_thread_ids,
    read_for_review,
)

logger = logging.getLogger(__name__)

#: Echoed text is trimmed to this many characters. The verification block
#: lands in an LLM's context window on every single write, so it is a
#: receipt, not a debug dump.
ECHO_MAX_CHARS = 200

TRUNCATION_MARKER = "…"


def _doc_link(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _clip(text: Optional[str], limit: int = ECHO_MAX_CHARS) -> Optional[str]:
    """Trim for DISPLAY, marking the cut. ``None`` stays ``None``.

    **Clip what is shown; never what is compared.** Every call here must be
    the last thing that happens to a string before it enters the response
    dict. A clipped value that is then fed to ``in`` / ``==`` does not compare
    the document, it compares the first :data:`ECHO_MAX_CHARS` characters of
    it, and the answer is decided by the truncation rather than by the write.
    The post-write window handed to :func:`_verify_resolution` used to be
    clipped here while the comparison used the UNCLIPPED
    ``pre_text``/``post_text``, so accepting a deletion whose ``pre_text``
    exceeded the window reported ``matches_expectation: true`` on the
    destructive path without any check having occurred -- fail-open
    verification -- and the mirror case, a long replacement, raised a false
    alarm on a write that had landed perfectly, which an agent may "fix" by
    re-suggesting into a customer document.

    The verdict now comes from
    :func:`gdocs_preview.analysis.check_resolution`, which never sees a
    clipped value: this function runs on the way out and nowhere else.
    """
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + TRUNCATION_MARKER


def _echo_suggestion(record: dict[str, Any]) -> dict[str, Any]:
    """One analysis record, trimmed to what "did my edit land?" needs.

    ``pre_text``/``post_text`` carry :mod:`gdocs_preview.analysis`'s
    semantics unchanged -- the base text of the affected range, and that
    range with this suggestion (and only this one) applied -- which is
    exactly the before/after an agent would otherwise have to re-read for.

    The indexes come through :func:`gdocs_preview.address.with_address`, so
    they arrive with ``segment``/``segment_id``/``tab_id`` or not at all.
    This echo is the agent's ONLY record of the suggestion it just made, and
    the next thing it does with it is hand the numbers to
    ``create_anchored_doc_comment`` or ``suggest_doc_edit`` -- both of which
    default to the body of the default tab. A header echo reading
    ``start_index: 5`` with no ``segment_id`` therefore aims the follow-up
    write at character 5 of the BODY of a customer document, silently: index
    0 fails loud on the floor check, every other index does not.
    """
    return with_address(
        {
            "suggestion_id": record.get("suggestion_id"),
            "type": record.get("type"),
            "pre_text": _clip(record.get("pre_text")),
            "post_text": _clip(record.get("post_text")),
            "context_before": record.get("context_before"),
            "context_after": record.get("context_after"),
            "summary_text": record.get("summary_text"),
            "status": record.get("status"),
        },
        record,
    )


class _PostWriteRead:
    """The one read a write tool makes to verify itself.

    Not a dataclass because ``records`` and ``base_texts`` are derived
    from the same payload and must not drift apart.

    ``base_texts`` is keyed by ``(tab_id, segment_id)`` -- the coordinate
    space, not the document -- because that is the only text a record's
    indexes and context windows mean anything against. A merged
    whole-document string (what ``render_tabs`` produces) concatenates every
    tab's body and drops headers, footers and footnotes entirely, so
    locating a header resolution in it either finds nothing or finds the
    same words somewhere else and calls that a match.

    It holds :class:`~gdocs_preview.analysis.BaseText`, NOT rendered text.
    It used to hold ``render_document(...)["body_text"]`` and the
    header/footer/footnote maps beside it -- CriticMarkup-marked strings --
    while every value compared against them (``pre_text``, ``post_text``,
    ``context_before``) is base text, so the verification was a comparison
    between two different representations of the same document. Its three
    failure modes were all reachable from prod and none from the mock:
    a deletion spanning two runs (prod splits a ``textRun`` at every style
    boundary, so "brave new" renders ``{-brave-}{- new-}``) could not be
    found, and "not found" was the evidence an accept had removed it; a
    still-pending neighbour inside the window broke the mirror comparison and
    raised a false alarm on a write that had landed; and a marker inside the
    40-character anchor made the range unlocatable, which was then reported
    as "the likeliest cause is a concurrent edit by another editor".

    **It also knows what it did NOT see.** ``complete`` and
    ``observed_spaces`` are carried alongside the records because absence is
    only evidence when the read looked: the GA fallback returns one unnamed
    body and no tab ids at all, so a card in ``t.0`` is missing from
    ``records`` without having gone anywhere. Every membership question
    therefore goes through :meth:`pending_state`, which answers UNKNOWN for a
    space this read never observed rather than ``False``. ``x in
    read.records`` used to be asked directly in three places -- the resolution
    verdict and both collateral diffs -- and each of them turned a blind read
    into a confident claim about a customer's document.
    """

    def __init__(self, read: Any) -> None:
        self.source: str = read.source
        #: Did this read enumerate the WHOLE document -- every tab, every
        #: segment? Taken straight off
        #: :class:`~gdocs_preview.preview_read.ReviewRead` rather than
        #: re-derived from ``source`` here: the read is the authority on its
        #: own coverage, and a second opinion about it is a second thing to
        #: keep in sync.
        self.complete: bool = bool(getattr(read, "complete", False))
        analysed = extract_suggestions_from_tabs(read.tabs, read.threads)
        self.records: dict[str, dict[str, Any]] = {
            r["suggestion_id"]: r for r in analysed["suggestions"]
        }
        #: The API's OWN pending set, which is wider than ``records``.
        #: ``records`` comes from walking the document's content marks, and a
        #: paragraph-style, bullet or table row/cell-style suggestion leaves
        #: none (``docs/findings/coverage.md``, measured 2026-08-02). Rejecting
        #: such a card and re-reading therefore found it absent from
        #: ``records`` -- because it was never IN ``records`` -- and
        #: :meth:`pending_state` answered ``False``: "the reject landed" said
        #: about a suggestion still sitting OPEN in the document, on the one
        #: destructive path this package has, and derived from a read that had
        #: the contradicting evidence in hand. An id the API lists as pending
        #: is pending, whether or not this package can describe it.
        #: The raw suggestion threads this read carried, kept so the response
        #: can NAME the unmodelled cards rather than only counting them --
        #: ``summaryText`` is Google's own label and the only place their kind
        #: appears anywhere in the payload. Empty on a degraded read, which is
        #: why every consumer of it is gated on :attr:`complete`.
        self.threads: dict[str, dict[str, Any]] = dict(
            getattr(read, "threads", None) or {}
        )
        self.pending_thread_ids: frozenset[str] = pending_thread_ids(self.threads)
        #: Every tab this read saw, whether or not it holds a suggestion.
        #: This is the inventory :func:`resolve_range_scope` refuses against,
        #: and it must not be re-derived from ``records``: a multi-tab
        #: document whose pending cards all sit in one tab would then look
        #: single-tab, and the echo would name that tab's suggestions as the
        #: ones the edit landed on.
        self.tab_ids: list[Optional[str]] = [tab_id for tab_id, _ in read.tabs]
        self.base_texts: dict[tuple[Optional[str], Optional[str]], BaseText] = {}
        for tab_id, document in read.tabs:
            self.base_texts.update(segment_base_texts(document, tab_id=tab_id))

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(self.records)

    @property
    def observed_spaces(self) -> frozenset[tuple[Optional[str], Optional[str]]]:
        """The ``(tab_id, segment_id)`` spaces this read actually walked."""
        return frozenset(self.base_texts)

    def space_of(
        self, record: dict[str, Any]
    ) -> tuple[Optional[tuple[Optional[str], Optional[str]]], Optional[str]]:
        """The ONE ``(tab_id, segment_id)`` ``record`` is numbered in.

        Returns ``(key, None)`` or ``(None, reason)``. A record that names no
        tab is resolved the way
        :func:`gdocs_preview.address.resolve_range_scope` resolves an omitted
        ``tab_id``: implicitly when the read has one tab, and not at all when
        it has several -- a guessed tab makes every answer derived from it a
        statement about a different part of the document.
        """
        segment_id = record.get("segment_id") or None
        tab_id = record.get("tab_id") or None
        if tab_id is None:
            candidates = sorted({tab for tab in self.tab_ids if tab})
            if len(candidates) > 1:
                return None, "ambiguous_tab"
            tab_id = candidates[0] if candidates else None
        return (tab_id, segment_id), None

    def text_at(
        self, record: dict[str, Any]
    ) -> tuple[Optional[BaseText], Optional[str]]:
        """The base text of the ONE ``(tab, segment)`` ``record`` lives in.

        Returns ``(text, None)``, or ``(None, reason)`` naming why this read
        cannot say -- the space (:meth:`space_of`) or the text in it.

        The reason is returned rather than swallowed because a bare ``None``
        window is three different situations wearing one face, and an agent
        acts differently on each: an ambiguous multi-tab read is fixed by
        naming the tab, a degraded read is fixed by retrying, and a missing
        anchor means somebody else edited the document. Reporting all three
        as ``resulting_text: null`` also made them indistinguishable from
        "we never listed this id" -- which is the one case where nothing is
        wrong at all.
        """
        key, reason = self.space_of(record)
        if key is None:
            return None, reason
        text = self.base_texts.get(key)
        if text is None:
            return None, "segment_not_in_read"
        return text, None

    def pending_state(
        self, suggestion_id: str, record: Optional[dict[str, Any]]
    ) -> tuple[Optional[bool], Optional[str]]:
        """Is ``suggestion_id`` STILL in the document's pending set?

        Returns ``(True, None)``, ``(False, None)`` or ``(None, reason)`` --
        and the third answer is the point of this method. Presence is decisive
        whatever the read's coverage: an id this read LISTED is pending, full
        stop. Absence is decisive only if the read looked where the id lives,
        which a complete read did for every id and a degraded read did for
        nothing outside :attr:`observed_spaces`.

        ``record`` is the card as the ledger listed it -- the only thing that
        says which ``(tab, segment)`` the id belongs to. Without it, a read
        that did not cover the document cannot even name the space it failed
        to look in, so the answer is UNKNOWN rather than a ``False`` nobody
        checked.

        Presence is asked of BOTH the modelled records and the API's own
        pending threads (:attr:`pending_thread_ids`), because ``records`` is
        the narrower set: it is built by walking content marks, and the kinds
        that leave none were invisible to this method entirely -- reported
        ``False``, "gone", from a read that was listing them as OPEN.
        """
        if suggestion_id in self.records or suggestion_id in self.pending_thread_ids:
            return True, None
        if self.complete:
            return False, None
        if record is not None:
            key, _ = self.space_of(record)
            if key is not None:
                if key in self.observed_spaces:
                    return False, None
                return None, "segment_not_in_read"
        return None, "read_incomplete"

    def absences(
        self,
        suggestion_ids: Iterable[str],
        records: dict[str, Optional[dict[str, Any]]],
    ) -> tuple[list[str], list[str], list[str]]:
        """Split ids into (gone -- checked), (still pending), (cannot say).

        The input is a ledger-minus-read difference, which is a candidate list
        and not a finding: on a degraded read every id in a tab the read
        cannot see drops out of that subtraction without having gone anywhere.
        Only the first list may be reported as removed, or RECORDED as removed
        -- :func:`gdocs_preview.suggestion_ledger.record_resolution` pops what
        it is given, so a fabricated removal outlives the response it appeared
        in and is repeated by ``explain_missing`` later.

        **The middle list is a fact, not a gap.** It used to be folded into
        the third one: everything that was not a decided ``False`` went into
        "this read cannot say", and the sentence written for that pile asserts
        *the read did not cover the whole document* -- which is FALSE about a
        complete read. :meth:`pending_state` answers ``True`` for an id the
        API still lists as OPEN but the analysis layer no longer describes
        (``docs/findings/coverage.md``), and that id has been observed, not
        missed: it is still there. "Still present but unmodelled" and "we
        could not look" are opposite facts about the read and must not share a
        sentence. The third list is now reachable only from
        ``complete is False``, which is what makes its sentence true.
        """
        gone: list[str] = []
        still_pending: list[str] = []
        unattested: list[str] = []
        for sid in sorted(suggestion_ids):
            state, _ = self.pending_state(sid, records.get(sid))
            if state is False:
                gone.append(sid)
            elif state is True:
                still_pending.append(sid)
            else:
                unattested.append(sid)
        return gone, still_pending, unattested


async def _post_write_read(
    service: Any, document_id: str, *, user_google_email: str
) -> tuple[Optional[_PostWriteRead], Optional[str]]:
    """Read the document back once, in SUGGESTIONS_INLINE view.

    Returns ``(read, None)`` or ``(None, reason)``. Failures are RETURNED,
    never raised: the write already landed, and a verification problem must
    not turn a successful mutation into an error the agent will try to
    "fix" by writing again. The broad ``except`` is deliberate for the same
    reason -- there is no failure mode here worth failing the tool over.
    """
    try:
        read = await read_for_review(
            service,
            document_id,
            "SUGGESTIONS_INLINE",
            user_google_email=user_google_email,
        )
        return _PostWriteRead(read), None
    except Exception as error:  # noqa: BLE001 - see docstring
        logger.info(
            f"[docs_preview] post-write verification read failed for "
            f"{document_id}: {error}"
        )
        return None, f"{type(error).__name__}: {error}"[:200]


#: The reason codes :func:`_verify_resolution` reports as
#: ``resulting_text_unavailable``. Every one of them means the same thing
#: about the verdict -- ``matches_expectation: null`` because NO CHECK RAN --
#: and a different thing about what the caller should do next, which is the
#: whole reason they are separate values instead of one silent ``null``.
UNLOCATED_REASONS = (
    "suggestion_not_listed",
    "ambiguous_tab",
    "segment_not_in_read",
    ANCHOR_NOT_FOUND,
    AMBIGUOUS_ANCHOR,
    "nothing_to_compare",
)

_NOT_A_FAILED_WRITE = (
    "No TEXT comparison could be made, which is a statement about the READ, "
    "not about the write: `still_pending` is the evidence about whether the "
    "resolution itself took effect."
)

#: The same sentence when ``still_pending`` is ALSO null. Round 6: every note
#: below pointed the agent at ``still_pending`` as the evidence, and a read
#: that could not see the suggestion's tab set that field from a subtraction
#: it had no standing to make -- so the field the prose called "the evidence"
#: was a `false` nobody had checked. It is now null in that case, and a note
#: cannot go on calling a null field the evidence.
_NOTHING_REPORTS_ON_THE_WRITE = (
    "No TEXT comparison could be made, and this read could not see the "
    "pending set the suggestion lives in either, so NOTHING in this response "
    "reports on whether the resolution took effect -- see "
    "`still_pending_unavailable`."
)


def _not_a_failed_write(still_pending: Optional[bool]) -> str:
    """What an unlocated window means, given what the pending set could say."""
    return (
        _NOTHING_REPORTS_ON_THE_WRITE if still_pending is None else _NOT_A_FAILED_WRITE
    )


def _note_suggestion_not_listed(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    return (
        f"this session never listed suggestion {suggestion_id!r}, so there "
        f"is no expected text to compare the document against. "
        f"{_not_a_failed_write(still_pending)} Call list_document_suggestions "
        "before resolving to get the before/after text of the card."
    )


def _note_ambiguous_tab(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    tabs = sorted({t for t in read.tab_ids if t})
    return (
        f"suggestion {suggestion_id!r} was listed without a tab_id and this "
        f"read has {len(tabs)} tabs ({', '.join(tabs)}), so the text at its "
        "range cannot be located: an index means a different place in each "
        f"tab. {_not_a_failed_write(still_pending)} Re-read with "
        "list_document_suggestions(tab_id=...) to see the resulting text."
    )


def _note_segment_not_in_read(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    where = (
        f"tab={(record or {}).get('tab_id')!r}, "
        f"segment={(record or {}).get('segment_id')!r}"
    )
    return (
        f"the post-write read does not contain the ({where}) that "
        f"suggestion {suggestion_id!r} was listed in, so its range could "
        f"not be located -- read_source is {read.source!r}, and the GA "
        "documents.get carries no tabs at all, so a read that degraded "
        f"loses every tab id. {_not_a_failed_write(still_pending)} "
        "Retry the read."
    )


def _note_anchor_not_found(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    return (
        f"the base text immediately before suggestion {suggestion_id!r}'s range "
        "is no longer in that segment, so the range could not be located -- "
        "that text is unaffected by accepting or rejecting, so the likeliest "
        "cause is a concurrent edit by another editor between the listing and "
        f"this write. {_not_a_failed_write(still_pending)} Re-read the "
        "document to see its current state."
    )


def _note_ambiguous_anchor(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    return (
        f"the base text around suggestion {suggestion_id!r}'s range repeats "
        "in that segment, so more than one place reads as its range and "
        "they do not agree on whether the resolution landed. No verdict is "
        f"reported rather than one picked at random. "
        f"{_not_a_failed_write(still_pending)} "
        "Read the range back with get_doc_review_view(start_index=..., "
        "end_index=...) to see the one place you resolved."
    )


def _note_nothing_to_compare(
    *, suggestion_id: str, record: Any, read: Any, still_pending: Optional[bool]
) -> str:
    return (
        f"suggestion {suggestion_id!r} was listed without the before/after "
        "text a resolution is checked against, so there was nothing to "
        f"compare the document to. {_not_a_failed_write(still_pending)} Call "
        "list_document_suggestions(fields='full') before resolving."
    )


#: One sentence per reason code, dispatched rather than fallen through to.
#: :func:`_unlocated_note` used to be an if-chain whose final unguarded
#: ``return`` was the ``anchor_not_found`` sentence, so a reason nobody had
#: written a sentence for did not go quiet -- it was answered with "the
#: likeliest cause is a concurrent edit by another editor", a confident
#: diagnosis of a situation that had not been diagnosed at all. A mapping
#: cannot fall through, and the completeness check below turns "a reason with
#: no sentence" into an ImportError rather than a wrong story about a
#: customer's document.
_UNLOCATED_NOTES = {
    "suggestion_not_listed": _note_suggestion_not_listed,
    "ambiguous_tab": _note_ambiguous_tab,
    "segment_not_in_read": _note_segment_not_in_read,
    ANCHOR_NOT_FOUND: _note_anchor_not_found,
    AMBIGUOUS_ANCHOR: _note_ambiguous_anchor,
    "nothing_to_compare": _note_nothing_to_compare,
}

if set(_UNLOCATED_NOTES) != set(UNLOCATED_REASONS):  # pragma: no cover - import guard
    raise RuntimeError(
        "every reason in UNLOCATED_REASONS needs its own sentence in "
        "_UNLOCATED_NOTES (and vice versa); the difference is "
        f"{set(_UNLOCATED_NOTES) ^ set(UNLOCATED_REASONS)}"
    )


def _unlocated_note(
    reason: str,
    *,
    suggestion_id: str,
    record: Optional[dict[str, Any]],
    read: "_PostWriteRead",
    still_pending: Optional[bool],
) -> str:
    """One sentence saying why no text comparison was made, and what to do.

    ``still_pending`` is the verdict's OTHER half, and it is passed in rather
    than looked up because every sentence here ends by pointing at it: when it
    is null there is nothing to point at, and the note has to say that instead.
    """
    builder = _UNLOCATED_NOTES.get(reason)
    if builder is None:
        # A reason from outside UNLOCATED_REASONS. Logged rather than raised:
        # this runs after a landed write, and turning a verification gap into
        # an exception hands the agent a failure for a mutation that
        # succeeded -- see _post_write_read. The import guard above is where
        # this is meant to be caught.
        logger.error(
            f"[docs_preview] no note is written for unlocated reason "
            f"{reason!r}; add one to _UNLOCATED_NOTES"
        )
        return (
            f"the text at suggestion {suggestion_id!r}'s range could not be "
            f"compared, and the reason given ({reason!r}) is one this build "
            f"has no explanation for. {_not_a_failed_write(still_pending)} "
            "Re-read the document to see its current state."
        )
    return builder(
        suggestion_id=suggestion_id,
        record=record,
        read=read,
        still_pending=still_pending,
    )


#: The reason codes :func:`_verify_resolution` reports as
#: ``still_pending_unavailable``: the post-write read could not say whether
#: the id is in the document's pending set, because it did not look where the
#: id lives. Both are only reachable from a read that degraded
#: (:attr:`_PostWriteRead.complete` false); a complete read answers every id.
PENDING_UNKNOWN_REASONS = ("segment_not_in_read", "read_incomplete")

#: EVERY value ``still_pending_unavailable`` can take. The two above are what a
#: READ reports when it could not look where the id lives; ``not_verified`` is
#: what the two verify-less returns report, where no read happened at all. It
#: was emitted without being in any vocabulary and without appearing in the
#: tool's documented enum, so a client branching on the documented values fell
#: through the one case where nothing checked the write. It is deliberately NOT
#: in :data:`PENDING_UNKNOWN_REASONS`, whose members each own a sentence in
#: ``_PENDING_UNKNOWN_NOTES`` describing what the read missed -- there is no
#: read here to describe, and :func:`_unverified_note` says so instead.
STILL_PENDING_UNAVAILABLE_REASONS = PENDING_UNKNOWN_REASONS + ("not_verified",)


def _note_pending_segment_not_in_read(
    *, action: str, suggestion_id: str, record: Any, read: Any
) -> str:
    where = (
        f"tab={(record or {}).get('tab_id')!r}, "
        f"segment={(record or {}).get('segment_id')!r}"
    )
    return (
        f"whether suggestion {suggestion_id!r} is still pending is UNKNOWN: "
        f"it was listed in ({where}) and this post-write read did not cover "
        f"that space -- read_source is {read.source!r}, and the GA "
        "documents.get carries no tabs at all, so a read that degraded loses "
        "every tab id. The id is absent from this read, and that absence is "
        f"NOT evidence that the {action} landed: a read that cannot see the "
        "card's tab cannot see the card. Re-read with "
        "list_document_suggestions before treating it as resolved, and do "
        f"not repeat the {action} on the strength of this response."
    )


def _note_pending_read_incomplete(
    *, action: str, suggestion_id: str, record: Any, read: Any
) -> str:
    return (
        f"whether suggestion {suggestion_id!r} is still pending is UNKNOWN: "
        f"this post-write read did not cover the whole document "
        f"(read_source={read.source!r}) and this session never listed the id, "
        "so there is nothing saying which tab it lives in and no way to tell "
        "whether this read looked there. Its absence from the read is "
        f"therefore not evidence that the {action} landed. Call "
        "list_document_suggestions to see the document's real pending set."
    )


#: Same discipline as :data:`_UNLOCATED_NOTES`: a mapping, so a reason cannot
#: fall through into somebody else's diagnosis, plus an import-time guard so a
#: reason without a sentence is a build failure rather than a silent null.
_PENDING_UNKNOWN_NOTES = {
    "segment_not_in_read": _note_pending_segment_not_in_read,
    "read_incomplete": _note_pending_read_incomplete,
}

if set(_PENDING_UNKNOWN_NOTES) != set(  # pragma: no cover - import guard
    PENDING_UNKNOWN_REASONS
):
    raise RuntimeError(
        "every reason in PENDING_UNKNOWN_REASONS needs its own sentence in "
        "_PENDING_UNKNOWN_NOTES (and vice versa); the difference is "
        f"{set(_PENDING_UNKNOWN_NOTES) ^ set(PENDING_UNKNOWN_REASONS)}"
    )


def _pending_unknown_note(
    reason: str,
    *,
    action: str,
    suggestion_id: str,
    record: Optional[dict[str, Any]],
    read: "_PostWriteRead",
) -> str:
    """One sentence for a pending set this read had no standing to report."""
    builder = _PENDING_UNKNOWN_NOTES.get(reason)
    if builder is None:  # pragma: no cover - the guard above is the catch
        logger.error(
            f"[docs_preview] no note is written for pending-unknown reason "
            f"{reason!r}; add one to _PENDING_UNKNOWN_NOTES"
        )
        return (
            f"whether suggestion {suggestion_id!r} is still pending is "
            f"UNKNOWN, and the reason given ({reason!r}) is one this build "
            "has no explanation for. Re-read with list_document_suggestions."
        )
    return builder(action=action, suggestion_id=suggestion_id, record=record, read=read)


def _matches_expectation(
    still_pending: Optional[bool], text_check: Optional[bool]
) -> Optional[bool]:
    """The ONE rule turning the two pieces of evidence into a verdict.

    It is a module function rather than a method so that
    :meth:`_ResolutionVerdict.derive` and :meth:`_ResolutionVerdict.
    __post_init__` cannot drift: the constructor checks the field against the
    same rule the factory used, so any hand-built verdict that is not entailed
    by its own evidence raises.
    """
    if still_pending is None:
        # The pending set was not observed. The text cannot decide alone: for
        # a reject, and for a style-only accept, it reads identically whether
        # or not the resolution landed.
        return None
    if still_pending:
        return False
    return text_check


@dataclass(frozen=True)
class _ResolutionVerdict:
    """``still_pending`` and ``matches_expectation``, derived TOGETHER.

    The two used to be assembled independently -- a structural fact and a
    text comparison, each written into the response block on its own line --
    so they could contradict each other, and on a reject they always did.
    Base text is IDENTICAL whether or not a reject took effect: the
    suggestion's insertion is stripped from base text either way
    (:func:`gdocs_preview.analysis._collect_segments`) and its deletion kept
    either way, so :func:`~gdocs_preview.analysis.check_resolution` reads the
    expected text in both worlds and the API's documented HTTP-200-no-op
    emitted ``{"still_pending": true, "matches_expectation": true}`` -- a
    positive verdict standing beside the evidence that contradicts it. The
    accept half had the same hole for a style-only suggestion, whose
    ``pre_text`` and ``post_text`` are one string.

    Membership of the post-write pending set is decisive for BOTH actions: an
    accepted or rejected suggestion is gone from it, so an id that is still
    there is a resolution that did not take effect, whatever the text reads.
    :meth:`derive` is therefore the only producer of the pair -- the text
    check can answer the question the pending set leaves open and can never
    overrule it -- and :meth:`__post_init__` makes the contradictory pair
    unrepresentable. FOUR consecutive review rounds found this verification
    reporting success on a write it had not verified, each time through a
    different branch; deriving the verdict from the evidence rather than
    comparing the two afterwards is what retires the branch.

    **Round 6: that was necessary and not sufficient.** Making the pair
    consistent says nothing about whether its INPUTS were founded, and
    ``still_pending`` was computed as ``suggestion_id in read.records`` --
    which on a read that structurally cannot see the suggestion's tab (the GA
    fallback carries no tab ids at all) is ``False`` for a card that never
    moved. The derivation then faithfully turned an unfounded input into a
    confident output, on the destructive path. So ``still_pending`` is now
    ``Optional[bool]``: UNKNOWN is a first-class input
    (:meth:`_PostWriteRead.pending_state`) that this type must PROPAGATE
    rather than collapse. A positive verdict requires the pending set to have
    been observed and the id to have been absent from it; nothing weaker can
    be spelled.
    """

    #: Is the resolved id STILL in the post-write pending set? ``None`` when
    #: the read could not see the space the id lives in --
    #: :data:`PENDING_UNKNOWN_REASONS` names which way.
    still_pending: Optional[bool]
    #: What :func:`~gdocs_preview.analysis.check_resolution` said about the
    #: text at the range, or ``None`` when no comparison could be made.
    text_check: Optional[bool]
    #: The agent-facing verdict, which is never positive unless the pending
    #: set was observed and the id was gone from it.
    matches_expectation: Optional[bool]

    def __post_init__(self) -> None:
        entailed = _matches_expectation(self.still_pending, self.text_check)
        if self.matches_expectation is not entailed:
            raise ValueError(
                f"matches_expectation={self.matches_expectation!r} is not "
                f"what still_pending={self.still_pending!r} and "
                f"text_check={self.text_check!r} entail ({entailed!r}). "
                "Build this with _ResolutionVerdict.derive(), which cannot "
                "produce an unentailed verdict."
            )

    @classmethod
    def derive(
        cls, *, still_pending: Optional[bool], text_check: Optional[bool]
    ) -> "_ResolutionVerdict":
        """The verdict the two pieces of evidence add up to.

        A pending id is a decided ``False``; only once it is OBSERVED to be
        gone does the text decide, and a text check that could not run stays
        ``None`` there (with :data:`UNLOCATED_REASONS` naming why). A pending
        set nobody could see decides nothing at all -- the text cannot stand
        in for it, because for a reject, and for a style-only accept, base
        text reads the same in both worlds.
        """
        return cls(
            still_pending=still_pending,
            text_check=text_check,
            matches_expectation=_matches_expectation(still_pending, text_check),
        )

    @classmethod
    def unknown(cls) -> "_ResolutionVerdict":
        """The verdict that claims nothing. Always representable, by
        construction -- which is what makes it a safe degrade target for the
        post-write path (see :func:`_verify_resolution`)."""
        return cls(still_pending=None, text_check=None, matches_expectation=None)


def _still_pending_note(
    action: str, suggestion_id: str, text_check: Optional[bool]
) -> str:
    """One sentence for a resolution the pending set says did not happen."""
    note = (
        f"suggestion {suggestion_id!r} is STILL in the document's pending set "
        f"after this {action}, so the resolution did not take effect and "
        "matches_expectation is false on that evidence. The preview API can "
        "answer a resolution with HTTP 200 and no effect -- an id that no "
        "longer resolves is one way -- so the response ids alone do not say "
        "the write landed. Re-read with list_document_suggestions before "
        "treating the card as resolved."
    )
    if text_check is True:
        note += (
            " The text at its range does read what the card promised, which "
            "is not evidence here: for some suggestions the base text is the "
            "same in both worlds -- a rejected insertion is absent from it "
            "either way, a rejected deletion present either way, and a "
            "style-only suggestion's before and after are one string -- so "
            "only the pending set separates a resolution that landed from "
            "one that did not."
        )
    return note


#: What each ``commentUpdateState`` says about "was the thread update
#: persisted?". A state the API did NOT send is absent from this mapping, and
#: absence answers ``None``: it is not a report of failure.
#: ``ALL_FAILED_UNKNOWN_REASON`` never reaches a verdict --
#: :func:`_execute_preview_batch_update` raises on it with
#: ``enforce_comment_update=True`` -- but it is spelled out because the
#: mapping is the definition of the field, not a lookup table for the paths
#: that happen to be reachable today.
_COMMENT_UPDATE_SAVED: dict[str, bool] = {
    "ALL_SAVED": True,
    "ALL_FAILED_UNKNOWN_REASON": False,
}


def _saved_from_state(comment_update_state: Optional[str]) -> Optional[bool]:
    """Did the thread update save? Only ``commentUpdateState`` can say."""
    return _COMMENT_UPDATE_SAVED.get(comment_update_state or "")


@dataclass(frozen=True)
class _ThreadWriteVerdict:
    """``saved`` for a thread write -- derived from the state, never asserted.

    ``reply_to_doc_thread`` and ``create_anchored_doc_comment`` verify for
    free off the batchUpdate response, and both used to write
    ``"saved": comment_update_state == "ALL_SAVED"``. That is an ASSERTION
    wearing a comparison: an absent state produced ``saved: false`` beside a
    fully populated stored Post -- ``post_id``, ``create_time``, and
    ``stored_content`` equal to what was sent -- which is a report of failure
    for a write that most likely landed. The reply an agent then sends again
    is a duplicate in a customer's document, and duplicate comments cannot be
    un-sent by re-reading.

    So the field is three-valued, on the same rule
    :class:`_ResolutionVerdict` follows: the API's own ``commentUpdateState``
    is the only evidence about persistence, and where it is silent so is this.
    The stored Post is reported alongside as what it is -- evidence about the
    CONTENT (``matches_request``), not about the save.
    """

    comment_update_state: Optional[str]
    saved: Optional[bool]

    def __post_init__(self) -> None:
        entailed = _saved_from_state(self.comment_update_state)
        if self.saved is not entailed:
            raise ValueError(
                f"saved={self.saved!r} is not what "
                f"comment_update_state={self.comment_update_state!r} entails "
                f"({entailed!r}). Build this with _ThreadWriteVerdict.derive()."
            )

    @classmethod
    def derive(cls, comment_update_state: Optional[str]) -> "_ThreadWriteVerdict":
        return cls(
            comment_update_state=comment_update_state,
            saved=_saved_from_state(comment_update_state),
        )

    @property
    def unavailable_reason(self) -> str:
        """Why ``saved`` is null: no state at all, or one we cannot read."""
        return (
            "no_comment_update_state"
            if self.comment_update_state is None
            else "unrecognized_comment_update_state"
        )

    def note(self, *, what: str, post_id: Optional[str]) -> str:
        """One sentence for a save nothing reported on, and what NOT to do."""
        # Two different situations reach a null ``saved``, and both used to be
        # narrated as "this response carries no commentUpdateState" -- printed
        # directly beside ``comment_update_state: "<the value>"`` in the same
        # JSON when the API sent a state this build does not model. A response
        # contradicting itself about a field it is carrying is worse than
        # either fact alone.
        opening = (
            "this response carries no commentUpdateState"
            if self.comment_update_state is None
            else (
                f"this response carries commentUpdateState "
                f"{self.comment_update_state!r}, which this build does not "
                "recognise as either saved or failed"
            )
        )
        return (
            f"{opening}, which is the only "
            f"thing that reports whether the {what} was persisted, so whether "
            "it saved is UNKNOWN -- not false. "
            + (
                f"The API did return a stored post ({post_id!r}), and "
                "stored_content/matches_request compare it with what was "
                "sent -- but that is evidence about the CONTENT, not about "
                "the save. "
                if post_id
                else "No stored post came back either. "
            )
            + f"Do NOT repeat the {what} on the strength of this response: "
            "check with list_document_comments or get_doc_review_view first, "
            "because a retry that was not needed leaves a duplicate in the "
            "document."
        )


def _partial_pending_note(read: "_PostWriteRead") -> str:
    """One sentence for a pending count that is a count of what was SEEN.

    ``pending_suggestion_count`` / ``pending_suggestion_ids`` are the same
    shape of claim as ``still_pending``, one level up: they read as the
    document's pending set, and off a degraded read they are one tab's. An
    agent that sees ``pending_suggestion_count: 0`` stops reviewing.
    """
    return (
        f"pending_suggestion_count and pending_suggestion_ids are what this "
        f"read saw, not what the document holds: read_source is "
        f"{read.source!r}, the GA documents.get returns one unnamed body with "
        "no tab ids, so cards in any other tab are missing from both. A count "
        "of 0 here does NOT mean the document has no pending suggestions. "
        "Re-read with list_document_suggestions."
    )


def _attach_unreported_pending(
    verification: dict[str, Any], read: "_PostWriteRead"
) -> dict[str, Any]:
    """The unmodelled remainder of the pending set, beside the two counts.

    ``pending_suggestion_count`` and ``pending_suggestion_ids`` are the
    MODELLED set (``len(read.records)`` / ``sorted(read.records)``), while
    ``still_pending`` beside them consults :attr:`_PostWriteRead.
    pending_thread_ids` -- the API's OWN inventory of OPEN cards, which is
    wider (``docs/findings/coverage.md``). The two therefore disagreed inside
    one response: a **complete** post-write read of a document holding an
    unmodelled OPEN card returned ``pending_suggestion_count: 0`` and
    ``pending_suggestion_ids: []`` with no ``pending_suggestions_are_partial``
    to qualify them -- the read WAS complete -- and could print
    ``still_pending: true`` beside a ``pending_suggestion_ids`` omitting that
    very id, on the destructive path.

    The fix is the read tools' (:func:`gdocs_preview.review_page.
    attach_unreported`), reused rather than re-derived so the two surfaces
    cannot answer the same question differently: the counts keep their
    meaning, and the remainder is emitted beside them with its own notice and
    Google's own labels for the kinds involved. A caller can then reconcile
    ``still_pending`` with the accounting -- the id is in one list or the
    other -- and ``unreported_suggestion_count`` is refused (null +
    ``read_degraded``) on a read that carries no thread array to subtract from.
    """
    return review_page.attach_unreported(
        verification,
        threads=read.threads,
        reported_ids=list(read.records),
        complete=read.complete,
    )


def _unverified_note(action: str, suggestion_id: str, why: str) -> str:
    """One sentence for a resolution nothing checked.

    ``{"source": "skipped"}`` and ``{"source": "unavailable"}`` used to be the
    whole story on these two paths, and the response around them is
    byte-for-byte the shape prod returns for a resolution that resolved
    NOTHING: ``rejected_suggestion_ids: ["sug.x"]`` and
    ``comment_update_state: "ALL_SAVED"`` beside them, with nothing saying the
    ids are a receipt for the REQUEST rather than for its effect. The warning
    that used to fire only on the verified path fires here too, because here
    is where there is least to go on.
    """
    return (
        f"nothing verified this {action}: {why}. The preview API can answer a "
        "resolution with HTTP 200 and no effect -- an id that no longer "
        f"resolves is one way -- so {action}ed_suggestion_ids and "
        "comment_update_state are a receipt for the REQUEST, not evidence "
        f"that {suggestion_id!r} left the document's pending set. Re-read "
        "with list_document_suggestions before treating the card as resolved."
    )


def _unverified_verification(
    *,
    source: str,
    reason: Optional[str],
    action: str,
    suggestion_id: str,
    resolved_record: Optional[dict[str, Any]],
    why: str,
) -> dict[str, Any]:
    """The ``verification`` block for a resolution nothing checked.

    Both verify-less paths returned five keys, while the tool's ``Returns``
    documents ``read_source``, ``resolved_suggestion``, ``expected_text``,
    ``resulting_text``, ``matches_expectation``, ``pending_suggestion_count``
    and ``pending_suggestion_ids`` unconditionally. A client that reads
    ``verification["matches_expectation"]`` raised ``KeyError`` on exactly the
    two paths where the answer matters most, and one that used ``.get`` could
    not tell "unknown" from "this build does not report it".

    The keys are therefore present and NULL -- the same rule
    :func:`gdocs_preview.address.address_of` follows, for the same reason: a
    block whose shape varies makes an absent field and an unknown value the
    same observation. ``pending_suggestion_count`` is null rather than 0,
    because 0 is a claim about the document that no read here supports.
    ``resolved_suggestion`` is the one thing that IS known -- it comes from the
    ledger, not from a read -- so it is echoed rather than nulled.
    """
    return {
        "source": source,
        "reason": reason,
        "read_source": None,
        "still_pending": None,
        "still_pending_unavailable": "not_verified",
        "resolved_suggestion": (
            _echo_suggestion(resolved_record) if resolved_record else None
        ),
        "expected_text": None,
        "resulting_text": None,
        "matches_expectation": None,
        # NOT ``resulting_text_unavailable``: that field's vocabulary is
        # :data:`UNLOCATED_REASONS`, every member of which owns a sentence in
        # ``_UNLOCATED_NOTES``, and inventing a seventh value for it here
        # would be the same bug this function exists to fix. It is documented
        # as conditional, and the condition is a read that ran.
        "pending_suggestion_count": None,
        "pending_suggestion_ids": None,
        # The remainder the two counts do not cover is null for the same
        # reason they are: it is a subtraction against a read, and no read ran.
        "unreported_suggestion_count": None,
        "unreported_suggestions_unavailable": (
            review_page.UNREPORTED_UNAVAILABLE_NOT_VERIFIED
        ),
        "notes": [_unverified_note(action, suggestion_id, why)],
    }


def _unverified_suggest_verification(
    source: str, reason: Optional[str]
) -> dict[str, Any]:
    """``suggest_doc_edit``'s verification block when no read backed it.

    The twin of :func:`_unverified_verification`, which was written for the
    resolution path and left this one returning a bare ``{"source",
    "reason"}`` -- while ``suggest_doc_edit``'s Returns documents
    ``read_source``, ``created_suggestions`` and ``pending_suggestion_count``
    unconditionally. The ``unavailable`` half is reachable on the DEFAULT
    path (``verify=true``, post-write read fails), so a client reading
    ``verification["created_suggestions"]`` raised ``KeyError`` on a write
    that had landed.

    ``created_suggestions`` is ``null``, NOT ``[]``: the API's
    ``created_suggestion_ids`` sit in the response beside this block, and an
    empty echo list would read as "the write created nothing".
    """
    return {
        "source": source,
        "reason": reason,
        "read_source": None,
        "created_suggestions": None,
        "pending_suggestion_count": None,
        "unreported_suggestion_count": None,
        "unreported_suggestions_unavailable": (
            review_page.UNREPORTED_UNAVAILABLE_NOT_VERIFIED
        ),
        "notes": [
            f"nothing verified this edit: {reason}. created_suggestion_ids is "
            "the API's receipt for the REQUEST; no read confirmed the "
            "suggestion is in the document, and none of the echo "
            "(created_suggestions, the merge check, collateral removals) "
            "could be computed. Re-read with list_document_suggestions before "
            "treating the edit as landed -- and do NOT repeat it, since a "
            "second identical edit that did land leaves two suggestions."
        ],
    }


def _is_ours(record: dict[str, Any]) -> bool:
    """Did the AUTHENTICATED user write this suggestion?

    ``PostAuthor.me`` is the API's own answer and the only evidence there is;
    it is ``None`` on a read that degraded to the GA ``documents.get``, which
    carries no threads and therefore no authors. Unknown authorship is NOT
    ownership: a strict ``is True`` keeps the degraded read from
    manufacturing a claim it cannot support.
    """
    return ((record.get("author") or {}).get("me")) is True


def _concurrent_note(
    suggestion_ids: list[str],
    read: "_PostWriteRead",
    *,
    listing_was_complete: bool,
) -> str:
    """One sentence about suggestions that appeared but are not ours.

    "Appeared between the last listing and this write" is a claim about the
    LISTING, and it only holds if that listing could see where the card lives.
    A degraded listing carries one unnamed body and no tab ids, so every card
    in every other tab is "new" to the diff against it without anything having
    happened -- the same unfounded subtraction that produced fabricated
    collateral on the resolution path, running in the opposite direction.
    """
    names = ", ".join(repr(sid) for sid in suggestion_ids)
    #: ``None`` is unknown authorship, not "not mine" (see :func:`_is_ours`).
    me_flags = {
        (read.records[sid].get("author") or {}).get("me") for sid in suggestion_ids
    }
    if None in me_flags:
        why = (
            "this read carries no authors (read_source="
            f"{read.source!r}), so authorship could not be established"
        )
    elif me_flags == {True}:
        # Ours by author, but not shown to be ours by THIS call: the only
        # thing that put it in this set is a subtraction against a listing
        # that could not see every tab. Saying "a different author" here --
        # which is what this sentence used to say about everything in it --
        # would be a second false claim replacing the first.
        why = (
            "the thread names you as its author, but the API did not report "
            "this call creating it, and the listing it is 'new' against was "
            "degraded"
        )
    elif me_flags == {False}:
        why = "the suggestion thread names a different author"
    else:
        why = (
            "these threads name more than one author and the API reported "
            "this call creating none of them"
        )
    appeared = (
        f"{names} appeared between the last listing and this write"
        if listing_was_complete
        else (
            f"{names} is in the document now and was not in the last listing "
            "-- though that listing was a degraded read which could not see "
            "every tab, so it may have been there all along"
        )
    )
    return (
        f"{appeared}, and is "
        f"NOT reported as created by this call: {why}. A second reviewer "
        "editing the same document produces exactly this, and attributing "
        "their card to your write would have you resolve or reply to it as "
        "your own. Check the author before acting on it."
    )


def _ledger_records(
    user_google_email: str,
    document_id: str,
    before: Optional[suggestion_ledger.Snapshot],
) -> dict[str, Optional[dict[str, Any]]]:
    """The listed card behind every id in ``before`` -- its (tab, segment).

    An id with no record is kept as ``None`` rather than dropped:
    :meth:`_PostWriteRead.absences` treats "we do not know where it lives" as
    a reason it cannot attest the absence, which is exactly what it is.
    """
    if before is None:
        return {}
    return {
        sid: suggestion_ledger.record_of(user_google_email, document_id, sid)
        for sid in before.ids
    }


def _collateral_unavailable_note(
    action: str, suggestion_ids: list[str], read: "_PostWriteRead"
) -> str:
    """One sentence for removals this read had no standing to report.

    The claim being withheld is ``collateral_note``'s: "accepting X also
    removed it, because that removed the last character it marked. Its comment
    thread went with it." That is causation about a customer's document, and
    the only evidence for it is a before/after diff -- which says nothing at
    all when the "after" read cannot see the tab the id lives in.

    **Only reachable from an incomplete read**, which is what licenses the
    sentence below. :meth:`_PostWriteRead.absences` used to route a
    still-OPEN-but-unmodelled id here too, so "that read did not cover the
    whole document" was printed about a read whose ``complete`` was ``True``
    -- a false statement about the READ, made while explaining why a claim
    about the DOCUMENT was being withheld. Those ids now have their own
    sentence (:func:`_unmodelled_still_pending_note`).
    """
    names = ", ".join(repr(sid) for sid in suggestion_ids)
    return (
        f"{names} was listed before this {action} and is absent from the "
        f"post-write read, but that read did not cover the whole document "
        f"(read_source={read.source!r}, which carries no tab ids at all), so "
        "whether this write removed anything else is UNKNOWN. Nothing is "
        "reported as also-removed and nothing has been recorded against "
        "those ids: a read that cannot see a tab is not evidence about the "
        "suggestions in it. Re-read with list_document_suggestions to see "
        "which are still pending."
    )


def _unmodelled_still_pending_note(
    action: str, suggestion_ids: list[str], read: "_PostWriteRead"
) -> str:
    """One sentence for an id that dropped out of the diff without going away.

    The other half of :func:`_collateral_unavailable_note`'s old job, and the
    opposite fact. This read DID cover the document; the id is simply no longer
    something the analysis layer describes, while the API goes on listing its
    thread as OPEN. Reporting it as removed would be false, and reporting it
    as "we could not look" would be false about the read -- so it is reported
    as what it is: still pending, still resolvable by id, invisible to
    everything in this response that is derived from content marks.
    """
    names = ", ".join(repr(sid) for sid in suggestion_ids)
    return (
        f"{names} was listed before this {action} and is no longer described "
        f"by this tool's analysis layer, but the API still lists it as pending "
        f"in its own suggestion-thread array (read_source={read.source!r}, "
        "which covered the whole document), so it was NOT removed by this "
        "write and nothing is reported as also-removed for it. The analysis "
        "layer reads a document's CONTENT MARKS and some suggestion kinds "
        "leave none -- paragraph style, bullets, table row/cell style "
        "(docs/findings/coverage.md) -- so such a card is absent from "
        "pending_suggestion_ids while being fully pending. It is listed in "
        "unreported_suggestions and manage_document_suggestion still accepts "
        "or rejects it by id."
    )


def _overlaps(record: dict[str, Any], edit_range: tuple[int, int], scope: dict) -> bool:
    """Does a suggestion's index range touch the range an edit targeted?

    Bounds are inclusive on both sides so an insertion (a zero-width edit)
    at the seam of a suggestion still counts -- that is precisely the case
    where the API merges instead of creating a new suggestion.

    ``scope`` is a resolved ``(tab, segment)`` from
    :func:`gdocs_preview.address.resolve_range_scope`, and membership is
    :func:`gdocs_preview.address.in_range_scope` -- the SAME pair the listing's
    range filter uses. It used to be a hand-rolled comparison here that
    checked ``segment_id`` always but ``tab_id`` only when the caller had
    named one, so on a multi-tab document the default ``tab_id=None`` echoed
    overlapping suggestions from EVERY tab and asserted "your edit merged
    into it" about a suggestion in a tab the edit never touched. The read
    path refuses that guess; making the write path share the resolver is
    what stops the two answering the same question differently again.
    """
    start, end = record.get("start_index"), record.get("end_index")
    if start is None or end is None:
        return False
    if not in_range_scope(record, scope):
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
    user_google_email: str,
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
                # Carries this document's id, via HttpError's URI. Filed under
                # the caller who produced it and read back by nobody else.
                "message": message[:500],
                "surface": "write",
            },
            source="tool_call",
            user_google_email=user_google_email,
        )
        if (availability, reason) == ("unavailable", "not_enrolled"):
            # The verdict just recorded for THIS account is ``unavailable``.
            # Name the other authenticated accounts as candidates -- and say
            # that nothing was attempted under them. Empty string unless there
            # really is another account, so single-account output is unchanged.
            # State only what was observed. Saying "enrollment for the
            # authenticated project" resolved by assertion a question that is
            # genuinely open -- whether Developer Preview enrollment follows the
            # Cloud project or the account is undocumented for the
            # two-accounts-one-OAuth-client case -- and it contradicted the
            # candidate hint appended one line below, which says so.
            raise UserInputError(
                f"{tool_name} needs Google Workspace Developer Preview access, and "
                f"the preview request was rejected as not enrolled for "
                f"{user_google_email}. See docs/preview-api-reference.md for how to "
                f"enroll and for what is still unknown about the scope of an "
                f"enrollment. Verify with check_docs_review_capabilities(probe=true)."
                + candidate_account_hint(user_google_email, HINT_PREVIEW_UNAVAILABLE)
            ) from error
        raise
    preview_status.record(
        "available",
        {
            "http_status": 200,
            "reason": "preview_request_succeeded",
            # A batchUpdate really did go through: evidence about the WRITE
            # surface, which a successful read does not entail.
            "surface": "write",
        },
        source="tool_call",
        user_google_email=user_google_email,
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
    # "no longer exists" asserts the id once DID -- a removal. All the API
    # proved is that it does not resolve now, which a typo satisfies just as
    # well, and the ledger sentence that follows may itself be saying "most
    # likely the id is wrong". The two sentences contradicted each other in
    # the same string. "does not exist" is what was observed; the ledger says
    # whether it ever did.
    return UserInputError(
        f"{tool_name}: suggestion {suggestion_id!r} does not exist in "
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
        start_index (int): UTF-16 start index of the edit. Must be >= 1 in
            the body (index 0 is the section break) and >= 0 in a
            header/footer/footnote segment, which is numbered from its own
            start.
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
            context_after, segment, segment_id, tab_id, start_index,
            end_index, summary_text, status], pending_suggestion_count,
            unreported_suggestion_count, and -- only when they apply --
            suggestions_at_edit_range (when the
            API reported no new id, which happens when the edit merged into
            an existing same-author suggestion) with the range_scope it was
            read in, appeared_since_last_read, also_removed_suggestion_ids,
            also_removed_suggestion_ids_unavailable,
            still_pending_unmodelled_suggestion_ids, unreported_suggestions,
            unreported_suggestions_unavailable, notice_unreported,
            suggestions_at_edit_range_unavailable (a multi-tab document and no
            tab_id: the range cannot be resolved to one coordinate space),
            pending_suggestions_are_partial (the post-write read degraded, so
            pending_suggestion_count is what it SAW), reason (on the two
            unverified sources, naming what stopped the check), and notes}.

            pending_suggestion_count counts the suggestions this tool MODELS
            -- it reads a document's content marks, and paragraph-style,
            bullet and table row/cell-style suggestions leave none.
            unreported_suggestion_count is the rest of what the API itself
            lists as pending, named in unreported_suggestions (id, Google's
            own summary_text label, author, status) with notice_unreported
            explaining them; a review is finished only when BOTH are zero. On
            a degraded read it is null with unreported_suggestions_unavailable
            = read_degraded, and with no read at all, not_verified.

            On verify=false, and when the post-write read fails, every key
            above is present and NULL rather than absent, so an unknown answer
            and a missing field never look alike. created_suggestions is null
            rather than [] there: created_suggestion_ids sits beside this
            block, and an empty echo would read as "the write created
            nothing".

            created_suggestions claims AUTHORSHIP, so it holds only what the
            API reported as created plus what the suggestion thread
            attributes to you. A card that merely appeared between the last
            listing and this write -- which is what a second reviewer
            editing the same document looks like -- is reported under
            appeared_since_last_read instead, with a note. Check the author
            before resolving or replying to one of those.

            Every echoed suggestion carries segment/segment_id/tab_id
            alongside its indexes, because a Docs index is only unique
            within one (tabId, segmentId): pass all of them back, not just
            the numbers.
    """
    # The body's first insertable position is 1 (index 0 is the section
    # break). A header/footer/footnote segment is numbered from its own start,
    # so 0 IS a position there -- verified against the live API 2026-07-31 by
    # inserting at {"index": 0, "segmentId": <header>}. Refusing it made the
    # first character of every such segment unwritable.
    floor = 0 if segment_id else 1
    if start_index < floor:
        raise UserInputError(
            f"start_index must be >= {floor}"
            + (
                " in a header/footer/footnote segment (segments are numbered "
                "from their own start)."
                if segment_id
                else " in the document body (index 0 is the section break). "
                "Pass segment_id to write into a header, footer or footnote, "
                "where 0 is a valid position."
            )
            + " Take indexes verbatim from list_document_suggestions or "
            "get_doc_review_view output, together with the segment_id and "
            "tab_id they came with."
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
        # modify_doc_text's replacement path.
        #
        # VERIFIED 2026-08-01 (docs/findings/suggest-semantics.md,
        # e2e/test_suggest_semantics.py). A SUGGEST batch resolves each
        # request's indexes PROGRESSIVELY -- against the document as the
        # earlier requests in the same batch left it, exactly like EDIT mode,
        # not against the pre-batch document as this comment used to claim of
        # EDIT. What makes the shape below correct is the other half of the
        # finding: the space it progresses in is SUGGESTIONS_INLINE, and a
        # SUGGESTED deletion leaves its characters there (marked), so
        # request 0 shifts nothing and request 1's start_index still means
        # start_index. Over "0123456789", [delete(1,5), insert@1 "X"] gives
        # inline "X0123456789" and accepted "X456789".
        #
        # The corollary is the reason no third request may be appended here
        # without re-deriving its index: a suggested INSERTION *is* in the
        # inline space immediately and does shift everything after it.
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
        f"start={start_index}, end={end_index}, "
        f"segment={segment_id or 'body'}, tab={tab_id or 'default'}"
    )
    before = suggestion_ledger.snapshot(user_google_email, document_id)
    response = await _execute_preview_batch_update(
        service,
        "suggest_doc_edit",
        document_id,
        requests,
        write_mode="SUGGEST",
        user_google_email=user_google_email,
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
            before=before,
            edit_range=(
                start_index,
                end_index if end_index is not None else start_index,
            ),
            segment_id=segment_id,
            tab_id=tab_id,
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
    before: Optional[suggestion_ledger.Snapshot],
    edit_range: tuple[int, int],
    verify: bool,
    segment_id: Optional[str] = None,
    tab_id: Optional[str] = None,
) -> dict[str, Any]:
    """Post-write echo for :func:`suggest_doc_edit`.

    ``createdSuggestionIds`` is the primary join key, but it is not
    trustworthy on its own, so three sources are used in order, and each is
    reported under the strength of claim it can actually support:

    1. the reported created ids, intersected with what the read actually
       found (an id the response named may already be retired) -- authorship
       proven by the API;
    2. anything new since the last read -- the diff answers even when the
       response says nothing, but "new since a snapshot" is not "made by
       this call". Only the ones the thread attributes to the authenticated
       user (:func:`_is_ours`) join ``created_suggestions``; the rest go to
       ``appeared_since_last_read`` with a note;
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

    **``notes`` accumulates; it is never assigned.** Four independent things
    have something to say about one write here -- somebody else's card
    appeared, the tab is ambiguous, the edit merged into an existing
    suggestion, another suggestion was garbage-collected -- and more than one
    of them can be true at once. Two used to do
    ``verification["notes"] = [...]``, which deleted the concurrent-
    authorship note ("is NOT reported as created by this call ... Check the
    author before acting on it") on exactly the merge path this module exists
    to handle: a second reviewer's card, echoed under
    ``appeared_since_last_read`` with nothing left to say why. Use
    ``setdefault("notes", []).append`` / ``.extend``.

    **Both diffs against ``before`` are bounded by what the two reads saw.**
    ``before`` is a :class:`~gdocs_preview.suggestion_ledger.Snapshot`, not a
    bare id set, because each direction of the subtraction is unfounded when
    the read on that side of it could not see the tab in question: "new since
    the listing" is not new if the listing was blind (the note says so), and
    "gone since the listing" is not gone if THIS read is blind (the ids are
    withheld from ``also_removed_suggestion_ids`` and from the ledger).
    """
    if not verify:
        return _unverified_suggest_verification("skipped", "verify=false")
    read, failure = await _post_write_read(
        service, document_id, user_google_email=user_google_email
    )
    if read is None:
        return _unverified_suggest_verification("unavailable", failure)

    # (1) What the API itself said it created. Authorship is proven.
    echoed_ids = [sid for sid in created_ids if sid in read.records]
    # (2) What is new since the last read. This is a DIFF against a snapshot
    # that may be arbitrarily old -- the ledger holds whatever the last
    # listing saw -- so "new since then" is not "made by this call". A second
    # reviewer working in the document at the same time has their card appear
    # in exactly this set, and it used to be echoed under
    # ``created_suggestions``: this call claiming authorship of somebody
    # else's suggestion, which the agent may then reply to or resolve as its
    # own. The thread says who wrote it, so ask.
    appeared: list[str] = []
    if before is not None:
        for sid in read.records:
            if sid not in before.ids and sid not in echoed_ids:
                appeared.append(sid)
    # Promoting an appeared card to "created by this call" rests on the SAME
    # premise as the note below: that the last listing could see where the
    # card lives. It did not check. A degraded listing carries one unnamed
    # body and no tab ids, so every pre-existing card of OURS in every other
    # tab is "new" to the subtraction without anything having happened -- and
    # being ours, it went straight into ``created_suggestions``, whose
    # docstring says this call made it. The API's own ``createdSuggestionIds``
    # is unaffected: that is proof, and it stays in ``echoed_ids`` either way.
    # This is the mirror of the guard 20 lines down, which the other branch
    # already applies.
    listing_was_complete = bool(before and before.complete)
    others: list[str] = []
    for sid in appeared:
        if _is_ours(read.records[sid]) and listing_was_complete:
            echoed_ids.append(sid)
        else:
            others.append(sid)

    verification: dict[str, Any] = {
        "source": "post_write_read",
        "read_source": read.source,
        "created_suggestions": [_echo_suggestion(read.records[s]) for s in echoed_ids],
        "pending_suggestion_count": len(read.records),
    }
    if not read.complete:
        verification["pending_suggestions_are_partial"] = True
        verification.setdefault("notes", []).append(_partial_pending_note(read))
    if others:
        verification["appeared_since_last_read"] = [
            _echo_suggestion(read.records[s]) for s in others
        ]
        verification.setdefault("notes", []).append(
            _concurrent_note(
                others,
                read,
                # ``others`` came out of a subtraction against ``before``, so
                # the sentence describing it may only claim what THAT read
                # could see.
                listing_was_complete=listing_was_complete,
            )
        )
    if not echoed_ids:
        try:
            scope = resolve_range_scope(
                list(read.records.values()),
                tab_ids=read.tab_ids,
                segment_id=segment_id,
                tab_id=tab_id,
            )
        except ValueError as error:
            # Multi-tab document, no tab_id given: the listing REFUSES this
            # and so must the echo. Naming a suggestion from some other tab
            # as "the one your edit merged into" is a wrong id the agent may
            # go on to reply to or accept.
            verification["suggestions_at_edit_range_unavailable"] = str(error)
            verification.setdefault("notes", []).append(
                "this edit named no tab_id and the document has more than "
                "one tab, so the suggestion(s) at the edited range cannot be "
                "identified: an index means a different place in each tab. "
                "Re-run with the tab_id the record carries, or read the "
                "range back with list_document_suggestions(tab_id=...)."
            )
            scope = None
        if scope is not None:
            overlapping = [
                _echo_suggestion(record)
                for record in read.records.values()
                if _overlaps(record, edit_range, scope)
            ]
            if overlapping:
                verification["suggestions_at_edit_range"] = overlapping
                verification["range_scope"] = scope
                verification.setdefault("notes", []).append(
                    "the API reported no new suggestion id for this edit; the "
                    "suggestion(s) now covering the edited range are echoed "
                    "instead -- editing inside an existing same-author "
                    "suggestion merges into it rather than creating a new one."
                )
    # The absence half of the diff, filtered by what THIS read observed: an
    # id that dropped out of the subtraction because the read cannot see its
    # tab has not vanished, it was never looked for -- and one the API still
    # lists as pending has not vanished either, it just stopped being
    # something this layer can describe.
    vanished, still_pending_unmodelled, unattested = read.absences(
        (before.ids - read.live_ids) if before is not None else (),
        _ledger_records(user_google_email, document_id, before),
    )
    if vanished:
        resolutions = suggestion_ledger.record_resolution(
            user_google_email,
            document_id,
            "suggest_doc_edit",
            # No id was RESOLVED here. The id below is the one this call
            # CREATED, and passing it as the resolved id filed a "how it went
            # away" record for a card that had just arrived -- so a later
            # lookup of it answered "You suggest_doc_edited it yourself;
            # resolving a suggestion removes it". It is the merge's ``cause``,
            # which names the absorber without claiming it went anywhere.
            "",
            vanished,
            # The edit itself landed: the batchUpdate returned and this read
            # is the one that saw its result. What is uncertain about a merge
            # is which id absorbed which, and that is carried by ``cause``.
            landed=True,
            cause=echoed_ids[0] if echoed_ids else None,
        )
        verification["also_removed_suggestion_ids"] = vanished
        verification.setdefault("notes", []).extend(
            suggestion_ledger.collateral_note(r) for r in resolutions if not r.direct
        )
    if still_pending_unmodelled:
        verification["still_pending_unmodelled_suggestion_ids"] = (
            still_pending_unmodelled
        )
        verification.setdefault("notes", []).append(
            _unmodelled_still_pending_note(
                "suggest_doc_edit", still_pending_unmodelled, read
            )
        )
    if unattested:
        verification["also_removed_suggestion_ids_unavailable"] = "read_incomplete"
        verification.setdefault("notes", []).append(
            _collateral_unavailable_note("suggest_doc_edit", unattested, read)
        )
    _attach_unreported_pending(verification, read)
    suggestion_ledger.observe(
        user_google_email, document_id, read.records.values(), complete=read.complete
    )
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
            {source, read_source, still_pending, resolved_suggestion
            (including its segment/segment_id/tab_id, so its indexes are a
            complete address), expected_text, resulting_text,
            matches_expectation, pending_suggestion_count,
            pending_suggestion_ids, unreported_suggestion_count, and -- only
            when they apply -- still_pending_unavailable,
            resulting_text_unavailable, also_removed_suggestion_ids,
            also_removed_suggestion_ids_unavailable,
            still_pending_unmodelled_suggestion_ids, unreported_suggestions,
            unreported_suggestions_unavailable, notice_unreported,
            pending_suggestions_are_partial (the post-write read degraded, so
            the two pending_* fields are what it SAW, not what the document
            holds), reason (on the two unverified sources, naming what
            stopped the check) and notes}.

            pending_suggestion_count and pending_suggestion_ids are the
            suggestions this tool MODELS -- it reads a document's content
            marks, and paragraph-style, bullet and table row/cell-style
            suggestions leave none. unreported_suggestion_count is the rest of
            what the API itself lists as pending, named in
            unreported_suggestions (id, Google's own summary_text label,
            author, status) with notice_unreported explaining them. The
            document's pending set is the two together, so an id reported as
            still_pending is always in one list or the other, and a review is
            finished only when BOTH numbers are zero. On a degraded read the
            thread array is absent, so unreported_suggestion_count is null
            with unreported_suggestions_unavailable = read_degraded; with no
            read at all it is null with not_verified.

            still_pending_unmodelled_suggestion_ids names ids that were in the
            last listing, are no longer described by this tool, and are STILL
            pending per the API. They were not removed by this write and are
            deliberately absent from also_removed_suggestion_ids.

            resulting_text is read in the resolved suggestion's OWN (tab,
            segment), never the document body, so matches_expectation is a
            statement about the place the write happened. It is compared at
            full width; only the echoed copy is trimmed.

            still_pending is the structural evidence about the write: an
            accepted or rejected suggestion is gone from the pending set, so
            an id still in it is a resolution that did NOT take effect.
            matches_expectation is derived from that first and the text
            second, and is therefore never true while still_pending is true,
            whatever the text reads -- rejecting anything leaves the base
            text exactly as it was, so on a reject the text alone cannot tell
            a write that landed from one that did not.

            still_pending is null when nothing established it, and
            still_pending_unavailable then says which of three reasons:
            segment_not_in_read (the read lost the card's tab/segment -- the
            GA fallback carries no tab ids at all), read_incomplete (that read
            did not cover the document and this session never listed the id,
            so there is nothing saying which tab to look in), or not_verified
            (no read happened at all -- verify=false, or the post-write read
            itself failed). On not_verified the whole verification block is
            still present with its documented keys null, so an unknown answer
            and a missing field never look alike; resolved_suggestion is the
            exception and is echoed, since it comes from this session's own
            listing rather than from a read. Absence from a read that could not look there is NOT
            evidence the resolution landed, so matches_expectation is null too
            and nothing in the response reports on the write: re-read with
            list_document_suggestions rather than repeating the resolution.
            The same coverage rule governs also_removed_suggestion_ids -- ids
            whose disappearance that read cannot attest are withheld from it,
            and also_removed_suggestion_ids_unavailable is set instead.

            matches_expectation is null ONLY when no check could be run, and
            resulting_text_unavailable then names which of the six reasons
            it was: suggestion_not_listed (this session never listed the id,
            so there is no before/after to compare), ambiguous_tab (the card
            carried no tab_id and the document has several),
            segment_not_in_read (the post-write read lost the tab/segment the
            card was listed in -- a degraded GA read carries no tab ids),
            anchor_not_found (the base text before the range is gone, which
            means a concurrent edit), ambiguous_anchor (that base text
            repeats, so several places read as the range and they disagree),
            nothing_to_compare (the card was listed without the before/after
            text a resolution is checked against). None of them says the
            write failed; still_pending is the evidence about that, unless it
            is itself null.
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
    before = suggestion_ledger.snapshot(user_google_email, document_id)
    resolved_record = suggestion_ledger.record_of(
        user_google_email, document_id, suggestion_id
    )
    try:
        response = await _execute_preview_batch_update(
            service,
            "manage_document_suggestion",
            document_id,
            requests,
            user_google_email=user_google_email,
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
        before=before,
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
    before: Optional[suggestion_ledger.Snapshot],
    verify: bool,
) -> dict[str, Any]:
    """Post-write echo for :func:`manage_document_suggestion`.

    Three questions, answered from one read: is the target gone, does the
    document now read the way the suggestion promised, and what else
    disappeared. The first is STRUCTURAL and is the evidence about the write
    itself -- ``still_pending``, i.e. is the id absent from the post-write
    pending set, which it must be after any accept or reject.

    The second is :func:`gdocs_preview.analysis.check_resolution`, run over
    the post-write read's BASE text: the suggestion's own ``(tab, segment)``
    must read ``expected_text`` (``post_text`` for an accept, ``pre_text``
    for a reject) at the range located by its anchor, with the untouched
    ``context_after`` behind it. Every value on both sides of that comparison
    is produced by the same projection layer that produced the card, so there
    is one representation and no substring search -- see
    :class:`~gdocs_preview.analysis.BaseText`.

    **The two are not independent answers to be printed side by side.**
    ``matches_expectation`` is DERIVED from both, in
    :meth:`_ResolutionVerdict.derive`, because for a whole class of
    suggestions the text cannot tell the two worlds apart and the pending
    set always can: rejecting anything, and resolving a style-only
    suggestion, leaves the base text exactly as it was. Four consecutive
    review rounds found this verification reporting success on a write it had
    not verified -- the read-path addresses, the echo clip, the marked-vs-base
    representation, and then the reject half -- and each fix retired one
    branch of the same shape. Deriving the verdict from the evidence, rather
    than assembling the two and hoping they agree, is what retires the shape.

    ``expected_text`` is ``None`` when the caller resolved an id it never
    listed -- there is no promise to check the document against.

    Only the copies that go into the response are clipped (:func:`_clip`);
    the check never sees them.

    A ``matches_expectation`` of ``None`` always carries
    ``resulting_text_unavailable`` naming which of
    :data:`UNLOCATED_REASONS` prevented the text comparison, plus a note
    saying so in words. Silence made several different situations -- one of
    them entirely benign -- arrive at the agent as the same ``null``.

    **A read only answers about what it saw.** ``still_pending`` was
    ``suggestion_id in read.records`` -- a subtraction that reads ``False``
    for a card in a tab the read structurally cannot see, which is every card
    outside the body once the read degrades to the GA ``documents.get``. It is
    now :meth:`_PostWriteRead.pending_state`, which answers ``None`` there,
    and the same rule filters the collateral diff: an id whose absence this
    read cannot attest is neither reported as removed nor recorded as removed.

    The two ``verify``-less returns say so too. ``{"source": "skipped"}`` and
    ``{"source": "unavailable"}`` are byte-for-byte the shape prod returns for
    a resolution that resolved NOTHING -- an HTTP 200 no-op sits beside a
    populated ``rejected_suggestion_ids`` and ``comment_update_state:
    ALL_SAVED`` -- so both carry the warning that the response ids alone do
    not say the write landed.
    """
    if not verify:
        # Still remember the resolution: a later "does not exist" for this id
        # must be explainable even when the caller opted out of the read.
        # ``landed=None``, because that is exactly what opting out bought --
        # the request was accepted, nothing observed its effect.
        suggestion_ledger.record_resolution(
            user_google_email, document_id, action, suggestion_id, landed=None
        )
        return _unverified_verification(
            source="skipped",
            reason="verify=false",
            action=action,
            suggestion_id=suggestion_id,
            resolved_record=resolved_record,
            why="verify=false",
        )

    read, failure = await _post_write_read(
        service, document_id, user_google_email=user_google_email
    )
    if read is None:
        # Same as verify=false: the write was accepted and nothing looked.
        suggestion_ledger.record_resolution(
            user_google_email, document_id, action, suggestion_id, landed=None
        )
        return _unverified_verification(
            source="unavailable",
            reason=failure,
            action=action,
            suggestion_id=suggestion_id,
            resolved_record=resolved_record,
            why=f"the post-write read failed ({failure})",
        )

    expected_text: Optional[str] = None
    resulting_text: Optional[str] = None
    matches: Optional[bool] = None
    unlocated: Optional[str] = None if resolved_record else "suggestion_not_listed"
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
        # Scoped to the resolved suggestion's OWN (tab, segment): its indexes
        # and its context window are numbered there and nowhere else. Base
        # text, at full width -- the check below is the whole point of the
        # read, and it is typed so that rendered text cannot reach it.
        base_text, unlocated = read.text_at(resolved_record)
        if base_text is not None:
            if expected_text is None and removed_text is None:
                # A record with no before/after at all: nothing was promised,
                # so nothing can be checked. Reported rather than returned as
                # a bare null verdict -- the docstring's guarantee is that
                # matches_expectation: null ALWAYS names its reason.
                unlocated = "nothing_to_compare"
            else:
                check = check_resolution(
                    base_text,
                    context_before=resolved_record.get("context_before") or "",
                    context_after=resolved_record.get("context_after") or "",
                    expected_text=expected_text or "",
                    removed_text=removed_text or "",
                )
                matches = check.matches
                resulting_text = check.window
                unlocated = check.reason
    # Is the id still pending -- and CAN this read say? A read that never
    # walked the card's (tab, segment) has not observed its absence, it has
    # only failed to observe its presence.
    still_pending, pending_unknown = read.pending_state(suggestion_id, resolved_record)
    # The two pieces of evidence, added up in ONE place. `matches` from here
    # on is the derived verdict, not the text check: see _ResolutionVerdict.
    try:
        verdict = _ResolutionVerdict.derive(
            still_pending=still_pending, text_check=matches
        )
    except ValueError as error:  # pragma: no cover - derive cannot violate it
        # The invariant is real and the raise is how it is enforced against
        # direct construction -- but not HERE. This runs after a landed
        # destructive write, and an exception on this path turns an accept
        # that applied into a tool error the agent will try to "fix" by
        # writing again, which is precisely what _post_write_read refuses to
        # do. Degrade to the verdict that claims nothing, and log.
        logger.error(
            f"[docs_preview] resolution verdict for {suggestion_id!r} could "
            f"not be derived ({error}); reporting an unknown verdict rather "
            "than failing a write that already landed"
        )
        verdict = _ResolutionVerdict.unknown()
        still_pending, pending_unknown = None, "read_incomplete"
    if (
        verdict.matches_expectation is None and unlocated is None
    ):  # pragma: no cover - belt and braces
        # The docstring promises a null verdict always names its reason, and
        # a promise a caller reads is worth more than one the code merely
        # happens to keep: a future branch that forgets is caught here.
        unlocated = "nothing_to_compare"

    verification: dict[str, Any] = {
        "source": "post_write_read",
        "read_source": read.source,
        "still_pending": verdict.still_pending,
        "resolved_suggestion": (
            _echo_suggestion(resolved_record) if resolved_record else None
        ),
        # Clipped for the context window; ``matches_expectation`` above was
        # decided on the full strings, so a truncated echo never changes a
        # verdict -- it only shortens the receipt.
        "expected_text": _clip(expected_text),
        "resulting_text": _clip(resulting_text),
        "matches_expectation": verdict.matches_expectation,
        "pending_suggestion_count": len(read.records),
        "pending_suggestion_ids": sorted(read.records),
    }
    if not read.complete:
        verification["pending_suggestions_are_partial"] = True
        verification.setdefault("notes", []).append(_partial_pending_note(read))
    if verdict.still_pending is True:
        # First note, because it is the one that changed the verdict. The
        # unlocated note below explains a missing resulting_text; this one
        # explains a false matches_expectation, and they can both apply.
        verification.setdefault("notes", []).append(
            _still_pending_note(action, suggestion_id, verdict.text_check)
        )
    elif verdict.still_pending is None:
        # The pending set was not observed where this card lives. Named and
        # explained rather than left as a bare null, for the same reason
        # ``resulting_text_unavailable`` exists: "the read could not look
        # there" and "the read looked and it was gone" are opposite facts.
        verification["still_pending_unavailable"] = pending_unknown
        verification.setdefault("notes", []).append(
            _pending_unknown_note(
                pending_unknown or "read_incomplete",
                action=action,
                suggestion_id=suggestion_id,
                record=resolved_record,
                read=read,
            )
        )
    if unlocated is not None:
        # Its sibling _verify_suggest names its one ambiguity
        # (``suggestions_at_edit_range_unavailable``); a null verdict here
        # said nothing at all, so "the read could not see that tab", "the
        # anchor is gone" and "we never listed this id" arrived identical.
        verification["resulting_text_unavailable"] = unlocated
        # setdefault(...).append, never ``notes = [...]``: this block and the
        # collateral block below both have something to say about the same
        # write, and an assignment silently deletes whichever ran first --
        # see the note in :func:`_verify_suggest`.
        verification.setdefault("notes", []).append(
            _unlocated_note(
                unlocated,
                suggestion_id=suggestion_id,
                record=resolved_record,
                read=read,
                # Every unlocated sentence ends by pointing at ``still_pending``
                # as the evidence about the write. When that is null there is
                # nothing to point at, and the note has to say so instead.
                still_pending=verdict.still_pending,
            )
        )

    # The collateral claim -- "accepting X also removed it, because that
    # removed the last character it marked" -- is causation about a customer's
    # document, and its only evidence is this before/after diff. Ids the read
    # could not look for are withheld from BOTH the response and the ledger:
    # record_resolution pops what it is given, so a fabricated removal is
    # repeated by explain_missing for the rest of the session.
    collateral, still_pending_unmodelled, unattested = read.absences(
        ((before.ids - read.live_ids) - {suggestion_id}) if before is not None else (),
        _ledger_records(user_google_email, document_id, before),
    )
    # The ledger is told the SAME verdict the response carries. ``still_pending``
    # is the derived one (:class:`_ResolutionVerdict`), so "landed" here cannot
    # disagree with what this call reported to the agent: True only when the
    # read confirmed the id left the pending set, False when it was still
    # there, None when this read could not say. Filing an unlanded resolution
    # as a proven one made every later "does not exist" for the id answer "You
    # accepted it yourself" -- causation asserted from the one piece of
    # evidence that contradicted it.
    resolutions = suggestion_ledger.record_resolution(
        user_google_email,
        document_id,
        action,
        suggestion_id,
        collateral,
        landed=(None if verdict.still_pending is None else not verdict.still_pending),
    )
    if collateral:
        verification["also_removed_suggestion_ids"] = collateral
        verification.setdefault("notes", []).extend(
            suggestion_ledger.collateral_note(r) for r in resolutions if not r.direct
        )
    if still_pending_unmodelled:
        verification["still_pending_unmodelled_suggestion_ids"] = (
            still_pending_unmodelled
        )
        verification.setdefault("notes", []).append(
            _unmodelled_still_pending_note(action, still_pending_unmodelled, read)
        )
    if unattested:
        verification["also_removed_suggestion_ids_unavailable"] = "read_incomplete"
        verification.setdefault("notes", []).append(
            _collateral_unavailable_note(action, unattested, read)
        )
    _attach_unreported_pending(verification, read)
    suggestion_ledger.observe(
        user_google_email, document_id, read.records.values(), complete=read.complete
    )
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
            matches_request, and -- only when saved is null --
            saved_unavailable and notes}.

            saved is the API's own commentUpdateState and nothing else:
            true when it said ALL_SAVED, false when it reported a failure
            state, and NULL when it sent no state at all. Null is not false.
            The stored post echoed beside it (post_id, stored_content,
            matches_request) is evidence about the CONTENT the API received,
            not about persistence -- so on a null, read the thread back rather
            than replying again, since a needless retry leaves a duplicate
            reply in the document.
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
            user_google_email=user_google_email,
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
    post_id = post.get("postId")
    verdict = _ThreadWriteVerdict.derive(comment_update_state)
    verification: dict[str, Any] = {
        # No extra read: the batchUpdate response already carries the
        # stored Post, so the echo costs nothing.
        "source": "batch_update_response",
        "saved": verdict.saved,
        "stored_content": _clip(stored_content),
        "matches_request": (
            stored_content == reply_content if stored_content is not None else None
        ),
    }
    if verdict.saved is None:
        verification["saved_unavailable"] = verdict.unavailable_reason
        verification.setdefault("notes", []).append(
            verdict.note(what="reply", post_id=post_id)
        )
    result = {
        "document_id": document_id,
        "thread_type": thread_type,
        thread_id_key: thread_id,
        "post_id": post_id,
        "author": normalize_author(post.get("author")),
        "content": _clip(stored_content),
        "create_time": post.get("createTime"),
        "comment_update_state": comment_update_state,
        "verification": verification,
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

    **An index is only half of an address.** Docs numbers each
    ``(tabId, segmentId)`` pair from its own start, so index 412 in a
    footnote and index 412 in the body are different places, and this tool
    defaults to ``segment_id=None``/``tab_id=None``, which means the body of
    the default tab. Every record from ``list_document_suggestions`` and
    every paragraph, header, footer and footnote from
    ``get_doc_review_view`` carries ``segment``, ``segment_id`` and
    ``tab_id`` alongside its indexes: pass them back here unchanged. Taking
    a header's or footnote's index without its ``segment_id`` anchors the
    comment in the body at that number, silently.

    ``segment_id`` and ``tab_id`` are a PAIR, not two independent options: a
    segment id is resolved WITHIN the tab the request names, so a segment id
    sent without the tab it came from is a 400 against a multi-tab document
    ("Segment with ID kix.… was not found", measured against the live API
    2026-07-31), with nothing in the response saying which tab would have
    worked. Send both or neither.

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
        start_index (int): UTF-16 start index of the anchored range, in the
            (tab_id, segment_id) coordinate space named below. Must be >= 1
            in the body (index 0 is the section break) and >= 0 in a
            header/footer/footnote segment, which is numbered from its own
            start.
        end_index (int): UTF-16 end index (exclusive). Must be greater
            than start_index.
        segment_id (str): Header/footer/footnote segment ID; omitted means
            the document body. Take it from the same record the indexes
            came from, and send tab_id WITH it -- a segment id is resolved
            inside one tab, so the pair is mandatory in a multi-tab
            document.
        tab_id (str): Document tab ID. Omitted means the default tab, which
            is only safe when the indexes came from that tab; always send it
            alongside a segment_id.
        assignee_email (str): Optional email address to assign the
            comment to.

    Returns:
        str: JSON with document_id, comment_id, post_id, author (the
            thread head post's PostAuthor as the API recorded it), content
            (as stored), create_time, anchor_id, quoted_text, status,
            comment_update_state, link, and verification {source:
            "batch_update_response", saved, requested_range, anchored_text,
            stored_content, matches_request, and -- only when saved is null --
            saved_unavailable and notes}.

            requested_range is the range this call ASKED for, echoed back; it
            is not evidence about where the comment landed, because no read is
            made and the API does not echo a resolved range. anchored_text
            (the thread's plainTextQuote) IS that evidence: compare it with
            the text you meant to comment on.

            saved is the API's own commentUpdateState and nothing else: true
            when it said ALL_SAVED, false when it reported a failure state,
            and NULL when it sent no state at all. Null is not false -- read
            the comments back rather than creating the comment again, since a
            needless retry leaves a duplicate in the document.
    """
    if not content or not content.strip():
        raise UserInputError("content must be non-empty.")
    # 0 is a real position in a header/footer/footnote; see suggest_doc_edit.
    floor = 0 if segment_id else 1
    if start_index < floor:
        raise UserInputError(
            f"start_index must be >= {floor}"
            + (
                " in a header/footer/footnote segment."
                if segment_id
                else " in the document body (index 0 is the section break)."
            )
        )
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
        f"range=[{start_index}, {end_index}), "
        f"segment={segment_id or 'body'}, tab={tab_id or 'default'}"
    )
    requests = [{"insertComment": insert_comment}]
    response = await _execute_preview_batch_update(
        service,
        "create_anchored_doc_comment",
        document_id,
        requests,
        enforce_comment_update=True,
        user_google_email=user_google_email,
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
    post_id = head_post.get("postId")
    verdict = _ThreadWriteVerdict.derive(comment_update_state)
    verification: dict[str, Any] = {
        # No extra read: InsertCommentResponse carries the CommentThread,
        # plainTextQuote included -- the anchored text for free.
        "source": "batch_update_response",
        "saved": verdict.saved,
        # The range this call ASKED for, echoed back under a name that says
        # so. It was ``anchored_range``, which reads as the range the comment
        # ended up on -- a claim nothing here supports: this tool makes no
        # read, and the API does not echo the resolved range. The one piece of
        # evidence about where the comment actually landed is
        # ``anchored_text`` (the thread's plainTextQuote), so compare THAT
        # with the text you meant to comment on; an off-by-one shows up there
        # and nowhere in these numbers.
        #
        # Built from ADDRESS_FIELDS like every other index block. ``segment``
        # is null rather than guessed when a segment_id was given: this tool
        # makes no read, so it knows the id but not the kind -- the same
        # convention resolve_range_scope uses for an unrecognised id.
        "requested_range": with_address(
            {},
            {
                "segment": "body" if segment_id is None else None,
                "segment_id": segment_id,
                "tab_id": tab_id,
                "start_index": start_index,
                "end_index": end_index,
            },
        ),
        "anchored_text": _clip(quoted_text),
        "stored_content": _clip(stored_content),
        "matches_request": (
            stored_content == content if stored_content is not None else None
        ),
    }
    if verdict.saved is None:
        verification["saved_unavailable"] = verdict.unavailable_reason
        verification.setdefault("notes", []).append(
            verdict.note(what="comment", post_id=post_id)
        )
    result = {
        "document_id": document_id,
        "comment_id": thread.get("commentId"),
        "post_id": post_id,
        "author": normalize_author(head_post.get("author")),
        "content": _clip(stored_content),
        "create_time": head_post.get("createTime"),
        "anchor_id": thread.get("anchorId"),
        "quoted_text": _clip(quoted_text),
        "status": thread.get("status"),
        "comment_update_state": comment_update_state,
        "verification": verification,
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
