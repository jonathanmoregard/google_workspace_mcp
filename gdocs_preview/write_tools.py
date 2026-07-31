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
from typing import Any, Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdocs_preview import preview_status, suggestion_ledger
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
    """

    def __init__(self, read: Any) -> None:
        self.source: str = read.source
        analysed = extract_suggestions_from_tabs(read.tabs, read.threads)
        self.records: dict[str, dict[str, Any]] = {
            r["suggestion_id"]: r for r in analysed["suggestions"]
        }
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

    def text_at(
        self, record: dict[str, Any]
    ) -> tuple[Optional[BaseText], Optional[str]]:
        """The base text of the ONE ``(tab, segment)`` ``record`` lives in.

        Returns ``(text, None)``, or ``(None, reason)`` naming why this read
        cannot say. A record that names no tab is resolved the way
        :func:`gdocs_preview.address.resolve_range_scope` resolves an omitted
        ``tab_id``: implicitly when the read has one tab, and not at all when
        it has several -- a guessed tab makes ``matches_expectation`` a
        statement about a different part of the document.

        The reason is returned rather than swallowed because a bare ``None``
        window is three different situations wearing one face, and an agent
        acts differently on each: an ambiguous multi-tab read is fixed by
        naming the tab, a degraded read is fixed by retrying, and a missing
        anchor means somebody else edited the document. Reporting all three
        as ``resulting_text: null`` also made them indistinguishable from
        "we never listed this id" -- which is the one case where nothing is
        wrong at all.
        """
        segment_id = record.get("segment_id") or None
        tab_id = record.get("tab_id") or None
        if tab_id is None:
            candidates = sorted({tab for tab in self.tab_ids if tab})
            if len(candidates) > 1:
                return None, "ambiguous_tab"
            tab_id = candidates[0] if candidates else None
        text = self.base_texts.get((tab_id, segment_id))
        if text is None:
            return None, "segment_not_in_read"
        return text, None


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


def _unlocated_note(
    reason: str,
    *,
    suggestion_id: str,
    record: Optional[dict[str, Any]],
    read: "_PostWriteRead",
) -> str:
    """One sentence saying why no verdict was reached, and what to do."""
    if reason == "suggestion_not_listed":
        return (
            f"this session never listed suggestion {suggestion_id!r}, so there "
            f"is no expected text to compare the document against. "
            f"{_NOT_A_FAILED_WRITE} Call list_document_suggestions before "
            "resolving to get the before/after text of the card."
        )
    if reason == "ambiguous_tab":
        tabs = sorted({t for t in read.tab_ids if t})
        return (
            f"suggestion {suggestion_id!r} was listed without a tab_id and this "
            f"read has {len(tabs)} tabs ({', '.join(tabs)}), so the text at its "
            "range cannot be located: an index means a different place in each "
            f"tab. {_NOT_A_FAILED_WRITE} Re-read with "
            "list_document_suggestions(tab_id=...) to see the resulting text."
        )
    if reason == "segment_not_in_read":
        where = (
            f"tab={(record or {}).get('tab_id')!r}, "
            f"segment={(record or {}).get('segment_id')!r}"
        )
        return (
            f"the post-write read does not contain the ({where}) that "
            f"suggestion {suggestion_id!r} was listed in, so its range could "
            f"not be located -- read_source is {read.source!r}, and the GA "
            "documents.get carries no tabs at all, so a read that degraded "
            f"loses every tab id. {_NOT_A_FAILED_WRITE} Retry the read."
        )
    if reason == AMBIGUOUS_ANCHOR:
        return (
            f"the base text around suggestion {suggestion_id!r}'s range repeats "
            "in that segment, so more than one place reads as its range and "
            "they do not agree on whether the resolution landed. No verdict is "
            f"reported rather than one picked at random. {_NOT_A_FAILED_WRITE} "
            "Read the range back with get_doc_review_view(start_index=..., "
            "end_index=...) to see the one place you resolved."
        )
    if reason == "nothing_to_compare":
        return (
            f"suggestion {suggestion_id!r} was listed without the before/after "
            "text a resolution is checked against, so there was nothing to "
            f"compare the document to. {_NOT_A_FAILED_WRITE} Call "
            "list_document_suggestions(fields='full') before resolving."
        )
    return (
        f"the base text immediately before suggestion {suggestion_id!r}'s range "
        "is no longer in that segment, so the range could not be located -- "
        "that text is unaffected by accepting or rejecting, so the likeliest "
        "cause is a concurrent edit by another editor between the listing and "
        f"this write. {_NOT_A_FAILED_WRITE} Re-read the document to see its "
        "current state."
    )


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
    """

    #: Is the resolved id STILL in the post-write pending set?
    still_pending: bool
    #: What :func:`~gdocs_preview.analysis.check_resolution` said about the
    #: text at the range, or ``None`` when no comparison could be made.
    text_check: Optional[bool]
    #: The agent-facing verdict, which is never positive while the suggestion
    #: is pending.
    matches_expectation: Optional[bool]

    def __post_init__(self) -> None:
        if self.still_pending and self.matches_expectation is not False:
            raise ValueError(
                "matches_expectation cannot be "
                f"{self.matches_expectation!r} while still_pending is true: "
                "the id is in the post-write pending set, so the resolution "
                "did not take effect. Build this with _ResolutionVerdict."
                "derive(), which cannot produce that pair."
            )

    @classmethod
    def derive(
        cls, *, still_pending: bool, text_check: Optional[bool]
    ) -> "_ResolutionVerdict":
        """The verdict the two pieces of evidence add up to.

        A pending id is a decided ``False``; only once it is gone does the
        text decide, and a text check that could not run stays ``None`` there
        (with :data:`UNLOCATED_REASONS` naming why).
        """
        return cls(
            still_pending=still_pending,
            text_check=text_check,
            matches_expectation=False if still_pending else text_check,
        )


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


