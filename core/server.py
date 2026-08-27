# ruff: noqa: E402
# Startup warning filters must be installed before importing FastMCP/Authlib dependencies.
import asyncio
import hashlib
import logging
import os
from ipaddress import ip_address
from typing import List, Optional
from importlib import metadata
from urllib.parse import urlparse, ParseResult

from core.warning_filters import install_startup_warning_filters

install_startup_warning_filters()

from auth.auth_info_middleware import AuthInfoMiddleware
from auth.google_auth import handle_auth_callback, start_auth_flow, check_client_secrets
from auth.gateway_identity import get_verified_gateway_principal
from auth.mcp_session_middleware import MCPSessionMiddleware
from auth.oauth21_session_store import set_auth_provider
from auth.oauth_config import (
    is_oauth21_enabled,
    is_external_oauth21_provider,
    get_oauth_config,
    is_trust_gateway_identity,
)
from auth.oauth_responses import (
    create_error_response,
    create_success_response,
    create_server_error_response,
)
from auth.scopes import PROTOCOL_AUTH_SCOPES, SCOPES, get_current_scopes  # noqa
from core.account_directory import (
    build_server_instructions,
    render_account_report,
    resolve_default_account,
)
from core.env_flags import parse_bool_env
from core.config import (
    USER_GOOGLE_EMAIL,
    get_transport_mode,
    set_transport_mode as _set_transport_mode,
    get_oauth_redirect_uri as get_oauth_redirect_uri_for_current_mode,
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.types import ToolAnnotations, Icon
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.types import ASGIApp, Scope, Receive, Send

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_auth_provider: Optional[GoogleProvider] = None
_legacy_callback_registered = False

session_middleware = Middleware(MCPSessionMiddleware)


# Schemes whose origins are trusted by the scheme alone. The Origin header is a
# browser-forbidden header, so a remote web page (the DNS-rebinding threat this
# middleware defends against) cannot forge one of these — only the local IDE
# runtime emits them. VS Code in particular assigns a fresh, unpredictable host
# (a per-session GUID) to every webview, so its origin can never be enumerated in
# an allowlist; the scheme itself is the trust boundary.
TRUSTED_ORIGIN_SCHEMES = frozenset({"vscode-webview"})


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Default authority ports per scheme, used to compare an Origin against the Host
# header that received the request (a same-origin check).
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _parse_url(value: str) -> Optional[ParseResult]:
    """``urlparse``, with hostile input yielding None instead of ValueError.

    ``urlparse`` is lazy: it accepts ``http://[evil`` without complaint and
    raises only when ``.hostname`` or ``.port`` is read. Reading both here is
    what lets every caller treat the result as safe, so an attacker-supplied
    ``Origin`` or ``Host`` ends in a 403 rather than a 500 traceback.
    """
    try:
        parsed = urlparse(value)
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    return parsed


def _normalize_parsed(parsed: ParseResult) -> Optional[str]:
    """Reduce a parsed URL to a comparable authority. Takes a _parse_url result."""
    if not parsed.scheme:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    port = parsed.port
    # A browser never spells the default port in an Origin, so an allowlist
    # entry that does — ``https://x:443`` — would otherwise be permanently
    # inert, and a legitimate cross-origin client refused for writing it out.
    if port is not None and port == _DEFAULT_PORTS.get(parsed.scheme):
        port = None
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme}://{netloc}"


def _normalize_origin(origin: str) -> Optional[str]:
    parsed = _parse_url(origin)
    return _normalize_parsed(parsed) if parsed is not None else None


def _get_allowed_http_origins() -> set[str]:
    from auth.oauth_config import get_oauth_config

    config = get_oauth_config()
    origins = set()
    for origin in config.get_allowed_origins():
        normalized = _normalize_origin(origin)
        if normalized:
            origins.add(normalized)
    if config.external_url:
        normalized = _normalize_origin(config.external_url)
        if normalized:
            origins.add(normalized)
    return origins


def _is_origin_allowed(origin: str) -> bool:
    parsed = _parse_url(origin)
    if parsed is None:
        return False
    if parsed.scheme in TRUSTED_ORIGIN_SCHEMES:
        return True
    if parsed.hostname in _LOOPBACK_HOSTS:
        return True
    normalized = _normalize_parsed(parsed)
    if not normalized:
        return False
    return normalized in _get_allowed_http_origins()


def _configured_hostnames() -> set[str]:
    """DNS names this deployment is configured to answer on.

    Loopback, plus the host of every allowed origin and of the external URL.
    This is the allowlist a *name* in the Host header is checked against — an
    IP literal never reaches it, see :func:`_is_configured_host`.
    """
    # Local import, matching _get_allowed_http_origins: the module-level name
    # is bound at import time, and the config is reloaded after .env is read.
    from auth.oauth_config import get_oauth_config

    hostnames = {host.lower() for host in _LOOPBACK_HOSTS}
    config = get_oauth_config()
    # Deliberately NOT get_allowed_origins(): that answers "which browser
    # origins may talk to us", and hardcodes https://vscode.dev and
    # https://github.dev. Those are other people's hosts, never names this
    # deployment serves on, and harvesting them here put them in the
    # anti-rebinding Host allowlist — `Host: vscode.dev` passed on every
    # deployment. This answers the different question "which names do we
    # answer on", so it takes only the operator-supplied origins.
    candidates = [config.base_url]
    if config.external_url:
        candidates.append(config.external_url)
    candidates.extend(config.get_custom_allowed_origins())
    for candidate in candidates:
        parsed = _parse_url(candidate)
        if parsed is None:
            continue
        hostname = parsed.hostname
        if hostname:
            hostnames.add(hostname.lower())
    return hostnames


