"""Curated review tools for the docs_preview service.

Hand-written ergonomics layered on top of the generated API-parity surface
in :mod:`gdocs_preview.generated`: suggestion diffing with computed pre/post
text, a capabilities/preview-availability report, and a reviewer-view read
tool. Additive only -- these never replace generated tools.

The heavy lifting lives in the pure functions of
:mod:`gdocs_preview.analysis`; tools here are thin API-call wrappers.
"""

import asyncio
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.scopes import DOCS_PREVIEW_SCOPES
from auth.service_decorator import require_google_service
from core.server import server
from core.utils import UserInputError, handle_http_errors
from gdocs_preview import preview_status
from gdocs_preview.analysis import extract_suggestions, render_document

VIEW_MODES = (
    "SUGGESTIONS_INLINE",
    "PREVIEW_SUGGESTIONS_ACCEPTED",
    "PREVIEW_WITHOUT_SUGGESTIONS",
)

CURATED_TOOL_NAMES = [
    "docs_review_list_suggestions",
    "docs_review_capabilities",
    "docs_review_read_document",
]

#: Deliberately unresolvable suggestion id used by the capabilities probe.
#: Suggestion ids are server-generated; this shape cannot collide with one.
_PROBE_SUGGESTION_ID = "gdocs-review-capabilities-probe-nonexistent-suggestion"


@lru_cache(maxsize=1)
def _manifest_summary() -> dict[str, Any]:
    manifest_path = Path(__file__).resolve().parent / "generated" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tools = manifest["tools"]
    return {
        "total": len(tools),
        "preview": sum(1 for t in tools if t["preview"]),
        "ga": sum(1 for t in tools if not t["preview"]),
        "write_mode_capable": sum(1 for t in tools if t["write_mode"]),
        "by_kind": dict(Counter(t["kind"] for t in tools)),
    }


async def _get_document(service: Any, document_id: str, view_mode: str) -> dict:
    api_call = service.documents().get(
        documentId=document_id, suggestionsViewMode=view_mode
    )
    return await asyncio.to_thread(api_call.execute)