def _is_ours(record: dict[str, Any]) -> bool:
    """Did the AUTHENTICATED user write this suggestion?

    ``PostAuthor.me`` is the API's own answer and the only evidence there is;
    it is ``None`` on a read that degraded to the GA ``documents.get``, which
    carries no threads and therefore no authors. Unknown authorship is NOT
    ownership: a strict ``is True`` keeps the degraded read from
    manufacturing a claim it cannot support.
    """
    return ((record.get("author") or {}).get("me")) is True


def _concurrent_note(suggestion_ids: list[str], read: "_PostWriteRead") -> str:
    """One sentence about suggestions that appeared but are not ours."""
    names = ", ".join(repr(sid) for sid in suggestion_ids)
    unknown = any(
        (read.records[sid].get("author") or {}).get("me") is None
        for sid in suggestion_ids
    )
    why = (
        "this read carries no authors (read_source="
        f"{read.source!r}), so authorship could not be established"
        if unknown
        else "the suggestion thread names a different author"
    )
    return (
        f"{names} appeared between the last listing and this write, and is "
        f"NOT reported as created by this call: {why}. A second reviewer "
        "editing the same document produces exactly this, and attributing "
        "their card to your write would have you resolve or reply to it as "
        "your own. Check the author before acting on it."
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
            end_index, summary_text, status], pending_suggestion_count, and
            -- only when they apply -- suggestions_at_edit_range (when the
            API reported no new id, which happens when the edit merged into
            an existing same-author suggestion) with the range_scope it was
            read in, appeared_since_last_read, also_removed_suggestion_ids,
            and notes}.

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
        f"start={start_index}, end={end_index}, "
        f"segment={segment_id or 'body'}, tab={tab_id or 'default'}"
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
    known_before: Optional[frozenset[str]],
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
    """
    if not verify:
        return {"source": "skipped", "reason": "verify=false"}
    read, failure = await _post_write_read(service, document_id)
    if read is None:
        return {"source": "unavailable", "reason": failure}

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
    if known_before is not None:
        for sid in read.records:
            if sid not in known_before and sid not in echoed_ids:
                appeared.append(sid)
    others: list[str] = []
    for sid in appeared:
        if _is_ours(read.records[sid]):
            echoed_ids.append(sid)
        else:
            others.append(sid)

    verification: dict[str, Any] = {
        "source": "post_write_read",
        "read_source": read.source,
        "created_suggestions": [_echo_suggestion(read.records[s]) for s in echoed_ids],
        "pending_suggestion_count": len(read.records),
    }
    if others:
        verification["appeared_since_last_read"] = [
            _echo_suggestion(read.records[s]) for s in others
        ]
        verification.setdefault("notes", []).append(_concurrent_note(others, read))
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
            {source, read_source, still_pending, resolved_suggestion
            (including its segment/segment_id/tab_id, so its indexes are a
            complete address), expected_text, resulting_text,
            matches_expectation, pending_suggestion_count,
            pending_suggestion_ids, and -- only when they apply --
            resulting_text_unavailable, also_removed_suggestion_ids and
            notes}.

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

            matches_expectation is null ONLY when no check could be run, and
            resulting_text_unavailable then names which of the four reasons
            it was: suggestion_not_listed (this session never listed the id,
            so there is no before/after to compare), ambiguous_tab (the card
            carried no tab_id and the document has several),
            segment_not_in_read (the post-write read lost that (tab,
            segment) -- a degraded GA read carries no tab ids), or
            anchor_not_found (the base text before the range is gone, which
            means a concurrent edit). None of them says the write failed;
            still_pending is the evidence about that.
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
    # The two pieces of evidence, added up in ONE place. `matches` from here
    # on is the derived verdict, not the text check: see _ResolutionVerdict.
    verdict = _ResolutionVerdict.derive(
        still_pending=suggestion_id in read.records, text_check=matches
    )
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
    if verdict.still_pending:
        # First note, because it is the one that changed the verdict. The
        # unlocated note below explains a missing resulting_text; this one
        # explains a false matches_expectation, and they can both apply.
        verification.setdefault("notes", []).append(
            _still_pending_note(action, suggestion_id, verdict.text_check)
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
            )
        )

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
        verification.setdefault("notes", []).extend(
            suggestion_ledger.collateral_note(r) for r in resolutions if not r.direct
        )
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
            # The last agent-facing index block that was not built from
            # ADDRESS_FIELDS; it omitted ``segment``, so the echo of a
            # footnote comment read like a body one. ``segment`` is null
            # rather than guessed when a segment_id was given: this tool
            # makes no read, so it knows the id but not the kind -- the same
            # convention resolve_range_scope uses for an unrecognised id.
            "anchored_range": with_address(
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
        },
        "link": _doc_link(document_id),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)
