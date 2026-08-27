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
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from auth.credential_store import peek_credential_store
from auth.oauth_config import is_oauth21_enabled, is_trust_gateway_identity
from core.email_identity import fold_email

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


#: The one spelling rule, shared with :mod:`gdocs_preview.preview_status` so
#: that a verdict cached under one spelling is found under the other. Kept under
#: a short local name because this module compares addresses everywhere.
#: Comparing exactly in one place and folding in another is how a case-variant
#: ``USER_GOOGLE_EMAIL`` came to be reported as having no stored credentials
#: *and* listed as an alternate account to switch to.
_fold = fold_email


def _distinct_accounts(emails: Iterable[str]) -> Tuple[str, ...]:
    """The stored accounts, one entry per address; first spelling wins.

    Folding here is what stops one address stored under two spellings from
    counting as two accounts — which would otherwise turn multi-account output
    on for a store that really holds one.
    """
    seen: set = set()
    distinct: List[str] = []
    for email in emails or ():
        folded = _fold(email)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        distinct.append(email)
    return tuple(distinct)


def _accounts_other_than(
    emails: Iterable[str], caller: Optional[str]
) -> Tuple[str, ...]:
    """Every distinct stored account that is not ``caller``."""
    folded_caller = _fold(caller)
    return tuple(
        email for email in _distinct_accounts(emails) if _fold(email) != folded_caller
    )


def _tool_registered(tool_name: str) -> bool:
    """Whether ``tool_name`` is actually registered on the running server.

    Pointing an agent at a tool that is not registered in its configuration is
    worse than saying nothing, so every mention of one below is conditional on
    this.

    The registered component table, NOT ``is_tool_enabled``. That function reads
    the ``--tools`` / ``--tool-tier`` selection set, which is ``None`` — meaning
    "no tier filtering", so ``True`` for every name that was ever asked about —
    in the default configuration and under a plain ``--tools`` selection. It
    also cannot see the other two filters ``filter_server_tools`` applies,
    read-only mode and granular permissions. Worse, a service that was never
    imported registers nothing at all, which no filter set records. All of that
    lands in the component table, which is why the table is the question to ask.

    Fails CLOSED: if we cannot tell, we say nothing rather than name a tool that
    may not be there.

    Read out of ``sys.modules`` rather than imported. :mod:`core.server` imports
    this module, so importing it back would be a cycle; and an absent entry
    means no server has been built in this process, which is not a
    configuration in which anything reads these strings.
    """
    server = getattr(sys.modules.get("core.server"), "server", None)
    if server is None:
        return False
    try:
        from core.tool_registry import get_tool_components

        return tool_name in get_tool_components(server)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not check whether %s is registered: %s", tool_name, exc)
        return False


def _list_accounts_tool_available() -> bool:
    return _tool_registered("list_google_accounts")


def capabilities_tool_available() -> bool:
    """Whether ``check_docs_review_capabilities`` is registered in this process.

    Public because :mod:`gdocs_preview.write_tools` asks the same question about
    the same tool, and two copies of that reasoning would drift apart.
    """
    return _tool_registered("check_docs_review_capabilities")


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
    "account. A tool error may NAME other accounts as candidates; naming them is "
    "not permission to use one — confirm the choice with the user, then call again "
    "with user_google_email set to the account they named. Never retry a failed "
    "call under a different account on your own — a Google notFound means either "
    "'no access' or 'no such document', so an automatic retry would walk every "
    "account in the wallet."
)

