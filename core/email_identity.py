"""The one rule for comparing Google account addresses.

Two spellings of one address must never become two accounts. That claim has to
hold across module boundaries to mean anything — the credential store derives
its filenames from the address, the Developer Preview cache keys its per-caller
state by it, and the account directory compares it against
``USER_GOOGLE_EMAIL`` — so the rule lives in one leaf module that imports
nothing from this project, which is what makes it safe to import from anywhere.

Case folding rather than lower-casing: ``str.casefold`` is the Unicode-aware
form specified for caseless matching. The local part of an address is formally
case-sensitive (RFC 5321 §2.4), but Google does not treat it that way and
neither does any provider this server talks to, so folding is the behaviour
that matches reality.
"""

from __future__ import annotations

from typing import Optional


def fold_email(email: Optional[str]) -> str:
    """``email`` reduced to its comparison form: trimmed and case-folded.

    ``None`` and blank input fold to ``""`` so that callers can compare without
    a separate emptiness check; an empty result is never equal to a real
    address, which is the behaviour every caller here wants.
    """
    return (email or "").strip().casefold()
