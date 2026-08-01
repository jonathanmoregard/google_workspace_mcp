"""Developer Preview document read: tabs + comment/suggestion threads.

Also the home of :func:`read_for_review`, the ONE read every docs_preview
tool performs: the thread-bearing preview read, degrading to the GA
``documents.get`` when the preview surface is unavailable. Read tools call it
to answer the request; write tools call it once after a mutation to echo a
verifiable post-state (see :mod:`gdocs_preview.write_tools`).

``documents.get`` returns comment and suggestion **threads** -- and with them
the ``author`` every review record needs -- only when asked for them:

    GET https://docs.googleapis.com/v1/documents/{id}
        ?suggestionsViewMode=SUGGESTIONS_INLINE
        &commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED
        &includeTabsContent=true

``includeTabsContent=true`` is NOT optional here: without it the API answers
400 "Comments view mode may only be specified if tabs content is also
requested." Asking for tabs content moves the document content out of the
top-level ``body`` and into ``tabs[i].documentTab`` -- which is why this
module also carries the normalizer that hands
:mod:`gdocs_preview.analysis` the flat, GA-shaped Document it already knows
how to walk (per tab, so multi-tab documents analyse tab by tab).

**Why a raw authorized request.** ``commentsViewMode`` is absent from the
public Docs discovery document, so the googleapiclient ``docs`` Resource
rejects it before any request is sent
(``TypeError: Got an unexpected keyword argument commentsViewMode``;
``documents().get`` lists only ``documentId``, ``suggestionsViewMode`` and
``includeTabsContent``). Patching discovery for one query parameter is a
large blast radius for a small need, so the fetch tries the client first and
falls back to a ``google.auth.transport.requests.AuthorizedSession`` built
from the very credentials the injected Resource already holds
(``service._http.credentials``). When Google publishes the parameter, the
client path simply starts working and the fallback goes quiet.

Verified against the live API 2026-07-30 (enrolled Workspace Developer
Preview account) -- see docs/preview-api-reference.md for the payload shapes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

DOCS_GET_URL = "https://docs.googleapis.com/v1/documents/{document_id}"

#: ``google.apps.docs.v1.CommentsViewMode``. The API answers with
#: ``COMMENTS_VIEW_MODE_OMITTED`` when threads are not requested.
COMMENTS_VIEW_MODE_INCLUDED = "COMMENTS_VIEW_MODE_INCLUDED"

_REQUEST_TIMEOUT_SECONDS = 60


class PreviewReadError(Exception):
    """The preview (tabs + threads) read could not be performed.

    Always recoverable: callers fall back to the GA ``documents.get`` read
    and report the degradation rather than failing the tool call.
    """


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def credentials_from_service(service: Any) -> Any:
    """The OAuth credentials a built googleapiclient Resource is using.

    ``googleapiclient.discovery.build(..., credentials=creds)`` wraps them in
    a ``google_auth_httplib2.AuthorizedHttp`` exposed as ``service._http``
    (verified empirically: ``service._http.credentials is creds``). Private
    attribute, hence the defensive lookups and the explicit failure.
    """
    for holder, attribute in (
        (getattr(service, "_http", None), "credentials"),
        (service, "_credentials"),
    ):
        credentials = getattr(holder, attribute, None)
        if credentials is not None:
            return credentials
    raise PreviewReadError(
        "Could not reach the OAuth credentials of the injected Google service "
        "object; the preview (tabs + threads) read needs them to issue a raw "
        "authorized request."
    )


def _fetch_via_authorized_session(
    service: Any, document_id: str, view_mode: str
) -> dict[str, Any]:
    """The raw authorized GET, with EVERY failure shaped as a read failure.

    ``read_for_review`` catches :class:`PreviewReadError` and ``HttpError``
    and degrades to the GA read; anything else escapes the tool. A refused
    connection, a DNS blip or a timeout on this one request is exactly the
    transient condition the fallback exists for, and it used to come out as a
    raw ``requests.ConnectionError`` -- so a network hiccup hard-failed a
    read of a document the GA path could have answered, and hard-failed the
    post-write verification read too (where it becomes
    ``verification.source = "unavailable"`` instead of an exception only
    because that caller catches everything).
    """
    import google.auth.exceptions
    import requests.exceptions
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(credentials_from_service(service))
    try:
        response = session.get(
            DOCS_GET_URL.format(document_id=document_id),
            params={
                "suggestionsViewMode": view_mode,
                "commentsViewMode": COMMENTS_VIEW_MODE_INCLUDED,
                "includeTabsContent": "true",
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except (
        requests.exceptions.RequestException,
        google.auth.exceptions.GoogleAuthError,
    ) as error:
        # ConnectionError, Timeout, TooManyRedirects, SSLError; and the
        # credential-side RefreshError/TransportError, which is the same
        # network reaching the token endpoint instead.
        raise PreviewReadError(
            f"preview documents.get could not be performed: "
            f"{type(error).__name__}: {error}"
        ) from error
    finally:
        session.close()
    if response.status_code != 200:
        raise PreviewReadError(
            f"preview documents.get returned HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as error:  # pragma: no cover - defensive
        raise PreviewReadError(
            f"preview documents.get returned non-JSON content: {error}"
        ) from error


def _fetch_sync(service: Any, document_id: str, view_mode: str) -> dict[str, Any]:
    try:
        api_call = service.documents().get(
            documentId=document_id,
            suggestionsViewMode=view_mode,
            commentsViewMode=COMMENTS_VIEW_MODE_INCLUDED,
            includeTabsContent=True,
        )
    except TypeError as error:
        if "commentsViewMode" not in str(error):
            raise
        # Expected against the real client: the parameter is not in public
        # discovery (see the module docstring).
        return _fetch_via_authorized_session(service, document_id, view_mode)
    return api_call.execute()


async def fetch_document_with_threads(
    service: Any, document_id: str, view_mode: str
) -> dict[str, Any]:
    """Read a document WITH its comment and suggestion threads.

    Raises :class:`PreviewReadError` (or an ``HttpError`` from the client
    path) when the read is not available -- e.g. credentials whose project is
    not enrolled in the Workspace Developer Preview. Callers degrade to the
    GA read; enrollment must never hard-fail a read.
    """
    return await asyncio.to_thread(_fetch_sync, service, document_id, view_mode)


# ---------------------------------------------------------------------------
# Normalization: threads
# ---------------------------------------------------------------------------


def normalize_author(raw: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """``PostAuthor`` -> snake_case record, or ``None`` when absent.

    Never invents fields: ``anonymous`` is commonly omitted by the API and
    stays ``None`` rather than defaulting to ``False``.
    """
    if not isinstance(raw, dict):
        return None
    return {
        "display_name": raw.get("displayName"),
        "me": raw.get("me"),
        "anonymous": raw.get("anonymous"),
        "user": raw.get("user"),
    }


def normalize_post(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """One ``Post`` (thread head or reply) with its id and author."""
    raw = raw or {}
    return {
        "post_id": raw.get("postId"),
        "content": raw.get("content"),
        "author": normalize_author(raw.get("author")),
        "create_time": raw.get("createTime"),
        "update_time": raw.get("updateTime"),
    }


def suggestion_threads_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map suggestion id -> normalized ``SuggestionThread``.

    Empty when the payload carries no ``suggestions`` array (a GA read, or a
    tabs read without ``commentsViewMode``), so callers keep reporting
    ``author: null`` instead of guessing.
    """
    threads: dict[str, dict[str, Any]] = {}
    for raw in payload.get("suggestions") or []:
        if not isinstance(raw, dict):
            continue
        suggestion_id = raw.get("suggestionId")
        if not suggestion_id:
            continue
        head = raw.get("headPost") or {}
        threads[suggestion_id] = {
            "suggestion_id": suggestion_id,
            "head_post_id": head.get("postId"),
            "author": normalize_author(head.get("author")),
            "status": raw.get("status"),
            "create_time": head.get("createTime"),
            "update_time": head.get("updateTime"),
            "summary_text": raw.get("summaryText"),
            "replies": [normalize_post(p) for p in raw.get("replies") or []],
        }
    return threads


