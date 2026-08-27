import importlib
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


def test_well_known_cache_control_middleware_rewrites_headers():
    from core.server import WellKnownCacheControlMiddleware, _compute_scope_fingerprint

    async def well_known_endpoint(request):
        response = Response("ok")
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.set_cookie("a", "1")
        response.set_cookie("b", "2")
        return response

    async def regular_endpoint(request):
        response = Response("ok")
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    app = Starlette(
        routes=[
            Route("/.well-known/oauth-authorization-server", well_known_endpoint),
            Route("/.well-known/oauth-authorization-server-extra", regular_endpoint),
            Route("/health", regular_endpoint),
        ],
        middleware=[Middleware(WellKnownCacheControlMiddleware)],
    )
    client = TestClient(app)

    well_known = client.get("/.well-known/oauth-authorization-server")
    assert well_known.status_code == 200
    assert well_known.headers["cache-control"] == "no-store, must-revalidate"
    assert well_known.headers["etag"] == f'"{_compute_scope_fingerprint()}"'
    assert sorted(well_known.headers.get_list("set-cookie")) == sorted(
        ["a=1; Path=/; SameSite=lax", "b=2; Path=/; SameSite=lax"]
    )

    regular = client.get("/health")
    assert regular.status_code == 200
    assert regular.headers["cache-control"] == "public, max-age=3600"
    assert "etag" not in regular.headers

    extra = client.get("/.well-known/oauth-authorization-server-extra")
    assert extra.status_code == 200
    assert extra.headers["cache-control"] == "public, max-age=3600"
    assert "etag" not in extra.headers


def test_origin_validation_rejects_untrusted_browser_origin(monkeypatch):
    from core.server import OriginValidationMiddleware

    # Legacy HTTP mode. The same-origin route consults this now, and the stub
    # config below does not implement it.
    monkeypatch.setattr("core.server.is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            get_allowed_origins=lambda: ["http://localhost:8000"],
            external_url=None,
        ),
    )

    async def endpoint(request):
        return Response("ok")

    app = Starlette(
        routes=[Route("/health", endpoint)],
        middleware=[Middleware(OriginValidationMiddleware)],
    )
    client = TestClient(app)

    # Host is checked on every request now, so each of these has to name one
    # the deployment answers on before the Origin half is reached at all.
    assert (
        client.get(
            "/health",
            headers={"Origin": "http://evil.test", "Host": "localhost:8000"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/health",
            headers={"Origin": "http://localhost:5173", "Host": "localhost:8000"},
        ).status_code
        == 200
    )
    assert client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200


def test_origin_validation_allows_configured_external_origin(monkeypatch):
    from core.server import OriginValidationMiddleware

    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            get_allowed_origins=lambda: ["http://localhost:8000"],
            external_url="https://workspace.example.com/mcp",
        ),
    )

    async def endpoint(request):
        return Response("ok")

    app = Starlette(
        routes=[Route("/health", endpoint)],
        middleware=[Middleware(OriginValidationMiddleware)],
    )
    client = TestClient(app)

    response = client.get(
        "/health",
        headers={
            "Origin": "https://workspace.example.com",
            "Host": "workspace.example.com",
        },
    )
    assert response.status_code == 200


def test_origin_validation_trusts_any_vscode_webview_origin(monkeypatch):
    from core.server import OriginValidationMiddleware

    # VS Code assigns a fresh, random GUID authority to every webview, so its
    # origin can never be enumerated in an allowlist. The scheme is the trust
    # boundary; any vscode-webview origin must be accepted regardless of host.
    monkeypatch.setattr("core.server.is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            get_allowed_origins=lambda: ["http://localhost:8000"],
            external_url=None,
        ),
    )

    async def endpoint(request):
        return Response("ok")

    app = Starlette(
        routes=[Route("/health", endpoint)],
        middleware=[Middleware(OriginValidationMiddleware)],
    )
    client = TestClient(app)

    # Real-world VS Code webview origins carry a unique per-session GUID host.
    for host in (
        "1a2b3c4d-5e6f-7a8b-9c0d-1234567890ab",
        "ffffffff-0000-1111-2222-333344445555",
        "publisher.extension",
    ):
        assert (
            client.get(
                "/health",
                headers={
                    "Origin": f"vscode-webview://{host}",
                    "Host": "localhost:8000",
                },
            ).status_code
            == 200
        )
    # A genuine browser web origin that is not configured is still rejected.
    assert (
        client.get(
            "/health",
            headers={"Origin": "https://evil.test", "Host": "localhost:8000"},
        ).status_code
        == 403
    )


