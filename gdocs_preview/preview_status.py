"""Per-caller Developer Preview availability state for docs_preview.

The preview batchUpdate surface (acceptSuggestion, insertComment, ...) is
only usable after Workspace Developer Preview enrollment, and enrollment
cannot be verified offline. This module keeps the last-known availability
verdict (populated by the ``check_docs_review_capabilities`` probe) so that
side-effect-free capability reports can reuse real evidence instead of
re-probing.

**Keyed by ``user_google_email``**, for the same reason
:mod:`gdocs_preview.suggestion_ledger` is. This was one process-global dict,
and the server's DEFAULT transport mode is multi-user (``main.py``: anything
without ``--single-user``). Two things then crossed between tenants on a
probe-free ``check_docs_review_capabilities``, which makes no API call and
answers purely from here:

* **The evidence.** ``evidence["message"]`` is the failed call's error text,
  and ``HttpError.__str__`` embeds the request URI -- i.e. the DOCUMENT ID.
  Caller B's capability report handed back caller A's document id verbatim.
* **The verdict.** B was told ``available`` on the strength of A's probe,
  labelled ``source: "probe"``, having probed nothing. Whether enrollment
  propagates per-project or per-account is still an open UNCERTAIN item
  (``docs/preview-api-reference.md``), so even the verdict alone was a claim
  this module had no evidence for on B's behalf.

An unknown caller gets ``unknown`` -- which is the honest answer, and the one
that costs a probe rather than a wrong belief.
"""

from __future__ import annotations

import time
from typing import Any, Optional

#: Substrings (lowercased) that mark a proto-parse failure, i.e. the API
#: rejected the *request field itself* -- the caller is not enrolled in the
#: Developer Preview, so the field does not exist for them.
_UNKNOWN_FIELD_MARKERS = (
    "unknown name",
    "cannot find field",
    "invalid json payload",
)

#: Substrings (lowercased) that mark a *semantic* rejection of a preview
#: request -- the API parsed the request type and only objected to its
#: arguments. Empirically (2026-07-30, first enrolled run) the probe's
#: bogus-id acceptSuggestion comes back as HTTP 404 "Suggestion with ID ...
#: does not exist.", not 400: the suggestion id is treated as a missing
#: subresource. A bare 404 proves nothing (the document itself may be
#: missing), so only these markers upgrade a 404 to "available".
_SEMANTIC_PREVIEW_MARKERS = (
    "suggestion with id",
    "comment with id",
    "reply with id",
)

_INITIAL_STATE: dict[str, Any] = {
    "availability": "unknown",  # unknown | available | unavailable
    "evidence": None,
    "source": None,  # probe | tool_call | None
    "checked_at": None,
}

#: Callers tracked at once, per process. Eviction is oldest-touched-first,
#: matching :data:`gdocs_preview.suggestion_ledger.MAX_DOCUMENTS`. An evicted
#: caller falls back to ``unknown``, i.e. to re-probing -- never to somebody
#: else's verdict.
MAX_USERS = 64

#: user_google_email -> state. Insertion order is the LRU order.
_states: dict[str, dict[str, Any]] = {}


def classify_preview_error(status: Optional[int], message: str) -> tuple[str, str]:
    """Classify a failed preview batchUpdate call into an availability
    verdict plus a reason.

    Shapes (to be re-verified against the real API in the e2e phase):
      - 400 mentioning an unknown field -> the request type was not parsed:
        not enrolled (``unavailable``).
      - other 400 -> the request type WAS parsed and failed on semantics
        (e.g. nonexistent suggestion id): preview surface reachable
        (``available``).
      - 403 -> scope/permission problem; proves nothing about enrollment.
      - 404 naming a missing suggestion/comment/reply -> the request type WAS
        parsed and resolved far enough to look the subresource up: preview
        surface reachable (``available``). Verified against the real API on
        2026-07-30 - the bogus-id probe returns 404, not 400.
      - other 404 -> document not found/inaccessible; proves nothing.
    """
    text = (message or "").lower()
    if status == 400:
        if any(marker in text for marker in _UNKNOWN_FIELD_MARKERS):
            return "unavailable", "not_enrolled"
        return "available", "preview_request_type_recognized"
    if status == 403:
        return "unknown", "permission_or_scope"
    if status == 404:
        if any(marker in text for marker in _SEMANTIC_PREVIEW_MARKERS):
            return "available", "preview_request_type_recognized"
        return "unknown", "document_not_found"
    return "unknown", f"unexpected_http_{status}"


def record(
    availability: str,
    evidence: dict[str, Any],
    source: str,
    *,
    user_google_email: str,
) -> None:
    """Record a preview-availability observation for ONE caller.

    ``user_google_email`` is keyword-only and has no default: the evidence
    carries the caller's own document ids and error text, and a call site that
    cannot say whose observation this is must not be able to file it where
    another caller will read it back as their own.
    """
    key = user_google_email or ""
    state = _states.pop(key, None) or dict(_INITIAL_STATE)
    state.update(
        availability=availability,
        evidence=dict(evidence),
        source=source,
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    while len(_states) >= MAX_USERS:
        _states.pop(next(iter(_states)))
    _states[key] = state  # re-insert: dict order is the LRU order


def get_status(user_google_email: str) -> dict[str, Any]:
    """Copy of ``user_google_email``'s preview-availability state.

    A caller nothing has been recorded for gets the initial ``unknown`` --
    not the last verdict some other caller happened to produce.
    """
    state = _states.get(user_google_email or "")
    status = dict(state) if state is not None else dict(_INITIAL_STATE)
    if status["evidence"] is not None:
        status["evidence"] = dict(status["evidence"])
    return status


def reset() -> None:
    """Forget every caller's state (used by tests)."""
    _states.clear()
