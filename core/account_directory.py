"""Multi-account directory: who is authenticated, and how to route between them.

Fork-owned module (upstream has no multi-account concept). It holds three
things so that the edits to upstream files stay one-liners:

* :func:`enumerate_accounts` — offline enumeration of the credential store,
  which distinguishes "no accounts" from "could not read the store".
* :func:`build_account_report` / :func:`render_account_report` — what the
  ``list_google_accounts`` tool answers with.
* :func:`build_server_instructions` — the FastMCP ``instructions`` string.
* :func:`candidate_account_hint` / :func:`http_error_account_hint` — the
  sentence a failed call appends to name the accounts it did NOT try.

Two rules bind everything here:

**No network calls, ever.** Probing another identity — even read-only — is
itself an access attempt and must be an explicit decision, never an
implementation detail hidden inside a resolver. The Developer Preview verdict
is read from whatever ``check_docs_review_capabilities`` already cached; an
account nothing is cached for is reported ``unknown``, never ``unavailable``.

**No automatic cross-account fallback.** Google Drive answers ``404 notFound``
for both "no read access" and "no such file"
(<https://developers.google.com/workspace/drive/api/guides/handle-errors>), so
an automatic retry under a second identity would turn a typo'd document id into
an enumeration sweep across the wallet. This module names the alternatives and
stops there.

The preview verdict is read via ``sys.modules`` rather than by importing
:mod:`gdocs_preview.preview_status`. ``gdocs_preview/__init__`` imports modules
that do ``from core.server import server``, so importing it from ``core`` is a
circular import *and* registers all seven preview tools as a decorator side
effect. ``importlib.import_module`` does not help — Python initialises the
parent package first. An absent module means the docs_preview service is not
loaded in this process, which is a capability *unknown*, not a capability miss.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from auth.credential_store import peek_credential_store
from auth.oauth_config import is_oauth21_enabled, is_trust_gateway_identity

logger = logging.getLogger(__name__)

#: Read out of ``sys.modules`` only — never imported. See the module docstring.
PREVIEW_STATUS_MODULE = "gdocs_preview.preview_status"

#: The store was enumerated successfully (``emails`` may still be empty).
STORE_OK = "ok"
#: The store exists but could not be read — "0 accounts" would be a lie.
STORE_UNREADABLE = "unreadable"
#: The configured backend cannot enumerate users at all (GCS).
STORE_UNSUPPORTED = "unsupported"
#: The store could not be constructed or failed unexpectedly.
STORE_UNAVAILABLE = "unavailable"
#: Enumeration was deliberately not attempted (gateway / OAuth 2.1 mode).
STORE_NOT_ENUMERATED = "not_enumerated"

_UNKNOWN_CAPABILITY: Dict[str, Any] = {
    "availability": "unknown",
    "source": None,
    "checked_at": None,
}


@dataclass(frozen=True)
class AccountDirectory:
    """What the credential store could be made to say, and how confidently."""

    emails: Tuple[str, ...]
    status: str
    detail: Optional[str] = None

    @property
    def enumerated(self) -> bool:
        return self.status == STORE_OK


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _describe_local_directory(store: Any) -> Optional[Tuple[str, Optional[str]]]:
    """Tell an empty local credential directory apart from an unreadable one.

    ``LocalDirectoryCredentialStore.list_users()`` returns ``[]`` for a missing
    directory AND for one it cannot read (it swallows ``OSError``). A
    permissions problem must not silently render as "0 accounts", so when the
    listing came back empty we look at the directory ourselves.

    Returns ``None`` for a backend with no local directory to look at.
    """
    base_dir = getattr(store, "base_dir", None)
    if not isinstance(base_dir, str) or not base_dir:
        return None
    if not os.path.isdir(base_dir):
        return STORE_OK, f"credentials directory does not exist yet: {base_dir}"
    try:
        os.listdir(base_dir)
    except OSError as exc:
        return (
            STORE_UNREADABLE,
            f"credentials directory {base_dir} is unreadable: {exc}",
        )
    return STORE_OK, None


def enumerate_accounts() -> AccountDirectory:
    """List every account with stored credentials. Offline; never raises.

    The store is resolved lazily here rather than at import: resolving it raises
    ``ValueError`` for a misconfigured backend and constructs a
    ``storage.Client()`` on the GCS path, neither of which may happen while a
    module is being imported. ``peek_credential_store`` rather than
    ``get_credential_store`` because this runs at import time, before
    ``main.py`` has loaded ``.env`` — caching a store built from that
    environment would hand every later caller the wrong credentials directory.
    """
    try:
        store = peek_credential_store()
    except ValueError as exc:
        return AccountDirectory((), STORE_UNAVAILABLE, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not obtain the credential store: %s", exc)
        return AccountDirectory((), STORE_UNAVAILABLE, str(exc))

    try:
        emails = tuple(store.list_users() or ())
    except NotImplementedError as exc:
        return AccountDirectory((), STORE_UNSUPPORTED, str(exc))
    except Exception as exc:
        logger.warning("Could not list accounts in the credential store: %s", exc)
        return AccountDirectory((), STORE_UNREADABLE, str(exc))

    if emails:
        return AccountDirectory(emails, STORE_OK, None)

    described = _describe_local_directory(store)
    if described is None:
        return AccountDirectory(
            (),
            STORE_OK,
            f"no stored accounts; {type(store).__name__} exposes no directory to "
            "verify that against",
        )
    status, detail = described
    return AccountDirectory((), status, detail)


# ---------------------------------------------------------------------------
# Developer Preview capability, from cache only
# ---------------------------------------------------------------------------


def _preview_status_module() -> Optional[Any]:
    return sys.modules.get(PREVIEW_STATUS_MODULE)


def preview_capability(user_google_email: str) -> Dict[str, Any]:
    """The cached Developer Preview verdict for one account. No probing.

    ``evidence`` is deliberately dropped: it is the failed call's error text and
    ``HttpError.__str__`` embeds the request URI, i.e. a DOCUMENT ID.
    """
    module = _preview_status_module()
    if module is None:
        return dict(_UNKNOWN_CAPABILITY)
    try:
        status = module.get_status(user_google_email)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not read the docs_preview status for %s: %s",
            user_google_email,
            exc,
        )
        return dict(_UNKNOWN_CAPABILITY)
    if not isinstance(status, dict):
        return dict(_UNKNOWN_CAPABILITY)
    return {
        "availability": status.get("availability") or "unknown",
        "source": status.get("source"),
        "checked_at": status.get("checked_at"),
    }


# ---------------------------------------------------------------------------
# The list_google_accounts report
# ---------------------------------------------------------------------------

_ROUTING_NOTE = (
    "Use the default account. Switch only when the user explicitly names another "
    "account, or when a tool error names one as the account to use. Never retry a "
    "failed call under a different account on your own — a Google notFound means "
    "either 'no access' or 'no such document', so an automatic retry would walk "
    "every account in the wallet."
)

_WRITE_NOTE = (
    "Comments and suggestions are authored as the account and are visible to "
    "everyone with the document, so no write may switch identity."
)


def _identity_mode() -> str:
    if is_trust_gateway_identity():
        return "trusted_gateway"
    if is_oauth21_enabled():
        return "oauth21_multi_user"
    return "single_user"


def _managed_identity_report(mode: str) -> Dict[str, Any]:
    """The report for a mode where the server, not the caller, picks the account.

    The credential store is shared across principals in both modes, so it is not
    enumerated here: handing one caller the other callers' email addresses is a
    cross-tenant leak, and the caller could not act on them anyway.
    """
    if mode == "trusted_gateway":
        note = (
            "Trusted-gateway mode: the account is fixed per request by the verified "
            "gateway assertion. You cannot choose an account here, and the shared "
            "credential store is not enumerated."
        )
    else:
        note = (
            "OAuth 2.1 multi-user mode: the account is the authenticated principal of "
            "the request. You cannot choose an account here, and the shared credential "
            "store is not enumerated."
        )
    return {
        "identity_mode": mode,
        "default_account": None,
        "accounts": [],
        "accounts_enumerated": False,
        "store_status": STORE_NOT_ENUMERATED,
        "store_detail": None,
        "docs_preview_loaded": _preview_status_module() is not None,
        "probed": False,
        "notes": [note],
    }


def build_account_report(default_email: Optional[str]) -> Dict[str, Any]:
    """Everything ``list_google_accounts`` knows, without touching the network."""
    mode = _identity_mode()
    if mode != "single_user":
        return _managed_identity_report(mode)

    directory = enumerate_accounts()
    preview_loaded = _preview_status_module() is not None
    notes: List[str] = []

    accounts = [
        {
            "email": email,
            "is_default": bool(default_email) and email == default_email,
            "docs_preview": preview_capability(email),
        }
        for email in directory.emails
    ]

    if directory.status == STORE_UNREADABLE:
        notes.append(
            "The credential store could not be read, so this is NOT a report of zero "
            f"accounts — the account list is unknown: {directory.detail}"
        )
    elif directory.status == STORE_UNSUPPORTED:
        notes.append(
            "The configured credential backend cannot list accounts, so the account "
            f"list is unknown: {directory.detail}"
        )
    elif directory.status == STORE_UNAVAILABLE:
        notes.append(
            f"The credential store is unavailable, so the account list is unknown: "
            f"{directory.detail}"
        )
    elif not accounts:
        notes.append(
            "No authenticated accounts."
            + (f" {directory.detail}" if directory.detail else "")
        )

    if default_email and directory.enumerated and default_email not in directory.emails:
        notes.append(
            f"The configured default account {default_email} has no stored "
            "credentials; it has not completed the Google consent flow on this server."
        )

    if not preview_loaded:
        notes.append(
            "The docs_preview service is not loaded in this process, so the Developer "
            "Preview verdict is unknown for every account. It was not probed — run "
            "check_docs_review_capabilities against one account to find out."
        )
    else:
        notes.append(
            "Developer Preview verdicts are the last cached observation per account; "
            "'unknown' means nothing has been observed yet, not a capability miss. "
            "Nothing was probed by this call."
        )

    if len(accounts) > 1:
        notes.append(_ROUTING_NOTE)
        notes.append(_WRITE_NOTE)

    return {
        "identity_mode": mode,
        "default_account": default_email or None,
        "accounts": accounts,
        "accounts_enumerated": directory.enumerated,
        "store_status": directory.status,
        "store_detail": directory.detail,
        "docs_preview_loaded": preview_loaded,
        "probed": False,
        "notes": notes,
    }


def render_account_report(default_email: Optional[str]) -> str:
    """:func:`build_account_report` as the JSON the tool returns."""
    return json.dumps(build_account_report(default_email), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------


def _single_account_instructions(user_google_email: str) -> str:
    """The instructions string exactly as this fork inherited it.

    Byte-identical to upstream's inline f-string. Do not reflow it: a test
    asserts the literal bytes, because a fork that stays merge-friendly must not
    change single-account behaviour.
    """
    return f"""Connected Google account: {user_google_email}