@server.tool(
    title="Docs Review: list suggestions",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors(
    "docs_review_list_suggestions", is_read_only=True, service_type="docs"
)
@require_google_service("docs", "docs_read")
async def docs_review_list_suggestions(
    service: Any,
    user_google_email: str,
    document_id: str,
) -> str:
    """List every pending edit suggestion in a document, with computed
    pre/post text.

    Reads the document in SUGGESTIONS_INLINE view and returns one record per
    suggestion id: type (insertion/deletion/replacement/style/mixed),
    pre_text (base text of the affected range: all insertions stripped, all
    deletions kept), post_text (the range with this suggestion -- and only
    this one -- applied), ~40-char context windows computed on the base
    text, segment location (body/header/footer/footnote incl. segment id),
    table flag, and start/end indexes.

    Indexes are UTF-16 code units relative to the SUGGESTIONS_INLINE view,
    passed through from the API verbatim -- exactly what batchUpdate
    requests computed against that view expect.

    Authors are only available from Developer Preview suggestion-thread
    objects; when those are absent the record carries ``author: null`` and
    ``author_source: "unavailable"`` (never guessed).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to analyse.

    Returns:
        str: JSON with document_id, title, suggestion_count and the
            per-suggestion records described above.
    """
    document = await _get_document(service, document_id, "SUGGESTIONS_INLINE")
    result = extract_suggestions(document)
    return json.dumps(result, indent=2, ensure_ascii=False)


@server.tool(
    title="Docs Review: capabilities",
    annotations=ToolAnnotations(
        readOnlyHint=False,  # probe=true POSTs a content-safe batchUpdate
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("docs_review_capabilities", service_type="docs")
@require_google_service("docs", "docs_write")
async def docs_review_capabilities(
    service: Any,
    user_google_email: str,
    document_id: Optional[str] = None,
    probe: bool = False,
) -> str:
    """Report what the docs_preview service can do with the current
    credentials, including Developer Preview availability.

    Side-effect free by default: reports configured scopes, the generated
    tool inventory, and the cached (last-known) preview availability
    verdict. No API call is made unless ``probe=true``.

    With ``probe=true`` (requires ``document_id``) it performs the cheapest
    real preview check that cannot mutate user-visible content: a
    batchUpdate containing a single acceptSuggestion request for a
    deliberately nonexistent suggestion id. Accepting a nonexistent
    suggestion cannot alter the document. Outcome classification:
      - 400 unknown-field error -> preview ``unavailable`` (not enrolled;
        the request type was not even parsed);
      - other 400 -> ``available`` (request type recognized, failed only on
        the bogus suggestion id);
      - 200 -> ``available`` (per preview docs, suggestion/comment updates
        can no-op with a commentUpdateState instead of erroring);
      - 403/404 -> ``unknown`` (permission/scope or missing document --
        proves nothing about enrollment).
    The verdict is cached process-wide, so later probe-free calls report it.

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): Document used for the live probe. Only needed
            with probe=true.
        probe (bool): Perform the live preview-availability check described
            above. Defaults to False (no API call).

    Returns:
        str: JSON report: scopes, generated_tools summary, curated_tools,
            preview {availability, evidence, source, checked_at},
            probe_performed.
    """
    if probe:
        if not document_id:
            raise UserInputError(
                "probe=true requires document_id (the live preview check "
                "runs against a real document)."
            )
        body = {
            "requests": [{"acceptSuggestion": {"suggestionId": _PROBE_SUGGESTION_ID}}]
        }
        try:
            api_call = service.documents().batchUpdate(
                documentId=document_id, body=body
            )
            response = await asyncio.to_thread(api_call.execute)
            preview_status.record(
                "available",
                {
                    "http_status": 200,
                    "reason": "probe_request_accepted",
                    "comment_update_state": (response or {}).get("commentUpdateState"),
                },
                source="probe",
            )
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            content = getattr(error, "content", b"") or b""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            message = f"{error} {content}"
            availability, reason = preview_status.classify_preview_error(
                status, message
            )
            preview_status.record(
                availability,
                {
                    "http_status": status,
                    "reason": reason,
                    "message": message[:500],
                },
                source="probe",
            )

    report = {
        "service": "docs_preview",
        "scopes": list(DOCS_PREVIEW_SCOPES),
        "generated_tools": _manifest_summary(),
        "curated_tools": list(CURATED_TOOL_NAMES),
        "preview": preview_status.get_status(),
        "probe_performed": bool(probe),
    }
    return json.dumps(report, indent=2, ensure_ascii=False)


@server.tool(
    title="Docs Review: read document",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("docs_review_read_document", is_read_only=True, service_type="docs")
@require_google_service("docs", "docs_read")
async def docs_review_read_document(
    service: Any,
    user_google_email: str,
    document_id: str,
    view_mode: str = "SUGGESTIONS_INLINE",
) -> str:
    """Read a document the way a reviewer sees it: plain text with inline
    suggestion markers plus a paragraph map.

    In SUGGESTIONS_INLINE view (default), pending insertions render as
    ``{+text+}`` and pending deletions as ``{-text-}`` (CriticMarkup
    style); each paragraph entry lists the suggestion ids touching it.
    PREVIEW_SUGGESTIONS_ACCEPTED / PREVIEW_WITHOUT_SUGGESTIONS return the
    respective clean text. Comments are not part of the documents.get
    payload -- list them with the Drive comment tools
    (``drive_api_comments_list``).

    Args:
        user_google_email (str): The user's Google email address. Required.
        document_id (str): The ID of the document to read.
        view_mode (str): One of SUGGESTIONS_INLINE,
            PREVIEW_SUGGESTIONS_ACCEPTED, PREVIEW_WITHOUT_SUGGESTIONS.

    Returns:
        str: JSON with view_mode, body_text, per-paragraph map
            (segment/indexes/text/style/list/table/suggestion_ids),
            header/footer/footnote texts, and all suggestion ids.
    """
    if view_mode not in VIEW_MODES:
        raise UserInputError(
            f"Invalid view_mode '{view_mode}'. Must be one of: {', '.join(VIEW_MODES)}."
        )
    document = await _get_document(service, document_id, view_mode)
    rendered = render_document(document)
    result = {"view_mode": view_mode, **rendered}
    return json.dumps(result, indent=2, ensure_ascii=False)