_NO_DEFAULT_ROUTING_NOTE = (
    "No default account is configured (USER_GOOGLE_EMAIL is unset) and this "
    "server holds more than one. Ask the user which account to use and pass it "
    "as user_google_email; do not choose one yourself, and do not infer one from "
    "the order they are listed in. Never retry a failed call under a different "
    "account — a Google notFound means either 'no access' or 'no such document', "
    "so an automatic retry would walk every account in the wallet."
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

    # _distinct_accounts, not directory.emails: one address stored under two
    # spellings is one account here too, or the report shows it twice and — now
    # that is_default folds — marks BOTH of them the default.
    accounts = [
        {
            "email": email,
            "is_default": bool(default_email) and _fold(email) == _fold(default_email),
            "docs_preview": preview_capability(email),
        }
        for email in _distinct_accounts(directory.emails)
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

    if default_email and directory.enumerated:
        # Folding decides IDENTITY, but the credential store keys its files by
        # the exact spelling (auth/credential_store.py:_get_credential_path). So
        # "same account" and "loadable under this spelling" are two questions,
        # and answering only the first would report a case-variant default as
        # authenticated while every call under it failed to find a credential.
        stored_exactly = any(email == default_email for email in directory.emails)
        stored_folded = next(
            (
                email
                for email in directory.emails
                if _fold(email) == _fold(default_email)
            ),
            None,
        )
        if stored_folded is None:
            notes.append(
                f"The configured default account {default_email} has no stored "
                "credentials; it has not completed the Google consent flow on this "
                "server."
            )
        elif not stored_exactly:
            notes.append(
                f"The configured default {default_email} differs only in case from "
                f"the stored account {stored_folded}. Google treats those as one "
                f"address, but the credential store keys its files by the exact "
                f"spelling, so loading credentials for {default_email} will fail. "
                f"Set USER_GOOGLE_EMAIL to {stored_folded}."
            )

    # Never point at check_docs_review_capabilities unconditionally: when the
    # docs_preview service is not loaded that tool is not registered either, so
    # the old advice named a tool that could not exist whenever it was printed.
    probe_hint = (
        " Run check_docs_review_capabilities against one account to find out."
        if capabilities_tool_available()
        else ""
    )
    if not preview_loaded:
        notes.append(
            "The docs_preview service is not loaded in this process, so the Developer "
            "Preview verdict is unknown for every account. Nothing was probed."
            + probe_hint
        )
    else:
        notes.append(
            "Developer Preview verdicts are the last cached observation per account; "
            "'unknown' means nothing has been observed yet, not a capability miss. "
            "Nothing was probed by this call." + probe_hint
        )

    if len(accounts) > 1:
        # "Use the default account" is not advice an agent can follow when there
        # is no default. That case is real — a store with several accounts and
        # no USER_GOOGLE_EMAIL — and single-user mode then binds to whichever
        # address sorts first, which is exactly what the agent must not imitate.
        notes.append(_ROUTING_NOTE if default_email else _NO_DEFAULT_ROUTING_NOTE)
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


#: Appended to the multi-account instructions only when the tool is registered.
_LIST_ACCOUNTS_SENTENCE = """

`list_google_accounts` reports the authenticated accounts and their cached Google Docs Developer Preview status without making any API call."""


def _multi_account_instructions(user_google_email: str, others: Sequence[str]) -> str:
    """The instructions string for a store holding more than one account.

    ``others`` is already filtered and folded by :func:`_accounts_other_than`,
    so this never has to decide what counts as a different account.
    """
    named = ", ".join(others)
    body = f"""Connected Google account: {user_google_email}

Also authenticated on this server: {named}

When using Google Workspace tools, use `{user_google_email}` as the `user_google_email` parameter. Do not ask the user for their email address.

Switch to another account only on an explicit instruction from the user. A tool error may name the other accounts as candidates — that names them, it does not choose one, so confirm the choice with the user before calling under a different account. Never retry a failed call under a different account on your own: a Google `notFound` means either "no access" or "no such document", so an automatic retry would walk every account above. {_WRITE_NOTE}"""
    if _list_accounts_tool_available():
        body += _LIST_ACCOUNTS_SENTENCE
    return body


def resolve_default_account() -> Optional[str]:
    """The configured default account, read from the environment right now.

    ``core.config.USER_GOOGLE_EMAIL`` applies the same rule but freezes the
    answer at import, which in ``main.py`` is before ``load_dotenv()`` runs.
    Anything rebuilt after configuration is final must re-derive it instead, or
    a ``MCP_ENABLE_OAUTH21`` that lives only in ``.env`` still yields the
    single-user answer.
    """
    if is_oauth21_enabled():
        return None
    return os.getenv("USER_GOOGLE_EMAIL") or None


def build_server_instructions(
    user_google_email: Optional[str], *, enumerate_store: bool = True
) -> Optional[str]:
    """Build the FastMCP ``instructions`` string for the configured default.

    Returns ``None`` when there is no configured default, and in both modes
    where the server rather than the caller fixes the account: trusted-gateway,
    where the verified principal supersedes the configured default, and OAuth
    2.1, where the principal is the authenticated caller. The OAuth 2.1 guard is
    defence in depth — every caller today passes
    :func:`resolve_default_account`, which already answers ``None`` there — but
    the credential store is shared across principals in that mode, so a caller
    that passed a raw environment value would enumerate other tenants' accounts
    into the handshake. The guard belongs where the enumeration is, not only in
    the callers.

    ``enumerate_store=False`` produces the single-account string without reading
    the credential store at all. That is what :mod:`core.server` uses for the
    value it hands FastMCP's constructor: at import time the environment may
    still be missing everything ``.env`` configures — the identity mode and the
    credentials directory included — so an enumeration there could name accounts
    out of a store the server never uses, or out of a store that is shared
    across principals. See :func:`core.server.refresh_server_instructions`.

    The multi-account lookup is wrapped: any credential-store failure falls back
    to the single-account string. A GCS deployment must not fail to start
    because of this feature.
    """
    if not user_google_email or _identity_mode() != "single_user":
        return None

    if enumerate_store:
        try:
            directory = enumerate_accounts()
            # More than one DISTINCT stored account, exactly as before — two
            # spellings of one address are one account, and a single stored
            # account that is not the configured default is still one account.
            distinct = _distinct_accounts(directory.emails)
            if directory.enumerated and len(distinct) > 1:
                return _multi_account_instructions(
                    user_google_email,
                    _accounts_other_than(distinct, user_google_email),
                )
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

#: The three kinds, as a type. A bare ``str`` parameter made a typo'd kind a
#: silent no-hint (logged and dropped); spelling the domain out makes it
#: something a checker or an editor catches at the call site instead.
HintKind = Literal["http_403", "http_404", "preview_unavailable"]

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
    "account only."
)

#: Only appended when ``list_google_accounts`` is registered in this process.
_PREVIEW_GRANULARITY_TOOL = (
    " list_google_accounts reports each account's last observed verdict without "
    "probing anything."
)


def _hint_framing(kind: str) -> Optional[Tuple[str, str]]:
    """``(sentence before the naming, sentence after it)`` for a hint kind.

    Built per call rather than held in a table: the pointer at
    ``list_google_accounts`` is only true when that tool survived tier
    filtering, and that is not known at import time. See
    :func:`_list_accounts_tool_available`.
    """
    if kind == HINT_403:
        return "", ""
    if kind == HINT_404:
        return _NOTFOUND_AMBIGUITY, ""
    if kind == HINT_PREVIEW_UNAVAILABLE:
        trailing = _PREVIEW_GRANULARITY
        if _list_accounts_tool_available():
            trailing += _PREVIEW_GRANULARITY_TOOL
        return "", trailing
    return None


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

    if not any(_fold(email) == _fold(caller) for email in directory.emails):
        return ()

    return _accounts_other_than(directory.emails, caller)


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


def _candidate_account_hint(user_google_email: Optional[str], kind: HintKind) -> str:
    framing = _hint_framing(kind)
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


def candidate_account_hint(user_google_email: Optional[str], kind: HintKind) -> str:
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


#: Google returns 403 for throttling as well as for authorization. Naming other
#: accounts on a throttle reads as "rotate identities until one is not rate
#: limited" — an enumeration sweep with a different trigger, and the quota is
#: usually per project rather than per user anyway, so it would not even work.
_THROTTLING_REASONS = (
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "dailyLimitExceeded",
    "sharingRateLimitExceeded",
)


def _is_throttling(error_details: Optional[str]) -> bool:
    if not error_details:
        return False
    lowered = error_details.casefold()
    return any(reason.casefold() in lowered for reason in _THROTTLING_REASONS)


def http_error_account_hint(
    user_google_email: Optional[str],
    status: Optional[int],
    error_details: Optional[str] = None,
) -> str:
    """:func:`candidate_account_hint` for a Google HTTP status. Never raises.

    The single entry point ``core.utils.handle_http_errors`` calls, so that the
    decision about WHICH failures earn an affordance lives here rather than in
    the upstream-owned hot path. A 403 that is really a throttle earns none:
    another account is not the answer to a rate limit, and saying so invites the
    identity rotation this module exists to prevent.
    """
    kind = _STATUS_HINTS.get(status) if isinstance(status, int) else None
    if kind is None:
        return ""
    if kind == HINT_403 and _is_throttling(error_details):
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
    account, where there is nothing arbitrary about the choice — including a
    store holding one address under two spellings, which is one account.
    """
    if isinstance(emails, str):
        # A bare string would otherwise iterate into characters and warn about
        # a wallet full of single letters.
        emails = (emails,)
    accounts = list(_distinct_accounts(emails or ()))
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
