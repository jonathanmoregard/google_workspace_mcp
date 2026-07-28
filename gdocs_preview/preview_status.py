"""Process-wide Developer Preview availability state for docs_preview.

The preview batchUpdate surface (acceptSuggestion, insertComment, ...) is
only usable after Workspace Developer Preview enrollment, and enrollment
cannot be verified offline. This module keeps the last-known availability
verdict (populated by the ``check_docs_review_capabilities`` probe) so that
side-effect-free capability reports can reuse real evidence instead of
re-probing.
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

_INITIAL_STATE: dict[str, Any] = {
    "availability": "unknown",  # unknown | available | unavailable
    "evidence": None,
    "source": None,  # probe | None
    "checked_at": None,
}

_state = dict(_INITIAL_STATE)


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
      - 404 -> document not found/inaccessible; proves nothing.
    """
    text = (message or "").lower()
    if status == 400:
        if any(marker in text for marker in _UNKNOWN_FIELD_MARKERS):
            return "unavailable", "not_enrolled"
        return "available", "preview_request_type_recognized"
    if status == 403:
        return "unknown", "permission_or_scope"
    if status == 404:
        return "unknown", "document_not_found"
    return "unknown", f"unexpected_http_{status}"


def record(availability: str, evidence: dict[str, Any], source: str) -> None:
    """Record a preview-availability observation (process-wide)."""
    _state.update(
        availability=availability,
        evidence=dict(evidence),
        source=source,
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


def get_status() -> dict[str, Any]:
    """Copy of the current preview-availability state."""
    status = dict(_state)
    if status["evidence"] is not None:
        status["evidence"] = dict(status["evidence"])
    return status


def reset() -> None:
    """Reset to the initial unknown state (used by tests)."""
    _state.clear()
    _state.update(_INITIAL_STATE)