When using Google Workspace tools, always use `{user_google_email}` as the `user_google_email` parameter. Do not ask the user for their email address."""


def _multi_account_instructions(user_google_email: str, emails: Sequence[str]) -> str:
    others = ", ".join(email for email in emails if email != user_google_email)
    return f"""Connected Google account: {user_google_email}

Also authenticated on this server: {others}

When using Google Workspace tools, use `{user_google_email}` as the `user_google_email` parameter. Do not ask the user for their email address.

Switch to another account only on an explicit instruction from the user, or when a tool error names one of the other accounts as the one to use. Never retry a failed call under a different account on your own: a Google `notFound` means either "no access" or "no such document", so an automatic retry would walk every account above. {_WRITE_NOTE}

`list_google_accounts` reports the authenticated accounts and their cached Google Docs Developer Preview status without making any API call."""


def build_server_instructions(user_google_email: Optional[str]) -> Optional[str]:
    """Build the FastMCP ``instructions`` string for the configured default.

    Returns ``None`` when there is no configured default, and in trusted-gateway
    mode where the verified principal supersedes it.

    Called at import time (FastMCP's constructor needs the value), so the
    multi-account lookup is wrapped: any credential-store failure falls back to
    the single-account string. A GCS deployment must not fail to start because
    of this feature.
    """
    if not user_google_email or is_trust_gateway_identity():
        return None

    try:
        directory = enumerate_accounts()
        if directory.enumerated and len(directory.emails) > 1:
            return _multi_account_instructions(user_google_email, directory.emails)
    except Exception as exc:  # pragma: no cover - enumerate_accounts absorbs these
        logger.warning(
            "Could not inspect the credential store for multi-account server "
            "instructions (%s); using the single-account instructions.",
            exc,
        )

    return _single_account_instructions(user_google_email)


# ---------------------------------------------------------------------------
# Naming the other accounts when a call fails
# ---------------------------------------------------------------------------

#: A Docs/Drive 403: this account is authenticated but not allowed.
HINT_403 = "http_403"
#: A Docs/Drive 404, whose meaning is ambiguous by Google's own documentation.
HINT_404 = "http_404"
#: A preview-gated tool refused because the cached verdict for this account is
#: ``unavailable`` (the API rejected the preview request field itself).
HINT_PREVIEW_UNAVAILABLE = "preview_unavailable"

_NOTFOUND_AMBIGUITY = (
    "A Google notFound means either that this account cannot reach that id or "
    "that no such document exists: Drive returns notFound for both "
    "(https://developers.google.com/workspace/drive/api/guides/handle-errors), "
    "so it is not evidence that the document exists."
)

#: Whether Developer Preview enrollment is per-account, per-Cloud-project, or
#: both is undocumented for the two-accounts-one-OAuth-client case
#: (``docs/preview-api-reference.md``). The hint must not resolve it by
#: assertion in either direction — it says only that this failure is evidence
#: about ONE account, which is true under every candidate answer.
_PREVIEW_GRANULARITY = (
    "Whether Developer Preview enrollment follows the account or the Cloud "
    "project is an open question, so this failure is evidence about this "
    "account only; list_google_accounts reports each account's last observed "
    "verdict without probing anything."
)

#: kind -> (sentence before the naming, sentence after it).
_HINT_FRAMING: Dict[str, Tuple[str, str]] = {
    HINT_403: ("", ""),
    HINT_404: (_NOTFOUND_AMBIGUITY, ""),
    HINT_PREVIEW_UNAVAILABLE: ("", _PREVIEW_GRANULARITY),
}

#: HTTP status -> hint kind. 401 is deliberately absent: it means nobody is
#: authenticated for this call, not that the wrong identity was used, and the
#: fix is to re-authenticate rather than to look at a second account.
_STATUS_HINTS: Dict[int, str] = {403: HINT_403, 404: HINT_404}


def other_accounts(user_google_email: Optional[str]) -> Tuple[str, ...]:
    """The authenticated accounts a failed call was NOT attempted under.

    Empty — meaning "say nothing" — in four cases, each of which would
    otherwise put a claim in an error message that the evidence does not
    support:

    * **Trusted-gateway and OAuth 2.1 modes.** The credential store is shared
      across principals there, so its contents are other tenants' addresses.
      Handing them to a caller who cannot act on them is a leak.
    * **The store could not be enumerated.** "There are no other accounts" must
      never be inferred from "the store could not be read".
    * **The caller's own account is unknown.** ``handle_http_errors``
      substitutes ``"N/A"`` when the tool takes no ``user_google_email``
      keyword; without knowing which account failed there is no "other", and
      naming the failed account as a candidate would invite exactly the retry
      this feature exists to prevent.
    * **The store does not contain the caller's account.** The enumeration is
      then describing a different world from the one the call ran in, so the
      set it produces is not "the others".
    """
    if _identity_mode() != "single_user":
        return ()

    caller = (user_google_email or "").strip()
    if "@" not in caller:
        return ()

    directory = enumerate_accounts()
    if not directory.enumerated:
        return ()

    folded = caller.casefold()
    if not any(email.strip().casefold() == folded for email in directory.emails):
        return ()

    return tuple(
        email for email in directory.emails if email.strip().casefold() != folded
    )


def _no_attempt_sentence(count: int) -> str:
    """Say plainly that nothing was tried under the accounts just named."""
    if count == 1:
        pronoun, condition = "it", "that account is the one the user meant"
    else:
        pronoun, condition = "them", "one of them is the one the user meant"
    return (
        f"No call was attempted under {pronoun} — this server never retries a "
        f"failed call under a different account. If {condition}, confirm that "
        f"with the user and then call again with user_google_email set to it; "
        f"do not try the accounts in turn."
    )


def _candidate_account_hint(user_google_email: Optional[str], kind: str) -> str:
    framing = _HINT_FRAMING.get(kind)
    if framing is None:
        logger.warning("Unknown candidate-account hint kind: %r", kind)
        return ""

    others = other_accounts(user_google_email)
    if not others:
        return ""

    preamble, trailing = framing
    naming = "Other accounts authenticated on this server: " + ", ".join(others) + "."
    parts = (preamble, naming, _no_attempt_sentence(len(others)), trailing)
    return " " + " ".join(part for part in parts if part)


def candidate_account_hint(user_google_email: Optional[str], kind: str) -> str:
    """A sentence naming the other accounts, or ``""`` when there is none.

    The return value is meant to be appended verbatim to an existing error
    message, so it carries its own leading space and is empty whenever the
    affordance does not apply — with zero or one authenticated account the
    caller's message is left byte-identical to what this fork inherited.

    **Never raises.** This runs while another error is being formatted, and a
    failure here that escaped would replace the real failure with itself.
    """
    try:
        return _candidate_account_hint(user_google_email, kind)
    except Exception as exc:
        logger.warning(
            "Could not build the candidate-account hint (%s); leaving the error "
            "message unchanged.",
            exc,
        )
        return ""


def http_error_account_hint(
    user_google_email: Optional[str], status: Optional[int]
) -> str:
    """:func:`candidate_account_hint` for a Google HTTP status. Never raises.

    The single entry point ``core.utils.handle_http_errors`` calls, so that the
    decision about WHICH statuses earn an affordance lives here rather than in
    the upstream-owned hot path.
    """
    kind = _STATUS_HINTS.get(status) if isinstance(status, int) else None
    if kind is None:
        return ""
    return candidate_account_hint(user_google_email, kind)


# ---------------------------------------------------------------------------
# Single-user mode's arbitrary pick
# ---------------------------------------------------------------------------


def warn_on_arbitrary_account_pick(
    emails: Iterable[str], chosen: Optional[str]
) -> None:
    """Announce that single-user mode bound to one of several stored accounts.

    Diagnostic only — the caller still uses ``chosen``. Silent for zero or one
    account, where there is nothing arbitrary about the choice.
    """
    accounts = [email for email in (emails or ()) if email]
    if len(accounts) < 2:
        return
    logger.warning(
        "[single-user] %d authenticated Google accounts found (%s); using %s because "
        "it sorts first. Set USER_GOOGLE_EMAIL to pin the account you want, or call "
        "list_google_accounts to see them all.",
        len(accounts),
        ", ".join(accounts),
        chosen,
    )