def _is_configured_host(host_header: Optional[str]) -> bool:
    """Is this Host header one the deployment may answer? Absent or malformed: no.

    An IP literal is allowed unconditionally; a DNS name must be one this
    deployment is configured to answer on.

    This is the check that stops DNS rebinding, and it has to run on EVERY
    request. Rebinding serves the attacker's page from a name that later
    resolves to this server, so the browser treats its requests back here as
    same-origin — and per the Fetch standard a browser appends ``Origin`` only
    for methods other than GET and HEAD. A rebound page's GETs therefore carry
    no ``Origin`` at all, which is precisely the shape an Origin-gated check
    never inspects. ``Host`` is present on every HTTP/1.1 request and on every
    HTTP/2 ``:authority``, so it is the only header that can carry this.

    The IP-literal carve-out is not a convenience: **rebinding requires a
    name.** The attacker's only lever is a DNS record, and the browser writes
    the authority it was asked for into ``Host``, so ``Host: 10.42.0.7:8000``
    means no name was resolved and nothing was rebound. Insisting on the
    allowlist there refuses only requests that were never the threat — a
    kubelet dialling the pod IP for ``/health``, a LAN client reaching a server
    bound to ``0.0.0.0`` by address. Vite reached the same conclusion and
    allows every IP-literal Host by default (``server.allowedHosts``); Jupyter
    allows only loopback literals, which is the same idea drawn tighter and is
    the friction that pushes its users to switch the check off entirely.

    It takes every IP, not just loopback and RFC1918: IPv6 clusters hand pods
    globally routable addresses. What it does NOT do is condition enforcement
    on the bind address — reachability is not name authorization, and the bind
    here defaults to ``0.0.0.0`` (``main.py``), so that design would ship with
    the guard off.
    """
    if not host_header:
        return False
    parsed = _parse_url(f"//{host_header}")
    if parsed is None or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    try:
        # urlparse has already stripped the brackets from an IPv6 authority,
        # and ip_address keeps a zone id (fe80::1%eth0) — still a literal, so
        # still not something a name could have been rebound to. It is strict
        # about everything else: 2130706433, 0x7f000001 and 010.0.0.1 all raise
        # and fall through to the allowlist as the names they are, which is
        # what keeps the carve-out from becoming a spelling contest.
        ip_address(hostname)
        return True
    except ValueError:
        pass
    return hostname in _configured_hostnames()


def _is_same_origin_as_host(origin: str, host_header: Optional[str]) -> bool:
    """Return True when the Origin's authority IS the Host that received it.

    This admits the server's own page calling back on a host that is served but
    was never enumerated as an *origin* — the OAuth consent form posts to
    itself, on whatever host served it, and a deployment may only ever have set
    ``WORKSPACE_EXTERNAL_URL``.

    It carries no part of the rebinding defence. A rebound page sends the
    attacker's name in BOTH headers, so their authorities match perfectly and
    this test alone would admit the attack it was written to reject. What
    refuses that request is :func:`_is_configured_host`, which the middleware
    has already applied to every request by the time this runs.
    """
    if not host_header:
        return False
    parsed = _parse_url(origin)
    if parsed is None or not parsed.hostname:
        return False
    host = _parse_url(f"//{host_header}")
    if host is None or not host.hostname:
        return False
    origin_port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
    host_port = host.port or _DEFAULT_PORTS.get(parsed.scheme)
    return parsed.hostname == host.hostname and origin_port == host_port


def _sole_header(
    raw_headers: List[tuple[bytes, bytes]], name: bytes
) -> tuple[Optional[str], bool]:
    """Return ``(first value, was it repeated)`` for one raw ASGI header.

    FIRST, not last, because that is what Starlette's ``request.headers.get``
    returns — the guard and the application must read the same bytes. The
    repeat flag is reported rather than resolved: see the caller.
    """
    found = [value for key, value in raw_headers if key == name]
    if not found:
        return None, False
    return found[0].decode("latin-1"), len(found) > 1