def anchor_tab_ids(payload: dict[str, Any]) -> dict[str, Optional[str]]:
    """``anchorId`` -> the id of the tab whose body the anchor lives in.

    A ``CommentThread`` carries NO tab field (verified against prod
    2026-08-01: the whole thread object is ``commentId``, ``anchorId``,
    ``headPost``, ``replies``, ``status``, ``plainTextQuote``). Its
    ``anchorId`` is the join key: every tab's ``documentTab.commentAnchors``
    is a map of the anchors living in THAT tab, and the maps are disjoint --
    a three-tab document with one comment per tab answered with one anchor
    in each map and nothing repeated.

    A duplicate anchor id across two tabs has never been observed and there
    is no mechanism for it (an anchor range cannot span tabs), but it maps to
    ``None`` rather than to whichever tab was walked first: an ambiguous
    attribution reported as a fact is the failure mode this join exists to
    avoid.
    """
    resolved: dict[str, Optional[str]] = {}

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            tab_id = (node.get("tabProperties") or {}).get("tabId")
            anchors = (node.get("documentTab") or {}).get("commentAnchors")
            if isinstance(anchors, dict):
                for anchor_id in anchors:
                    if anchor_id in resolved and resolved[anchor_id] != tab_id:
                        resolved[anchor_id] = None
                    else:
                        resolved[anchor_id] = tab_id
            walk(node.get("childTabs"))

    walk(payload.get("tabs"))
    return resolved