def test_origin_validation_allows_same_origin_request(monkeypatch):
    from core.server import OriginValidationMiddleware

    # The OAuth proxy consent form posts to itself (action=""), so the request is
    # always same-origin with the host that served the page. That host has to be
    # one the deployment declared — WORKSPACE_EXTERNAL_URL here — but it need not
    # have been enumerated as an ORIGIN, which is what this route is for: the
    # form is served on a port the allowlist never spells out.
    monkeypatch.setattr("core.server.is_oauth21_enabled", lambda: True)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            get_allowed_origins=lambda: ["http://localhost:8000"],
            external_url="https://app.example.com",
        ),
    )

    async def endpoint(request):
        return Response("ok")

    app = Starlette(
        routes=[Route("/consent", endpoint, methods=["POST"])],
        middleware=[Middleware(OriginValidationMiddleware)],
    )
    client = TestClient(app)

    # Same-origin consent POST on the declared host, at an unenumerated port.
    same_origin = client.post(
        "/consent",
        headers={
            "Origin": "https://app.example.com:8443",
            "Host": "app.example.com:8443",
        },
    )
    assert same_origin.status_code == 200

    # A cross-origin request to that same host is still rejected.
    cross_origin = client.post(
        "/consent",
        headers={
            "Origin": "https://evil.test",
            "Host": "app.example.com",
        },
    )
    assert cross_origin.status_code == 403

    # And the host the deployment never declared is refused outright, whether or
    # not the Origin agrees with it — OAuth 2.1 mode buys it nothing.
    undeclared = client.post(
        "/consent",
        headers={
            "Origin": "https://other.example.com",
            "Host": "other.example.com",
        },
    )
    assert undeclared.status_code == 403


def test_configured_server_applies_no_cache_to_served_oauth_discovery_routes(
    monkeypatch,
):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "dummy-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "dummy-secret")
    monkeypatch.setenv("WORKSPACE_MCP_BASE_URI", "http://localhost")
    monkeypatch.setenv("WORKSPACE_MCP_PORT", "8000")
    monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "false")

    import core.server as core_server
    from auth.oauth_config import reload_oauth_config

    reload_oauth_config()
    core_server = importlib.reload(core_server)
    core_server.set_transport_mode("streamable-http")
    core_server.configure_server_for_http()

    app = core_server.server.http_app(transport="streamable-http", path="/mcp")
    # base_url drives the Host header, which is now checked on every request:
    # TestClient's default "testserver" is not a hostname this deployment
    # answers on, and would be refused before reaching any route.
    client = TestClient(app, base_url="http://localhost:8000")

    authorization_server = client.get("/.well-known/oauth-authorization-server")
    assert authorization_server.status_code == 200
    assert authorization_server.headers["cache-control"] == "no-store, must-revalidate"
    assert authorization_server.headers["etag"].startswith('"')
    assert authorization_server.headers["etag"].endswith('"')

    protected_resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert protected_resource.status_code == 200
    assert protected_resource.headers["cache-control"] == "no-store, must-revalidate"
    assert protected_resource.headers["etag"].startswith('"')
    assert protected_resource.headers["etag"].endswith('"')

    # Ensure we did not create a shadow route at the wrong path.
    wrong_path = client.get("/.well-known/oauth-protected-resource")
    assert wrong_path.status_code == 404


def test_external_oauth_metadata_matches_mcp_resource_and_challenge(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "dummy-client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "dummy-secret")
    monkeypatch.setenv("WORKSPACE_MCP_BASE_URI", "http://localhost")
    monkeypatch.setenv("WORKSPACE_MCP_PORT", "8000")
    monkeypatch.setenv("WORKSPACE_EXTERNAL_URL", "https://workspace.example.com")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")
    monkeypatch.setenv("WORKSPACE_MCP_STATELESS_MODE", "true")

    import core.server as core_server
    from auth.oauth_config import reload_oauth_config

    reload_oauth_config()
    core_server = importlib.reload(core_server)
    core_server.set_transport_mode("streamable-http")
    core_server.configure_server_for_http()

    app = core_server.server.http_app(transport="streamable-http", path="/mcp")
    # See the note above: the Host has to be one this deployment answers on.
    client = TestClient(app, base_url="https://workspace.example.com")

    protected_resource = client.get("/.well-known/oauth-protected-resource/mcp")
    assert protected_resource.status_code == 200
    assert protected_resource.json()["resource"] == "https://workspace.example.com/mcp"

    wrong_path = client.get("/.well-known/oauth-protected-resource")
    assert wrong_path.status_code == 404

    challenge = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert challenge.status_code == 401
    assert (
        'resource_metadata="https://workspace.example.com/'
        '.well-known/oauth-protected-resource/mcp"'
        in challenge.headers["www-authenticate"]
    )
