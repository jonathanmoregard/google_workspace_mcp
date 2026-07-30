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


def comment_threads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalized ``CommentThread`` list: every thread and reply carries its
    id and author (the Docs-side comment surface, richer than Drive's:
    anchor id, per-post ids, People resource names)."""
    threads = []
    for raw in payload.get("comments") or []:
        if not isinstance(raw, dict):
            continue
        head = normalize_post(raw.get("headPost"))
        threads.append(
            {
                "comment_id": raw.get("commentId"),
                "anchor_id": raw.get("anchorId"),
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
    """

    tab_id: Optional[str]
    title: Optional[str]
    index: Optional[int]
    document: dict[str, Any] = field(default_factory=dict)

    @property
    def metadata(self) -> dict[str, Any]:
        return {"tab_id": self.tab_id, "title": self.title, "index": self.index}


def _tab_document(tab: dict[str, Any], envelope: dict[str, Any]) -> TabDocument:
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
    )


def tab_documents(payload: dict[str, Any]) -> list[TabDocument]:
    """Flatten a tabs-mode payload into one GA-shaped Document per tab.

    Child tabs (``Tab.childTabs``) are included depth-first after their
    parent, so every tab of a nested document is analysed. A payload without
    ``tabs`` (the GA read, or the fallback path) yields a single implicit tab
    with ``tab_id=None`` -- one code path for both reads.
    """
    tabs = payload.get("tabs")
    if not isinstance(tabs, list):
        return [
            TabDocument(
                tab_id=None, title=payload.get("title"), index=None, document=payload
            )
        ]

    flattened: list[TabDocument] = []

    def walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            flattened.append(_tab_document(node, payload))
            children = node.get("childTabs")
            if isinstance(children, list):
                walk(children)

    walk(tabs)
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
    """

    tabs: list[tuple[Optional[str], dict[str, Any]]]
    tab_metadata: list[dict[str, Any]] = field(default_factory=list)
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    source: str = READ_SOURCE_GA
    degraded_reason: Optional[str] = None


async def fetch_ga_document(
    service: Any, document_id: str, view_mode: str
) -> dict[str, Any]:
    """Plain ``documents.get`` -- no tabs, no threads, always available."""
    api_call = service.documents().get(
        documentId=document_id, suggestionsViewMode=view_mode
    )
    return await asyncio.to_thread(api_call.execute)


async def read_for_review(service: Any, document_id: str, view_mode: str) -> ReviewRead:
    """Read a document with its comment/suggestion threads, degrading to the
    GA read when the Developer Preview surface is unavailable.

    Authors, thread status and Google's own suggestion summaries live only
    on the preview payload (docs/preview-api-reference.md). Enrollment is a
    property of the caller's GCP project, not of the document, so a failure
    here must never fail the read: the GA payload still carries the full
    suggestion algebra, just without authorship.
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
    preview_status.record(
        "available",
        {"http_status": 200, "reason": "preview_read_succeeded"},
        source="tool_call",
    )
    return ReviewRead(
        tabs=[(tab.tab_id, tab.document) for tab in tabs],
        tab_metadata=[tab.metadata for tab in tabs],
        threads=suggestion_threads_by_id(payload),
        comments=comment_threads(payload),
        source=READ_SOURCE_PREVIEW,
    )