def comment_threads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalized ``CommentThread`` list: every thread and reply carries its
    id and author (the Docs-side comment surface, richer than Drive's:
    anchor id, per-post ids, People resource names).

    ``tab_id`` is joined on from the tabs (:func:`anchor_tab_ids`), because
    the thread object itself has none. ``None`` means this read could not
    place the comment -- an unanchored thread, an anchor in no tab's map, or
    a payload with no tabs at all -- and is never the default tab.
    """
    anchors = anchor_tab_ids(payload)
    threads = []
    for raw in payload.get("comments") or []:
        if not isinstance(raw, dict):
            continue
        head = normalize_post(raw.get("headPost"))
        anchor_id = raw.get("anchorId")
        threads.append(
            {
                "comment_id": raw.get("commentId"),
                "anchor_id": anchor_id,
                "tab_id": anchors.get(anchor_id) if anchor_id else None,
                "status": raw.get("status"),
                "quoted_text": raw.get("plainTextQuote"),
                "content": head["content"],
                "author": head["author"],
                "post_id": head["post_id"],
                "create_time": head["create_time"],
                "update_time": head["update_time"],
                "replies": [normalize_post(p) for p in raw.get("replies") or []],
            }
        )
    return threads


# ---------------------------------------------------------------------------
# Normalization: tabs -> GA-shaped Documents
# ---------------------------------------------------------------------------

#: Segment containers that live on ``documentTab`` in tabs mode and directly
#: on ``Document`` in GA mode. ``analysis`` walks exactly these.
_SEGMENT_FIELDS = ("body", "headers", "footers", "footnotes")


@dataclass
class TabDocument:
    """One tab's content, reshaped as a plain GA ``Document``.

    ``document`` is what :mod:`gdocs_preview.analysis` consumes: the same
    ``body``/``headers``/``footers``/``footnotes`` layout a non-tabs
    ``documents.get`` returns. Indexes are untouched -- verified against the
    live API: ``tabs[0].documentTab.body`` is identical to the GA read's
    ``body`` for a single-tab document.

    ``parent_tab_id``/``nesting_level`` carry the tree position the flatten
    would otherwise throw away. They are not decoration: ``index`` is a
    tab's position **among its siblings**, so a document whose first tab has
    a child answers with TWO tabs at ``index: 0`` (measured against prod
    2026-08-01), and without the parent nothing in the inventory says which
    of them is nested. A top-level tab has ``parent_tab_id: None`` and
    ``nesting_level: 0``; the GA fallback's single implicit tab has ``None``
    for both, because that read cannot see the tab tree at all.
    """

    tab_id: Optional[str]
    title: Optional[str]
    index: Optional[int]
    document: dict[str, Any] = field(default_factory=dict)
    parent_tab_id: Optional[str] = None
    nesting_level: Optional[int] = None

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "title": self.title,
            "index": self.index,
            "parent_tab_id": self.parent_tab_id,
            "nesting_level": self.nesting_level,
        }


def _tab_document(
    tab: dict[str, Any],
    envelope: dict[str, Any],
    *,
    parent_tab_id: Optional[str],
    nesting_level: int,
) -> TabDocument:
    properties = tab.get("tabProperties") or {}
    document_tab = tab.get("documentTab") or {}
    document: dict[str, Any] = {
        "documentId": envelope.get("documentId"),
        "title": envelope.get("title"),
    }
    for name in _SEGMENT_FIELDS:
        if name in document_tab:
            document[name] = document_tab[name]
    return TabDocument(
        tab_id=properties.get("tabId"),
        title=properties.get("title"),
        index=properties.get("index"),
        document=document,
        # Taken from the WALK, not from tabProperties: proto3 omits
        # ``nestingLevel: 0`` and a top-level tab carries no ``parentTabId``
        # at all, so the fields are absent exactly where they would have to
        # be read as defaults. The position in the tree cannot be omitted.
        parent_tab_id=parent_tab_id,
        nesting_level=nesting_level,
    )


def tab_documents(payload: dict[str, Any]) -> list[TabDocument]:
    """Flatten a tabs-mode payload into one GA-shaped Document per tab.

    Child tabs (``Tab.childTabs``) are included depth-first after their
    parent, so every tab of a nested document is analysed. A payload without
    ``tabs`` (the GA read, or the fallback path) yields a single implicit tab
    with ``tab_id=None`` -- one code path for both reads.

    Nesting is real and reachable through the API: ``addDocumentTab`` with
    ``tabProperties.parentTabId`` creates a child tab (verified against prod
    2026-08-01), and the read then returns it under its parent's
    ``childTabs``. Each tab keeps its own index space, so flattening changes
    no index.
    """
    tabs = payload.get("tabs")
    if not isinstance(tabs, list):
        return [
            TabDocument(
                tab_id=None, title=payload.get("title"), index=None, document=payload
            )
        ]

    flattened: list[TabDocument] = []

    def walk(nodes: list[Any], parent_tab_id: Optional[str], depth: int) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            tab = _tab_document(
                node, payload, parent_tab_id=parent_tab_id, nesting_level=depth
            )
            flattened.append(tab)
            children = node.get("childTabs")
            if isinstance(children, list):
                walk(children, tab.tab_id, depth + 1)

    walk(tabs, None, 0)
    return flattened


# ---------------------------------------------------------------------------
# One read, normalized for every review tool
# ---------------------------------------------------------------------------

#: ``read_source`` values reported by the review tools.
READ_SOURCE_PREVIEW = "preview_threads"
READ_SOURCE_GA = "ga_documents_get"


@dataclass
class ReviewRead:
    """One document read, normalized for the review tools.

    ``tabs`` is ``(tab_id, GA-shaped Document)`` per tab -- a single
    ``(None, document)`` entry for the GA fallback -- so the analysis layer
    has exactly one input shape to walk.

    **A read carries how much of the document it saw.** ``complete`` is not a
    diagnostic: it is the premise every ABSENCE claim downstream rests on. A
    suggestion id missing from a complete read is missing from the document;
    the same id missing from the GA fallback may be sitting in a tab this read
    structurally cannot see, because ``documents.get`` without
    ``includeTabsContent`` returns one unnamed body and no tab ids at all.
    Treating those two absences alike is how a post-write verification came to
    report ``still_pending: false`` off a blind read, and how a resolution came
    to claim it had garbage-collected every live suggestion in the other tabs.
    It defaults to ``False`` so that a read assembled by hand -- or by a future
    code path nobody has thought about yet -- cannot attest an absence it never
    checked.
    """

    tabs: list[tuple[Optional[str], dict[str, Any]]]
    tab_metadata: list[dict[str, Any]] = field(default_factory=list)
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    source: str = READ_SOURCE_GA
    degraded_reason: Optional[str] = None
    #: Did this read enumerate EVERY tab and segment of the document?
    complete: bool = False


async def fetch_ga_document(
    service: Any, document_id: str, view_mode: str
) -> dict[str, Any]:
    """Plain ``documents.get`` -- no tabs, no threads, always available."""
    api_call = service.documents().get(
        documentId=document_id, suggestionsViewMode=view_mode
    )
    return await asyncio.to_thread(api_call.execute)


async def read_for_review(
    service: Any, document_id: str, view_mode: str, *, user_google_email: str
) -> ReviewRead:
    """Read a document with its comment/suggestion threads, degrading to the
    GA read when the Developer Preview surface is unavailable.

    Authors, thread status and Google's own suggestion summaries live only
    on the preview payload (docs/preview-api-reference.md). Enrollment is a
    property of the caller's GCP project, not of the document, so a failure
    here must never fail the read: the GA payload still carries the full
    suggestion algebra, just without authorship.

    A preview payload that carries NO tabs degrades the same way. It is the
    one shape that fails without failing: it parses, it yields zero tabs, and
    the whole review layer answers from nothing -- ``suggestion_count: 0``,
    ``body_text: ""`` -- while ``read_source`` still says the preview read
    worked. "The document has no suggestions" and "this read saw nothing" are
    not the same sentence, and the second one has to be said out loud.

    Either degradation returns ``complete=False``: the GA payload has one
    unnamed body and no tab ids, so nothing downstream may read an id's
    absence from it as the id's absence from the document (see
    :class:`ReviewRead`).
    """
    from googleapiclient.errors import HttpError

    from gdocs_preview import preview_status

    try:
        payload = await fetch_document_with_threads(service, document_id, view_mode)
    except (PreviewReadError, HttpError) as error:
        logger.info(
            f"[docs_preview] thread-bearing read unavailable for {document_id}; "
            f"falling back to the GA documents.get read: {error}"
        )
        document = await fetch_ga_document(service, document_id, view_mode)
        return ReviewRead(
            tabs=[(None, document)],
            source=READ_SOURCE_GA,
            degraded_reason=str(error)[:300],
        )

    tabs = tab_documents(payload)
    if not tabs:
        # ``tabs: []`` parses fine and carries no content at all, so every
        # tool downstream answered from nothing: suggestion_count 0,
        # body_text "", read_source "preview_threads" and NOTHING saying the
        # read had seen a document with no tabs in it. Every Google Doc has
        # at least one tab, so this is a broken payload, not an empty
        # document -- take the same fallback a failed read takes, which is
        # also what puts ``degraded_notice`` in the response.
        reason = (
            "the preview read returned an empty tabs array, which carries no "
            "document content at all; every Google Doc has at least one tab"
        )
        logger.info(f"[docs_preview] {reason} for {document_id}; falling back to GA")
        document = await fetch_ga_document(service, document_id, view_mode)
        return ReviewRead(
            tabs=[(None, document)],
            source=READ_SOURCE_GA,
            degraded_reason=reason,
        )
    preview_status.record(
        "available",
        {
            "http_status": 200,
            "reason": "preview_read_succeeded",
            # Which preview surface this evidence is actually about. A
            # thread-bearing read IS preview-gated, so its success is real
            # evidence -- but the batchUpdate request types are a separate
            # surface, and whether enrollment covers both is an open
            # UNCERTAIN item (docs/preview-api-reference.md). A caller
            # deciding whether a WRITE will work should read this field
            # rather than the bare verdict.
            "surface": "read",
        },
        source="tool_call",
        user_google_email=user_google_email,
    )
    return ReviewRead(
        tabs=[(tab.tab_id, tab.document) for tab in tabs],
        tab_metadata=[tab.metadata for tab in tabs],
        threads=suggestion_threads_by_id(payload),
        comments=comment_threads(payload),
        source=READ_SOURCE_PREVIEW,
        # The ONE path that saw the whole document: ``includeTabsContent``
        # returned every tab (``tab_documents`` walks childTabs too) and
        # :mod:`gdocs_preview.analysis` walks every segment of each. Both
        # fallbacks above leave ``complete`` at its False default, so an id
        # absent from them is not an id absent from the document.
        complete=True,
    )
