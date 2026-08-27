"""One strict parser for the boolean environment flags.

The mode flags (``MCP_ENABLE_OAUTH21``, ``TRUST_GATEWAY_IDENTITY``,
``WORKSPACE_MCP_STATELESS_MODE``, ``EXTERNAL_OAUTH21_PROVIDER``, ...) used to
be parsed three different ways in three different places: ``MCP_ENABLE_OAUTH21=1``
counted as on for the startup banner and for the credential store, while
``auth.oauth_config`` compared the raw string to ``"true"`` and read it as off —
so the server ran single-user with no protocol auth while the banner said the
flag was on. Everything that reads one of those flags now parses it here.

Deliberately dependency-free, so every layer can import it without a cycle.
"""

from typing import Optional

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


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
