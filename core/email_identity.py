"""The one rule for comparing Google account addresses.

Two spellings of one address must not become two accounts. That claim has to
hold across module boundaries to mean anything — the Developer Preview cache
and the suggestion ledger both key per-caller state by address, and the account
directory compares it against ``USER_GOOGLE_EMAIL`` — so the rule lives in one
leaf module that imports nothing from this project, which is what makes it safe
to import from anywhere.

Case folding rather than lower-casing: ``str.casefold`` is the Unicode-aware
form specified for caseless matching. The local part of an address is formally
case-sensitive (RFC 5321 §2.4), but Google does not treat it that way and
neither does any provider this server talks to, so folding is the behaviour
that matches reality.

**What this deliberately does NOT do**, so that no caller reads more into it
than it delivers:

* It does not apply provider-specific aliasing. Gmail ignores dots in the local
  part and everything after a ``+``, so ``a.b@gmail.com``, ``ab@gmail.com`` and
  ``ab+work@gmail.com`` are one inbox that this function reports as three
  addresses. Canonicalising them would be wrong for Workspace domains, where
  dots are significant, and this server cannot tell the two cases apart offline.
* It does not Unicode-normalise. Two normal forms of one address would compare
  unequal. Google canonicalises what it issues, so this is theoretical.
* It is not a validator. A folded string is a comparison key, not evidence that
  an address exists or is well formed.

Above all it is **not** a credential-store lookup key. The store keys its files
by the exact spelling it was given (``auth/credential_store.py``), so two
addresses that fold together may still be one loadable credential and one
missing file. Code that needs "can I load this?" must ask the store, not this.
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
