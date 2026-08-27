"""The Host header is part of the origin decision, not just the Origin header.

``OriginValidationMiddleware`` exists to stop DNS rebinding. Rebinding works by
serving the attacker's page from a name that later resolves to this server, so
the browser sends the attacker's hostname in BOTH ``Origin`` and ``Host``. A
check that only asks "do these two match?" therefore admits the exact request it
was written to reject — and does so in legacy HTTP mode, which has no MCP-level
auth provider behind it.

These tests pin both halves: the rebinding request is refused, and the
legitimate same-origin case that the check exists to serve still works.
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


def test_a_request_with_no_origin_header_is_untouched(monkeypatch):
    """Non-browser clients send no Origin; this middleware is not for them."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    assert _client().get("/health").status_code == 200


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_is_configured_without_being_enumerated(monkeypatch, host):
    _configure(monkeypatch, allowed_origins=[], external_url=None)

    response = _client().get(
        "/health",
        headers={"Origin": f"http://{host}:9999", "Host": f"{host}:9999"},
    )

    assert response.status_code == 200


def test_a_mismatched_host_and_origin_is_still_refused(monkeypatch):
    """The original half of the check has not been dropped."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"])

    response = _client().get(
        "/health", headers={"Origin": "http://evil.test", "Host": "localhost:8000"}
    )

    assert response.status_code == 403


def test_an_unconfigured_host_is_allowed_in_oauth21_mode(monkeypatch):
    """The consent-form case this escape hatch exists for.

    FastMCP's OAuth proxy renders a form that posts to itself, on whatever host
    served it, which a deployment may never have enumerated. In OAuth 2.1 mode
    every MCP request also carries a bearer token a rebound page does not have.
    """
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"], oauth21=True)

    response = _client().post(
        "/health",
        headers={"Origin": "https://app.example.com", "Host": "app.example.com"},
    )

    assert response.status_code in (200, 405)


def test_the_same_unconfigured_host_is_refused_in_legacy_mode(monkeypatch):
    """The pair to the test above: same request, no protocol auth behind it."""
    _configure(monkeypatch, allowed_origins=["http://localhost:8000"], oauth21=False)

    response = _client().post(
        "/health",
        headers={"Origin": "https://app.example.com", "Host": "app.example.com"},
    )

    assert response.status_code == 403