def _refuse_request(
    origin: Optional[str], host_header: Optional[str]
) -> Optional[tuple[str, str]]:
    """Return ``(client_error, operator_log)`` when the request must be refused.

    Two checks, and this used to be only the second:

    * the ``Host`` must be an IP literal, or a DNS name this deployment answers
      on, on every request — see :func:`_is_configured_host` for why an
      Origin-gated check cannot cover the requests DNS rebinding actually
      produces, and why an IP literal is never one of them;
    * an ``Origin``, when present, must be allowlisted or be the Host itself.

    The two messages differ on purpose. The refusal an operator is most likely
    to hit is the first one, from a reverse proxy passing through a hostname
    the server was never told about, and "Origin not allowed" gave them nothing
    to act on — the request they are debugging may carry no Origin at all.
    """
    if not _is_configured_host(host_header):
        return (
            "Host not allowed",
            f"Rejected HTTP request: Host {host_header!r} is not a name this "
            f"deployment is configured to answer on. If this deployment really "
            f"serves that name, add it to OAUTH_ALLOWED_ORIGINS or set "
            f"WORKSPACE_EXTERNAL_URL.",
        )
    if origin is None:
        return None
    if _is_origin_allowed(origin) or _is_same_origin_as_host(origin, host_header):
        return None
    return (
        # !r on both, as above: these are attacker-controlled strings going into
        # a log line, and repr is what keeps a newline in one from forging the
        # next entry.
        "Origin not allowed",
        f"Rejected HTTP request from Origin: {origin!r} (Host: {host_header!r})",
    )


class OriginValidationMiddleware:
    """Reject HTTP requests this deployment is not configured to serve.

    Every request's ``Host`` must be an IP literal or a configured DNS name,
    and an ``Origin``, when present, must be allowlisted or be the Host itself.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw_headers = scope.get("headers") or []
            origin, origin_dup = _sole_header(raw_headers, b"origin")
            host_header, host_dup = _sole_header(raw_headers, b"host")
            if origin_dup or host_dup:
                # A repeated Origin or Host is never legitimate, and letting one
                # through is a full bypass rather than a nuisance: dict(headers)
                # keeps the LAST value while Starlette's request.headers.get
                # returns the FIRST, so the guard validated one name and the
                # application consumed another. Measured: Host: evil.test plus
                # Host: localhost:8000 was admitted as loopback and served to a
                # route that saw evil.test.
                logger.warning(
                    "Rejected HTTP request: repeated %s header",
                    "Origin" if origin_dup else "Host",
                )
                response = JSONResponse(
                    {"error": "Malformed request headers"}, status_code=400
                )
                await response(scope, receive, send)
                return
            refusal = _refuse_request(origin, host_header)
            if refusal:
                client_error, operator_log = refusal
                logger.warning("%s", operator_log)
                response = JSONResponse({"error": client_error}, status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


origin_validation_middleware = Middleware(OriginValidationMiddleware)


class WellKnownCacheControlMiddleware:
    """Force no-cache headers for OAuth well-known discovery endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_oauth_well_known = (
            path == "/.well-known/oauth-authorization-server"
            or path.startswith("/.well-known/oauth-authorization-server/")
            or path == "/.well-known/oauth-protected-resource"
            or path.startswith("/.well-known/oauth-protected-resource/")
        )
        if not is_oauth_well_known:
            await self.app(scope, receive, send)
            return

        async def send_with_no_cache_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["Cache-Control"] = "no-store, must-revalidate"
                headers["ETag"] = f'"{_compute_scope_fingerprint()}"'
            await send(message)

        await self.app(scope, receive, send_with_no_cache_headers)


well_known_cache_control_middleware = Middleware(WellKnownCacheControlMiddleware)


def _compute_scope_fingerprint() -> str:
    """Compute a short hash of the current scope configuration for cache-busting."""
    scopes_str = ",".join(sorted(get_current_scopes()))
    return hashlib.sha256(scopes_str.encode()).hexdigest()[:12]


