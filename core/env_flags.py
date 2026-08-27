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
    reads the same way a human does: ``"1"`` when it is on, the empty string
    when it is off. Empty rather than deleted, because "the operator declined"
    and "the operator said nothing" are different — only the latter may be
    overridden by the loopback auto-grant in ``auth.google_auth``.

    An unrecognised value fails closed: a typo in a flag that disables a
    transport-security check must not be the thing that disables it. The value
    is logged at ERROR so the mistake is not silent.

    Returns True when the bypass is left enabled.
    """
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
        enabled = False

    os.environ[INSECURE_TRANSPORT_ENV_VAR] = "1" if enabled else ""
    return enabled
