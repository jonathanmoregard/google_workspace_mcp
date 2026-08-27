"""The Host header is the origin decision; the Origin header only narrows it.

``OriginValidationMiddleware`` exists to stop DNS rebinding. Rebinding works by
serving the attacker's page from a name that later resolves to this server, so
the browser sends the attacker's hostname in BOTH ``Origin`` and ``Host``. A
check that only asks "do these two match?" therefore admits the exact request it
was written to reject.

Worse, an Origin-GATED check never runs on most of what rebinding produces. Per
the Fetch standard a browser appends ``Origin`` only for methods other than GET
and HEAD, and a rebound page's requests are same-origin as far as the browser is
concerned — so its GETs arrive with no ``Origin`` at all. ``Host`` is the only
header present on every request, which is why it is checked on every request.

These tests pin all three halves: the rebinding request is refused whether or
not it carries an Origin, the legitimate same-origin case still works, and
hostile header values produce a 403 rather than a traceback.
"""

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from core.server import OriginValidationMiddleware


def _configure(monkeypatch, *, allowed_origins, external_url=None, oauth21=False):
    monkeypatch.setattr("core.server.is_oauth21_enabled", lambda: oauth21)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            get_allowed_origins=lambda: list(allowed_origins),
            external_url=external_url,
        ),
    )


def _client():
    async def endpoint(request):
        return Response("ok")

    return TestClient(
        Starlette(
            routes=[Route("/health", endpoint)],
            middleware=[Middleware(OriginValidationMiddleware)],
        )
    )


def test_a_dns_rebinding_request_is_refused(monkeypatch):
    """Attacker hostname in both headers — the authorities match perfectly."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    response = _client().get(
        "/health", headers={"Origin": "http://evil.test", "Host": "evil.test"}
    )

    assert response.status_code == 403


def test_rebinding_is_refused_even_on_a_matching_explicit_port(monkeypatch):
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    response = _client().get(
        "/health",
        headers={"Origin": "http://evil.test:8000", "Host": "evil.test:8000"},
    )

    assert response.status_code == 403


def test_a_configured_host_still_passes_the_same_origin_route(monkeypatch):
    """The case the check exists for must keep working.

    The origin is not in the allowlist as an exact authority — a different
    port — so it can only be admitted by the same-origin route, and it is,
    because the Host names a hostname this deployment is configured to answer
    on.
    """
    _configure(
        monkeypatch,
        allowed_origins=["https://workspace.example.com"],
        external_url=None,
    )

    response = _client().get(
        "/health",
        headers={
            "Origin": "https://workspace.example.com:8443",
            "Host": "workspace.example.com:8443",
        },
    )

    assert response.status_code == 200


def test_the_external_url_hostname_is_configured(monkeypatch):
    _configure(
        monkeypatch,
        allowed_origins=[],
        external_url="https://mcp.example.com/mcp",
    )

    response = _client().get(
        "/health",
        headers={
            "Origin": "https://mcp.example.com:8443",
            "Host": "mcp.example.com:8443",
        },
    )

    assert response.status_code == 200


def test_a_request_with_no_origin_header_still_has_its_host_checked(monkeypatch):
    """The rebinding case an Origin-gated check cannot see.

    A rebound page's GETs are same-origin to the browser, so they carry no
    Origin. Only the Host names the attacker.
    """
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    assert _client().get("/health", headers={"Host": "evil.test"}).status_code == 403


def test_a_request_with_no_origin_header_passes_on_a_configured_host(monkeypatch):
    """Non-browser clients send no Origin, and must keep working."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    assert (
        _client().get("/health", headers={"Host": "localhost:8000"}).status_code == 200
    )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_is_configured_without_being_enumerated(monkeypatch, host):
    """Exercises _configured_hostnames in the DECIDING position.

    With an Origin present this passes through ``_is_origin_allowed``'s loopback
    short-circuit instead, which is what made an earlier version of this test
    vacuous: it never reached the loopback entries it names. Omitting the Origin
    leaves the Host allowlist as the only thing that can admit the request.
    """
    _configure(monkeypatch, allowed_origins=[], external_url=None)

    response = _client().get("/health", headers={"Host": f"{host}:9999"})

    assert response.status_code == 200


def test_a_mismatched_host_and_origin_is_still_refused(monkeypatch):
    """The original half of the check has not been dropped."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    response = _client().get(
        "/health", headers={"Origin": "http://evil.test", "Host": "localhost:8000"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("oauth21", [True, False])
def test_an_unconfigured_host_is_refused_in_either_mode(monkeypatch, oauth21):
    """OAuth 2.1 mode no longer buys an unconfigured Host a pass.

    The carve-out was justified by the consent form needing a host a deployment
    may never have enumerated, and by every MCP request carrying a bearer token
    a rebound page does not have. The second half is false for the OAuth proxy's
    own unauthenticated endpoints — /oauth2/register, /oauth2/authorize and the
    consent POST are exactly what a rebound page can reach.

    The first half does not hold either: core.server hands FastMCP
    ``get_oauth_base_url()`` as both base_url and resource_server_url, and
    oauth_config always puts that same value into get_allowed_origins(). A
    deployment whose OAuth flow works therefore always has its serving host
    enumerated, so the carve-out only ever admitted hosts nothing needs.
    """
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"], oauth21=oauth21)

    response = _client().post(
        "/health",
        headers={"Origin": "https://app.example.com", "Host": "app.example.com"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/oauth2/register", "/oauth2/authorize"])
def test_a_rebound_page_cannot_reach_the_unauthenticated_oauth_endpoints(
    monkeypatch, path
):
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"], oauth21=True)

    response = _client().post(
        path, headers={"Origin": "http://evil.test", "Host": "evil.test"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://[evil", "Host": "localhost:8000"},
        {"Origin": "http://evil.test", "Host": "[evil"},
        {"Host": "[evil"},
        {"Origin": "http://x:notaport", "Host": "localhost:8000"},
        {"Origin": "http://evil.test", "Host": "localhost:notaport"},
    ],
)
def test_a_hostile_header_is_refused_not_crashed(monkeypatch, headers):
    """``urlparse`` is lazy — it raises on .hostname, not on parse.

    Every such read happens behind ``_parse_url`` now, so hostile input fails
    closed with a 403 instead of escaping the middleware as a 500 traceback.
    """
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    assert _client().get("/health", headers=headers).status_code == 403


def test_an_allowlist_entry_may_spell_the_default_port(monkeypatch):
    """A browser omits :443, so an entry that writes it must still match."""
    _configure(monkeypatch, allowed_origins=["https://app.example.com:443"])

    response = _client().get(
        "/health",
        headers={"Origin": "https://app.example.com", "Host": "localhost:8000"},
    )

    assert response.status_code == 200


def test_an_origin_may_spell_the_default_port(monkeypatch):
    """And the same in reverse, for a client that does write it out."""
    _configure(monkeypatch, allowed_origins=["https://app.example.com"])

    response = _client().get(
        "/health",
        headers={"Origin": "https://app.example.com:443", "Host": "localhost:8000"},
    )

    assert response.status_code == 200


def test_a_non_default_port_still_has_to_match(monkeypatch):
    _configure(monkeypatch, allowed_origins=["https://app.example.com:8443"])

    response = _client().get(
        "/health",
        headers={"Origin": "https://app.example.com", "Host": "localhost:8000"},
    )

    assert response.status_code == 403
