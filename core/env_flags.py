"""One strict parser for the boolean environment flags.

The mode flags (``MCP_ENABLE_OAUTH21``, ``TRUST_GATEWAY_IDENTITY``,
``WORKSPACE_MCP_STATELESS_MODE``, ``EXTERNAL_OAUTH21_PROVIDER``, ...) used to
be parsed three different ways in three different places: ``MCP_ENABLE_OAUTH21=1``
counted as on for the startup banner and for the credential store, while
``auth.oauth_config`` compared the raw string to ``"true"`` and read it as off —
so the server ran single-user with no protocol auth while the banner said the
flag was on. Everything that reads one of those flags now parses it here.

``OAUTHLIB_INSECURE_TRANSPORT`` is the exception that also lives here, because
oauthlib reads that one out of ``os.environ`` itself and never parses it. We
cannot make it use this parser, so instead we settle the *value* at startup
(:func:`normalize_insecure_transport_env`) and describe it by oauthlib's rule
rather than ours (:func:`insecure_transport_bypass_active`).

Deliberately dependency-free, so every layer can import it without a cycle.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

#: oauthlib reads this variable itself, straight out of ``os.environ`` and
#: without a parser, so it is the one flag whose value we cannot simply choose
#: how to interpret. See ``normalize_insecure_transport_env``.
INSECURE_TRANSPORT_ENV_VAR = "OAUTHLIB_INSECURE_TRANSPORT"

#: Whether an operator explicitly set the flag to a value meaning "off".
#:
#: Held in process state rather than in the environment on purpose. The
#: variable itself cannot carry this: oauthlib tests the truthiness of whatever
#: string is there, so any marker we left behind would either read as "lift the
#: HTTPS requirement" (any non-empty value) or be indistinguishable from unset
#: (the empty string, which an earlier revision used and which silently
#: suppressed the loopback grant for people who never typed anything).
_explicitly_declined = False

#: The raw value rejected by the strict parser, kept so the startup banner can
#: still show a typo that normalisation has already removed from the
#: environment.
_rejected_value: Optional[str] = None


def parse_bool_env(value: Optional[str]) -> bool:
    """Parse a boolean env var value, failing loudly on anything unrecognised.

    Accepts (case-insensitive, whitespace-trimmed):
        true:  ``1``, ``true``, ``yes``, ``on``
        false: ``0``, ``false``, ``no``, ``off``, empty string, None

    Raises ValueError for any other input. The strict parsing matters for
    security-relevant flags (``MCP_ENABLE_OAUTH21``,
    ``WORKSPACE_MCP_GCS_REQUIRE_CMEK``) where a typo like ``"treu"`` would
    otherwise silently leave the flag off.
    """
    if value is None:
        return False
    normalised = value.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise ValueError(
        f"Invalid boolean env var value: {value!r}. "
        f"Expected one of: {sorted((_TRUE_VALUES | _FALSE_VALUES) - {''})}"
    )


def insecure_transport_bypass_active() -> bool:
    """Report whether oauthlib is currently skipping its HTTPS requirement.

    This deliberately does NOT use :func:`parse_bool_env`. oauthlib's
    ``is_secure_transport`` (``oauthlib/oauth2/rfc6749/utils.py``, 3.3.1) reads
    the variable as ``os.environ.get(...)`` and returns early on the truthiness
    of the resulting *string*, so any non-empty value lifts the requirement —
    ``"0"`` and ``"false"`` included. Describing the flag by our own parser is
    how the startup banner came to print "off" for a process in which OAuth
    token exchange over plain HTTP was being accepted.
    """
    return bool(os.environ.get(INSECURE_TRANSPORT_ENV_VAR))


def normalize_insecure_transport_env() -> bool:
    """Make ``OAUTHLIB_INSECURE_TRANSPORT`` mean what its value says.

    Reads the variable with the strict parser and rewrites it to a form oauthlib
    reads the same way a human does: ``"1"`` when it is on, and *removed* when
    it is off, since oauthlib's only question is whether the string is
    non-empty.

    Removed rather than left present-and-empty, because the empty string is a
    value oauthlib can see, and an earlier revision's use of it as a marker
    silently suppressed the loopback grant. Removing it also stops a stale
    ``"0"`` being inherited by a child process, where it would read as on. The
    operator's decline is recorded in process state instead, where oauthlib
    cannot mistake it for a request to lift the requirement — see
    :func:`insecure_transport_explicitly_declined`.

    An unrecognised value fails closed: a typo in a flag that disables a
    transport-security check must not be the thing that disables it. The value
    is logged at ERROR and kept for the startup banner, so the mistake is
    neither silent nor invisible once the variable itself has been removed.

    Only a variable that is present is read. Once a decline has been recorded,
    later calls against the now-absent variable leave that decision standing.

    Returns True when the bypass is left enabled.
    """
    global _explicitly_declined, _rejected_value

    raw = os.environ.get(INSECURE_TRANSPORT_ENV_VAR)
    if raw is None:
        return False

    try:
        enabled = parse_bool_env(raw)
    except ValueError:
        logger.error(
            "%s=%r is not a recognised boolean. Refusing to lift oauthlib's "
            "HTTPS requirement on an unreadable value; treating it as off.",
            INSECURE_TRANSPORT_ENV_VAR,
            raw,
        )
        _rejected_value = raw
        enabled = False
    else:
        _rejected_value = None

    if enabled:
        os.environ[INSECURE_TRANSPORT_ENV_VAR] = "1"
        _explicitly_declined = False
        return True

    os.environ.pop(INSECURE_TRANSPORT_ENV_VAR, None)
    _explicitly_declined = True
    return False


def insecure_transport_explicitly_declined() -> bool:
    """Whether an operator asked for the HTTPS requirement to stand.

    A decline is a veto, and it outranks the loopback auto-grant in
    ``auth.google_auth``. That grant fires on a redirect URI that merely
    *looks* like loopback, so this is the operator's only way to stop a
    deployment that is in fact public from having the requirement lifted for
    it. Nothing in the shipped configuration sets a falsey value, so a decline
    only ever comes from someone typing one.
    """
    return _explicitly_declined


def insecure_transport_rejected_value() -> Optional[str]:
    """The unparseable value normalisation removed, if there was one."""
    return _rejected_value


def reset_insecure_transport_decision() -> None:
    """Forget the recorded decision so the environment is read afresh.

    The counterpart to ``auth.oauth_config.reload_oauth_config`` for this one
    flag: used by tests, and by anything re-initialising a process in place.
    """
    global _explicitly_declined, _rejected_value
    _explicitly_declined = False
    _rejected_value = None