# Custom FastMCP that adds secure middleware stack for OAuth 2.1
class SecureFastMCP(FastMCP):
    def http_app(self, **kwargs) -> "Starlette":
        """Override to add secure middleware stack for OAuth 2.1."""
        app = super().http_app(**kwargs)

        # Add middleware in order (first added = outermost layer)
        app.user_middleware.insert(0, well_known_cache_control_middleware)
        app.user_middleware.insert(1, origin_validation_middleware)

        # Session Management - extracts session info for MCP context
        app.user_middleware.insert(2, session_middleware)

        # Rebuild middleware stack
        app.middleware_stack = app.build_middleware_stack()
        logger.debug(
            "Added middleware stack: WellKnownCacheControl, OriginValidation, "
            "Session Management"
        )
        return app

    async def list_tools(self, *, run_middleware: bool = True):
        """Override to mark user_google_email as optional when USER_GOOGLE_EMAIL is set.

        In single-user / self-hosted mode the env var provides the default email, so
        callers (agents, MCP adapters) should not be required to supply it.  We patch
        the JSON schema returned by list_tools to remove 'user_google_email' from the
        ``required`` array and inject the env-var value as the ``default``.  The
        runtime still resolves the email correctly via the service decorator.
        """
        tools = list(await super().list_tools(run_middleware=run_middleware))
        if is_trust_gateway_identity():
            patched = []
            for tool in tools:
                if tool.name != "start_google_auth":
                    patched.append(tool)
                    continue
                schema = dict(tool.parameters)
                required = [
                    name
                    for name in schema.get("required", [])
                    if name != "user_google_email"
                ]
                properties = dict(schema.get("properties", {}))
                properties.pop("user_google_email", None)
                schema.update(required=required, properties=properties)
                patched.append(tool.model_copy(update={"parameters": schema}))
            return patched
        # resolve_default_account(), not the import-frozen core.config constant:
        # main.py imports this module before load_dotenv(), so a default living
        # only in .env is invisible to that constant. The instructions were
        # moved onto the live value; leaving the SCHEMA on the frozen one would
        # tell the agent "do not ask the user for their email address" while
        # still marking the parameter required and never injecting it.
        default_account = resolve_default_account()
        if not default_account or is_oauth21_enabled():
            return tools
        patched = []
        for tool in tools:
            schema = dict(tool.parameters)
            required = list(schema.get("required", []))
            if "user_google_email" in required:
                required = [r for r in required if r != "user_google_email"]
                props = {k: dict(v) for k, v in schema.get("properties", {}).items()}
                if "user_google_email" in props:
                    props["user_google_email"]["default"] = default_account
                schema = dict(schema, required=required, properties=props)
                patched.append(tool.model_copy(update={"parameters": schema}))
            else:
                patched.append(tool)
        return patched

    def _tool_takes_user_email(self, name: str) -> bool:
        """Whether the registered tool actually has a user_google_email parameter.

        Injecting the default into a tool that does not accept it is a pydantic
        ``unexpected_keyword_argument`` error, so account-level tools that take no
        arguments at all (``list_google_accounts``) would be uncallable whenever
        USER_GOOGLE_EMAIL is configured. Unknown tools answer True so that an
        unregistered name still fails the way FastMCP makes it fail.
        """
        from core.tool_registry import get_tool_components

        try:
            tool = get_tool_components(self).get(name)
            if tool is None:
                return True
            properties = (getattr(tool, "parameters", None) or {}).get("properties")
        except Exception:  # pragma: no cover - defensive
            return True
        if not isinstance(properties, dict):
            return True
        return "user_google_email" in properties

    async def call_tool(self, name: str, arguments: Optional[dict], *args, **kwargs):
        """Inject user_google_email before pydantic validates the call arguments.

        When USER_GOOGLE_EMAIL is configured and OAuth 2.1 is not active, callers
        (agents, adapters) are allowed to omit user_google_email.  FastMCP validates
        arguments against the function signature BEFORE calling the tool, so we must
        inject the default BEFORE that validation step.
        """
        arguments = arguments or {}
        if is_trust_gateway_identity():
            # The verified gateway principal is authoritative for every tool, and the
            # parameter is gone from tool signatures. Drop any caller-supplied email
            # (older clients may have the pre-gateway schema cached) instead of letting
            # it fail signature validation, and never inject USER_GOOGLE_EMAIL.
            arguments = {
                key: value
                for key, value in arguments.items()
                if key != "user_google_email"
            }
        elif "user_google_email" not in arguments and self._tool_takes_user_email(name):
            # Live value, for the same reason list_tools patches the schema from
            # one: the import-time constant cannot see a default set in .env,
            # and a schema that says "optional, defaults to X" must be backed by
            # an injection that actually supplies X.
            default_account = resolve_default_account()
            if default_account:
                arguments = {**arguments, "user_google_email": default_account}
        return await super().call_tool(name, arguments, *args, **kwargs)


# Build server instructions with user email context for single-user mode.
# Skipped in trusted-gateway mode: the verified principal supersedes the configured
# default, and user_google_email is no longer a tool parameter clients can pass.
#
# Deliberately built WITHOUT enumerating the credential store. main.py imports
# this module before it calls load_dotenv(), so anything configured only in .env
# — the identity mode and the credentials directory included — is invisible here.
# The value below therefore names nothing but the configured default, which is
# safe under every configuration; refresh_server_instructions() rebuilds it once
# configuration is final and is what may name other accounts. See
# core/account_directory.py.
_server_instructions = build_server_instructions(
    USER_GOOGLE_EMAIL, enumerate_store=False
)
if _server_instructions:
    logger.info(f"Server instructions configured for user: {USER_GOOGLE_EMAIL}")

# Branding for the OAuth consent page: FastMCP's OAuth proxy renders the server's
# name / icon / website on the consent screen (auth/oauth_config reads the env vars).
_brand_config = get_oauth_config()
_brand_icons = (
    [Icon(src=_brand_config.brand_icon_url)] if _brand_config.brand_icon_url else None
)

server = SecureFastMCP(
    name=_brand_config.brand_name or "google_workspace",
    auth=None,
    instructions=_server_instructions,
    website_url=_brand_config.brand_website_url,
    icons=_brand_icons,
)

# Add the AuthInfo middleware to inject authentication into FastMCP context
auth_info_middleware = AuthInfoMiddleware()
server.add_middleware(auth_info_middleware)


def refresh_server_instructions() -> Optional[str]:
    """Rebuild the ``instructions`` string from the environment as it stands now.

    Entry points import this module and only afterwards load ``.env`` and
    reload the OAuth config, so the string built at import cannot see any
    setting that lives only in ``.env``. Each entry point calls this once
    configuration is final; the effective default account is re-derived too,
    because ``core.config.USER_GOOGLE_EMAIL`` froze at import as well.

    There is no window in which the import-time value can be served: FastMCP
    reads ``instructions`` when it answers ``initialize``, which cannot happen
    before ``server.run()`` opens a transport, and every caller of this function
    runs before that.
    """
    instructions = build_server_instructions(resolve_default_account())
    if instructions != server.instructions:
        logger.debug(
            "Server instructions rebuilt after configuration was loaded (was %s, "
            "now %s characters).",
            "empty" if not server.instructions else len(server.instructions),
            "empty" if not instructions else len(instructions),
        )
    server.instructions = instructions
    return instructions


