"""Round 3 — per-caller state is keyed by identity, and identity must be real.

Two spellings of one address are one caller (so a verdict recorded under one
is found under the other), and a caller with no address is not a caller at all
(so it cannot be given a drawer that the next anonymous call reads back as its
own).
"""

import pytest

from gdocs_preview import preview_status, suggestion_ledger


def _reset():
    preview_status.reset()


def test_a_verdict_recorded_under_one_spelling_is_found_under_another():
    _reset()
    preview_status.record(
        "available",
        {"reason": "probe"},
        source="tool_call",
        user_google_email="Solo@Example.com",
    )

    assert preview_status.get_status("solo@example.com")["availability"] == "available"
    assert preview_status.get_status("SOLO@EXAMPLE.COM")["availability"] == "available"


def test_a_verdict_is_still_private_to_its_own_caller():
    """The control: folding must not merge two genuinely different callers."""
    _reset()
    preview_status.record(
        "available",
        {"reason": "probe"},
        source="tool_call",
        user_google_email="solo@example.com",
    )

    assert preview_status.get_status("work@example.com")["availability"] == "unknown"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_recording_without_a_caller_is_refused(blank):
    _reset()

    with pytest.raises(ValueError, match="must identify a caller"):
        preview_status.record(
            "available",
            {"reason": "probe"},
            source="tool_call",
            user_google_email=blank,
        )

    assert preview_status.get_status("")["availability"] == "unknown"


def test_the_ledger_folds_the_caller_too():
    """Otherwise explain_missing tells the reader they have not read it."""
    suggestion_ledger.reset()

    folded = suggestion_ledger._key("solo@example.com", "doc-1")

    assert folded == ("solo@example.com", "doc-1")
    assert suggestion_ledger._key("Solo@Example.com", "doc-1") == folded
    assert suggestion_ledger._key("SOLO@EXAMPLE.COM ", "doc-1") == folded
    # Different callers stay different.
    assert suggestion_ledger._key("work@example.com", "doc-1") != folded
