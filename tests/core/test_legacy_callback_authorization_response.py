"""The legacy OAuth 2.0 callback must not depend on the proxied request scheme.

oauthlib refuses to parse a non-HTTPS authorization response and the only way
past it is ``OAUTHLIB_INSECURE_TRANSPORT``, which lifts the HTTPS requirement
for every OAuth exchange in the process. A TLS-terminating reverse proxy
forwards plain HTTP upstream, and nothing here reads ``X-Forwarded-Proto``, so
``request.url`` is ``http://`` on exactly the deployments whose callback is
``https://``. Rebuilding the response on the redirect URI keeps that login
working without the bypass.
"""

import os
from types import SimpleNamespace

import pytest
from oauthlib.oauth2.rfc6749.errors import InsecureTransportError

from core.server import _authorization_response_url


@pytest.fixture
def https_required():
    """Guarantee oauthlib's HTTPS requirement is actually in force.

    Not defensive boilerplate — without it this file passes alone and fails in
    the full suite. ``_allow_insecure_transport_for_local_redirect`` writes
    ``OAUTHLIB_INSECURE_TRANSPORT`` straight into ``os.environ``, and
    monkeypatch cannot restore a variable it never recorded, so the loopback
    tests elsewhere leave it set for every test that runs after them. A test
    asserting that oauthlib REFUSES plain HTTP has to own that variable or it
    silently asserts nothing.
    """
    from core.env_flags import (
        INSECURE_TRANSPORT_ENV_VAR,
        reset_insecure_transport_decision,
    )

    previous = os.environ.get(INSECURE_TRANSPORT_ENV_VAR)
    os.environ.pop(INSECURE_TRANSPORT_ENV_VAR, None)
    reset_insecure_transport_decision()
    try:
        yield
    finally:
        os.environ.pop(INSECURE_TRANSPORT_ENV_VAR, None)
        if previous is not None:
            os.environ[INSECURE_TRANSPORT_ENV_VAR] = previous
        reset_insecure_transport_decision()


def _request(url: str):
    """Enough of a Starlette request for the helper: it reads `.url.query`."""
    from urllib.parse import urlsplit

    return SimpleNamespace(url=SimpleNamespace(query=urlsplit(url).query))


TLS_TERMINATED = "http://mcp.example.com/oauth2callback?state=abc&code=xyz"


def test_scheme_and_host_come_from_the_redirect_uri_not_the_request():
    result = _authorization_response_url(
        _request(TLS_TERMINATED), "https://mcp.example.com/oauth2callback"
    )

    assert result == "https://mcp.example.com/oauth2callback?state=abc&code=xyz"


def test_googles_query_is_preserved_verbatim():
    """It carries `code` and `state`; losing either breaks the exchange."""
    result = _authorization_response_url(
        _request(
            "http://mcp.example.com/oauth2callback?code=a%2Bb&state=s%2F1&scope=x"
        ),
        "https://mcp.example.com/oauth2callback",
    )

    assert result.endswith("?code=a%2Bb&state=s%2F1&scope=x")


def test_a_custom_redirect_path_is_honoured():
    result = _authorization_response_url(
        _request(TLS_TERMINATED), "https://mcp.example.com/custom/cb"
    )

    assert result == "https://mcp.example.com/custom/cb?state=abc&code=xyz"


def test_loopback_development_is_unchanged():
    """The local case must produce exactly what `str(request.url)` produced."""
    local = "http://localhost:8000/oauth2callback?state=abc&code=xyz"

    result = _authorization_response_url(
        _request(local), "http://localhost:8000/oauth2callback"
    )

    assert result == local


def test_a_callback_with_no_query_does_not_grow_one():
    result = _authorization_response_url(
        _request("http://mcp.example.com/oauth2callback"),
        "https://mcp.example.com/oauth2callback",
    )

    assert result == "https://mcp.example.com/oauth2callback"


def test_oauthlib_accepts_the_rebuilt_url_and_rejects_the_proxied_one(https_required):
    """The point of the helper, against the real parser rather than a stub."""
    from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

    with pytest.raises(InsecureTransportError):
        parse_authorization_code_response(TLS_TERMINATED, state="abc")

    rebuilt = _authorization_response_url(
        _request(TLS_TERMINATED), "https://mcp.example.com/oauth2callback"
    )
    parsed = parse_authorization_code_response(rebuilt, state="abc")

    assert parsed["code"] == "xyz"


def test_a_public_http_redirect_uri_is_still_refused(https_required):
    """The other half of the fix, and the one worth guarding hardest.

    The helper must not become a blanket bypass. It upgrades nothing: it
    copies whatever scheme the configured redirect URI has, so a deployment
    that genuinely serves plain HTTP on a public name still hits oauthlib's
    transport check. If a future refactor made this pass, the HTTPS
    requirement would be unreachable for every deployment rather than
    satisfied by one.
    """
    from oauthlib.oauth2.rfc6749.parameters import parse_authorization_code_response

    rebuilt = _authorization_response_url(
        _request(TLS_TERMINATED), "http://mcp.example.com/oauth2callback"
    )

    assert rebuilt.startswith("http://")
    with pytest.raises(InsecureTransportError):
        parse_authorization_code_response(rebuilt, state="abc")


def test_the_scheme_is_copied_never_upgraded(https_required):
    """No implicit https:// promotion anywhere in the helper."""
    for redirect_uri, expected_scheme in (
        ("https://mcp.example.com/oauth2callback", "https"),
        ("http://mcp.example.com/oauth2callback", "http"),
        ("http://localhost:8000/oauth2callback", "http"),
    ):
        rebuilt = _authorization_response_url(_request(TLS_TERMINATED), redirect_uri)
        assert rebuilt.split("://", 1)[0] == expected_scheme