#: The one shared parser (``core.env_flags``), kept under the historical name
#: so callers in this module read unchanged.
_parse_bool_env = parse_bool_env


def _parse_allowed_redirect_uris(value: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated list of OAuth client redirect URIs.

    Returns a list of non-empty, trimmed URIs, or None when the input is
    empty/None. Returning None preserves FastMCP's default behaviour of
    accepting any client-supplied redirect URI during DCR — callers that
    want to lock down registration must supply a non-empty list.

    Patterns supported by FastMCP's matcher (see
    ``fastmcp.server.auth.redirect_validation``) include ``*`` for port
    and path globs (e.g. ``http://localhost:*/callback``) and ``*.example.com``
    for subdomain wildcards.
    """
    if not value:
        return None
    uris = [u.strip() for u in value.split(",") if u.strip()]
    return uris or None


def set_transport_mode(mode: str):
    """Sets the transport mode for the server."""
    _set_transport_mode(mode)
    # Debug level: the startup banner already shows the active transport.
    logger.debug(f"Transport: {mode}")


def _ensure_legacy_callback_route() -> None:
    global _legacy_callback_registered
    if _legacy_callback_registered:
        return
    server.custom_route("/oauth2callback", methods=["GET"])(legacy_oauth2_callback)
    _legacy_callback_registered = True


def configure_server_for_http():
    """
    Configures the authentication provider for HTTP transport.
    This must be called BEFORE server.run().
    """
    global _auth_provider

    transport_mode = get_transport_mode()

    if transport_mode != "streamable-http":
        return

    # Use centralized OAuth configuration
    from auth.oauth_config import get_oauth_config

    config = get_oauth_config()

    # Check if OAuth 2.1 is enabled via centralized config
    oauth21_enabled = config.is_oauth21_enabled()

    if oauth21_enabled:
        if not config.is_configured():
            raise RuntimeError(
                "streamable-http transport requires GOOGLE_OAUTH_CLIENT_ID so OAuth 2.1 "
                "protocol authentication can be configured."
            )

        def validate_and_derive_jwt_key(
            jwt_signing_key_override: str | None, client_secret: str | None
        ) -> bytes:
            """Validate JWT signing key override and derive the final JWT key."""
            if jwt_signing_key_override:
                if len(jwt_signing_key_override) < 12:
                    logger.warning(
                        "OAuth 2.1: FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY is less than 12 characters; "
                        "use a longer secret to improve key derivation strength."
                    )
                return derive_jwt_key(
                    low_entropy_material=jwt_signing_key_override,
                    salt="fastmcp-jwt-signing-key",
                )
            if client_secret:
                return derive_jwt_key(
                    high_entropy_material=client_secret,
                    salt="fastmcp-jwt-signing-key",
                )
            raise ValueError(
                "Public client OAuth 2.1 requires FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY "
                "when GOOGLE_OAUTH_CLIENT_SECRET is not set."
            )

        try:
            # Import common dependencies for storage backends
            from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
            from cryptography.fernet import Fernet
            from fastmcp.server.auth.jwt_issuer import derive_jwt_key

            provider_valid_scopes: List[str] = sorted(get_current_scopes())
            provider_required_scopes: List[str] = sorted(PROTOCOL_AUTH_SCOPES)

            client_storage = None
            jwt_signing_key_override = (
                os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip()
                or None
            )
            storage_backend = (
                os.getenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "")
                .strip()
                .lower()
            )
            valkey_host = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", "").strip()

            # Determine storage backend: valkey, disk, memory (default)
            use_valkey = storage_backend == "valkey" or bool(valkey_host)
            use_disk = storage_backend == "disk"

            if use_valkey:
                try:
                    from key_value.aio.stores.valkey import ValkeyStore

                    valkey_port_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT", "6379"
                    ).strip()
                    valkey_db_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_DB", "0"
                    ).strip()

                    valkey_port = int(valkey_port_raw)
                    valkey_db = int(valkey_db_raw)
                    valkey_use_tls_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", ""
                    ).strip()
                    valkey_use_tls = (
                        _parse_bool_env(valkey_use_tls_raw)
                        if valkey_use_tls_raw
                        else valkey_port == 6380
                    )

                    valkey_request_timeout_ms_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", ""
                    ).strip()
                    valkey_connection_timeout_ms_raw = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", ""
                    ).strip()

                    valkey_request_timeout_ms = (
                        int(valkey_request_timeout_ms_raw)
                        if valkey_request_timeout_ms_raw
                        else None
                    )
                    valkey_connection_timeout_ms = (
                        int(valkey_connection_timeout_ms_raw)
                        if valkey_connection_timeout_ms_raw
                        else None
                    )

                    valkey_username = (
                        os.getenv(
                            "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USERNAME", ""
                        ).strip()
                        or None
                    )
                    valkey_password = (
                        os.getenv(
                            "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PASSWORD", ""
                        ).strip()
                        or None
                    )

                    if not valkey_host:
                        valkey_host = "localhost"

                    client_storage = ValkeyStore(
                        host=valkey_host,
                        port=valkey_port,
                        db=valkey_db,
                        username=valkey_username,
                        password=valkey_password,
                    )

                    # Configure TLS and timeouts on the underlying Glide client config.
                    # ValkeyStore currently doesn't expose these settings directly.
                    glide_config = getattr(client_storage, "_client_config", None)
                    if glide_config is not None:
                        glide_config.use_tls = valkey_use_tls

                        is_remote_host = valkey_host not in {"localhost", "127.0.0.1"}
                        if valkey_request_timeout_ms is None and (
                            valkey_use_tls or is_remote_host
                        ):
                            # Glide defaults to 250ms if unset; increase for remote/TLS endpoints.
                            valkey_request_timeout_ms = 5000
                        if valkey_request_timeout_ms is not None:
                            glide_config.request_timeout = valkey_request_timeout_ms

                        if valkey_connection_timeout_ms is None and (
                            valkey_use_tls or is_remote_host
                        ):
                            valkey_connection_timeout_ms = 10000
                        if valkey_connection_timeout_ms is not None:
                            from glide_shared.config import (
                                AdvancedGlideClientConfiguration,
                            )

                            glide_config.advanced_config = (
                                AdvancedGlideClientConfiguration(
                                    connection_timeout=valkey_connection_timeout_ms
                                )
                            )

                    jwt_signing_key = validate_and_derive_jwt_key(
                        jwt_signing_key_override, config.client_secret
                    )

                    storage_encryption_key = derive_jwt_key(
                        high_entropy_material=jwt_signing_key.decode(),
                        salt="fastmcp-storage-encryption-key",
                    )

                    client_storage = FernetEncryptionWrapper(
                        key_value=client_storage,
                        fernet=Fernet(key=storage_encryption_key),
                    )
                    logger.info(
                        "OAuth 2.1: Using ValkeyStore for FastMCP OAuth proxy client_storage (host=%s, port=%s, db=%s, tls=%s)",
                        valkey_host,
                        valkey_port,
                        valkey_db,
                        valkey_use_tls,
                    )
                    if valkey_request_timeout_ms is not None:
                        logger.info(
                            "OAuth 2.1: Valkey request timeout set to %sms",
                            valkey_request_timeout_ms,
                        )
                    if valkey_connection_timeout_ms is not None:
                        logger.info(
                            "OAuth 2.1: Valkey connection timeout set to %sms",
                            valkey_connection_timeout_ms,
                        )
                    logger.info(
                        "OAuth 2.1: Applied Fernet encryption wrapper to Valkey client_storage (key derived from FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY or GOOGLE_OAUTH_CLIENT_SECRET)."
                    )
                except ImportError as exc:
                    logger.warning(
                        "OAuth 2.1: Valkey client_storage requested but Valkey dependencies are not installed (%s). "
                        "Install 'workspace-mcp[valkey]' (or 'py-key-value-aio[valkey]', which includes 'valkey-glide') "
                        "or unset WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND/WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST.",
                        exc,
                    )
                except ValueError as exc:
                    logger.warning(
                        "OAuth 2.1: Invalid Valkey configuration; falling back to default storage (%s).",
                        exc,
                    )
            elif use_disk:
                try:
                    from core.storage import make_sanitized_file_store

                    disk_directory = os.getenv(
                        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", ""
                    ).strip()
                    if not disk_directory:
                        # Default to FASTMCP_HOME/oauth-proxy or ~/.fastmcp/oauth-proxy
                        fastmcp_home = os.getenv("FASTMCP_HOME", "").strip()
                        if fastmcp_home:
                            disk_directory = os.path.join(fastmcp_home, "oauth-proxy")
                        else:
                            disk_directory = os.path.expanduser(
                                "~/.fastmcp/oauth-proxy"
                            )

                    client_storage = make_sanitized_file_store(disk_directory)

                    jwt_signing_key = validate_and_derive_jwt_key(
                        jwt_signing_key_override, config.client_secret
                    )

                    storage_encryption_key = derive_jwt_key(
                        high_entropy_material=jwt_signing_key.decode(),
                        salt="fastmcp-storage-encryption-key",
                    )

                    client_storage = FernetEncryptionWrapper(
                        key_value=client_storage,
                        fernet=Fernet(key=storage_encryption_key),
                    )
                    logger.info(
                        "OAuth 2.1: Using FileTreeStore for FastMCP OAuth proxy client_storage (directory=%s)",
                        disk_directory,
                    )
                except ImportError as exc:
                    logger.warning(
                        "OAuth 2.1: Disk storage requested but dependencies not available (%s). "
                        "Falling back to default storage.",
                        exc,
                    )
            elif storage_backend == "memory":
                from key_value.aio.stores.memory import MemoryStore

                client_storage = MemoryStore()
                logger.info(
                    "OAuth 2.1: Using MemoryStore for FastMCP OAuth proxy client_storage"
                )
            # else: client_storage remains None, FastMCP uses its default

            # Ensure JWT signing key is always derived for all storage backends
            if "jwt_signing_key" not in locals():
                jwt_signing_key = validate_and_derive_jwt_key(
                    jwt_signing_key_override, config.client_secret
                )

            # Check if external OAuth provider is configured
            if config.is_external_oauth21_provider():
                # External OAuth mode: use custom provider that handles ya29.* access tokens
                from auth.external_oauth_provider import ExternalOAuthProvider

                provider = ExternalOAuthProvider(
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                    base_url=config.get_oauth_base_url(),
                    redirect_path=config.redirect_path,
                    required_scopes=provider_valid_scopes,
                    resource_server_url=config.get_oauth_base_url(),
                    jwt_signing_key=jwt_signing_key,
                )
                server.auth = provider

                logger.info("OAuth 2.1 enabled with EXTERNAL provider mode")
                logger.info(
                    "Expecting Authorization bearer tokens in tool call headers"
                )
                logger.info(
                    "Protected resource metadata points to Google's authorization server"
                )
            else:
                # Standard OAuth 2.1 mode: use FastMCP's GoogleProvider
                allowed_client_redirect_uris = _parse_allowed_redirect_uris(
                    os.getenv("WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS")
                )
                if allowed_client_redirect_uris:
                    logger.info(
                        "OAuth 2.1: restricting DCR client redirect URIs to allowlist: %s",
                        allowed_client_redirect_uris,
                    )
                provider = GoogleProvider(
                    client_id=config.client_id,
                    client_secret=config.client_secret,
                    base_url=config.get_oauth_base_url(),
                    redirect_path=config.redirect_path,
                    required_scopes=provider_required_scopes,
                    valid_scopes=provider_valid_scopes,
                    client_storage=client_storage,
                    jwt_signing_key=jwt_signing_key,
                    allowed_client_redirect_uris=allowed_client_redirect_uris,
                )
                if provider.client_registration_options is not None:
                    # Keep protocol-level auth limited to base identity scopes, but
                    # allow dynamically registered MCP clients to request any scope
                    # needed by enabled tools during subsequent authorization flows.
                    provider.client_registration_options.default_scopes = (
                        provider_valid_scopes
                    )
                # CIMD clients can bypass DCR defaults and fall back to FastMCP's
                # internal scope string, so keep it aligned with valid scopes too.
                cimd_default_scope = " ".join(provider_valid_scopes)
                provider._default_scope_str = cimd_default_scope
                cimd_manager = getattr(provider, "_cimd_manager", None)
                if cimd_manager is not None:
                    cimd_manager.default_scope = cimd_default_scope
                # Enable protocol-level auth
                server.auth = provider
                logger.info(
                    "OAuth 2.1 enabled using FastMCP GoogleProvider with protocol-level auth"
                )

            # Always set auth provider for token validation in middleware
            set_auth_provider(provider)
            _auth_provider = provider
        except Exception as exc:
            logger.error(
                "Failed to initialize FastMCP GoogleProvider: %s", exc, exc_info=True
            )
            raise
    else:
        # Debug level: main.py surfaces the loopback default as a startup notice.
        logger.debug(
            "OAuth 2.0 legacy mode - streamable HTTP defaults to loopback unless "
            "WORKSPACE_MCP_HOST is explicitly set."
        )
        server.auth = None
        _auth_provider = None
        set_auth_provider(None)
        _ensure_legacy_callback_route()


def get_auth_provider() -> Optional[GoogleProvider]:
    """Gets the global authentication provider instance."""
    return _auth_provider


@server.custom_route("/", methods=["GET"])
@server.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    try:
        version = metadata.version("workspace-mcp")
    except metadata.PackageNotFoundError:
        version = "dev"
    return JSONResponse(
        {
            "status": "healthy",
            "service": "workspace-mcp",
            "version": version,
            "transport": get_transport_mode(),
        }
    )


@server.custom_route("/attachments/{file_id}", methods=["GET"])
async def serve_attachment(request: Request):
    """Serve a stored attachment file."""
    from core.attachment_storage import get_attachment_storage

    file_id = request.path_params["file_id"]
    storage = get_attachment_storage()
    metadata = storage.get_attachment_metadata(file_id)

    if not metadata:
        return JSONResponse(
            {"error": "Attachment not found or expired"}, status_code=404
        )

    file_path = storage.get_attachment_path(file_id)
    if not file_path:
        return JSONResponse({"error": "Attachment file not found"}, status_code=404)

    return FileResponse(
        path=str(file_path),
        filename=metadata["filename"],
        media_type=metadata["mime_type"],
    )


async def legacy_oauth2_callback(request: Request) -> HTMLResponse:
    state = request.query_params.get("state")
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        msg = (
            f"Authentication failed: Google returned an error: {error}. State: {state}."
        )
        logger.error(msg)
        return create_error_response(msg)

    if not code:
        msg = "Authentication failed: No authorization code received from Google."
        logger.error(msg)
        return create_error_response(msg)

    try:
        error_message = check_client_secrets()
        if error_message:
            return create_server_error_response(error_message)

        logger.info("OAuth callback: Received authorization code.")

        mcp_session_id = None
        if hasattr(request, "state") and hasattr(request.state, "session_id"):
            mcp_session_id = request.state.session_id

        verified_user_id, credentials = await handle_auth_callback(
            scopes=get_current_scopes(),
            authorization_response=str(request.url),
            redirect_uri=get_oauth_redirect_uri_for_current_mode(),
            session_id=mcp_session_id,
        )

        logger.info(
            f"OAuth callback: Successfully authenticated user: {verified_user_id}."
        )

        return create_success_response(verified_user_id)
    except Exception as e:
        logger.error(f"Error processing OAuth callback: {str(e)}", exc_info=True)
        return create_server_error_response(str(e))


@server.tool(
    title="List Google Accounts",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def list_google_accounts() -> str:
    """
    List the Google accounts this server holds credentials for.

    Answers from the credential store and already-cached state only: it makes NO
    API calls and never probes an account, so it is safe to call at any time.

    Reports, per account: the email address, whether it is the configured default
    (USER_GOOGLE_EMAIL), and the last known Google Docs Developer Preview verdict
    (available / unavailable / unknown) with its source and timestamp. "unknown"
    means nothing has been observed for that account yet — it is not a capability
    miss. Where `check_docs_review_capabilities` is available, running it against
    an account is what settles the verdict; the `notes` in this tool's own output
    say whether it is, since this description is static text that cannot know
    which tools a given server was started with.

    Use this to see which accounts exist before asking the user which one to use.
    Seeing an account here is NOT permission to use it: keep using the default
    account unless the user explicitly names another one, and never retry a failed
    call under a different account on your own.

    Returns:
        str: JSON report: identity_mode, default_account, accounts
            [{email, is_default, docs_preview {availability, source, checked_at}}],
            accounts_enumerated, store_status, store_detail, docs_preview_loaded,
            probed, notes.
    """
    # resolve_default_account(), not core.config.USER_GOOGLE_EMAIL: that constant
    # froze while this module was being imported, which in main.py is before
    # load_dotenv() runs. A default configured only in .env would otherwise be
    # reported as "no default" here while the refreshed instructions name it —
    # and an agent told there is no default has been invited to pick one.
    return render_account_report(resolve_default_account())


@server.tool(
    title="Start Google Auth",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def start_google_auth(
    service_name: str, user_google_email: Optional[str] = None
) -> str:
    """
    Manually initiate Google OAuth authentication flow.

    NOTE: This is a legacy OAuth 2.0 tool and is disabled when OAuth 2.1 is enabled.
    The authentication system automatically handles credential checks and prompts for
    authentication when needed. Only use this tool if:
    1. You need to re-authenticate with different credentials
    2. You want to proactively authenticate before using other tools
    3. The automatic authentication flow failed and you need to retry

    In most cases, simply try calling the Google Workspace tool you need - it will
    automatically handle authentication if required.
    """
    if is_oauth21_enabled():
        if is_external_oauth21_provider():
            return (
                "start_google_auth is disabled when OAuth 2.1 is enabled. "
                "Provide a valid OAuth 2.1 bearer token in the Authorization header "
                "and retry the original tool."
            )
        return (
            "start_google_auth is disabled when OAuth 2.1 is enabled. "
            "Authenticate through your MCP client's OAuth 2.1 flow and retry the "
            "original tool."
        )

    if is_trust_gateway_identity():
        user_google_email = await get_verified_gateway_principal()

    # Resolved here rather than bound as a default argument. A default is
    # evaluated once, while this module is being imported — which in main.py is
    # before load_dotenv() — so a default account configured only in .env was
    # baked in as None AND advertised as None in the tool's schema, which
    # list_tools does not re-patch because the parameter is optional rather
    # than required. The rest of the server re-derives this value; so does this.
    if not user_google_email:
        user_google_email = resolve_default_account()

    if not user_google_email:
        raise ValueError("user_google_email must be provided.")

    error_message = check_client_secrets()
    if error_message:
        return f"**Authentication Error:** {error_message}"

    try:
        # Only stdio legacy OAuth depends on the standalone callback server; the
        # helper no-ops in other transports and binds the port lazily (#832).
        from auth.oauth_callback_server import ensure_stdio_oauth_callback_available

        success, error_msg = await asyncio.to_thread(
            ensure_stdio_oauth_callback_available
        )
        if not success:
            error_detail = f" ({error_msg})" if error_msg else ""
            return f"**Error:** Cannot initiate OAuth flow - callback server unavailable{error_detail}"

        auth_message = await start_auth_flow(
            user_google_email=user_google_email,
            service_name=service_name,
            redirect_uri=get_oauth_redirect_uri_for_current_mode(),
            principal_source=(
                "gateway_assertion" if is_trust_gateway_identity() else None
            ),
        )
        return auth_message
    except Exception as e:
        logger.error(f"Failed to start Google authentication flow: {e}", exc_info=True)
        return f"**Error:** An unexpected error occurred: {e}"
