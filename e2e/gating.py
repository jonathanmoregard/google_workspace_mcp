"""Credential gating for the blackbox e2e suite.

The e2e suite NEVER triggers interactive OAuth. Before any server is
spawned, this module decides whether a usable token exists in the
server's credential store (``~/.google_workspace_mcp/credentials`` by
default, overridable via ``WORKSPACE_MCP_CREDENTIALS_DIR``) and builds
loud, actionable skip messages when it does not.

Pure logic lives here so it is unit-testable without credentials or
network access (tests/e2e_harness/test_gating.py).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from auth.scopes import BASE_SCOPES, DOCS_PREVIEW_SCOPES, has_required_scopes

REPO_ROOT = Path(__file__).resolve().parent.parent
OAUTH_CLIENT_PATH = REPO_ROOT / "credentials" / "oauth_client.json"

#: Every scope the docs_preview surface (Docs API incl. preview overlay +
#: Drive v3 comments/replies) needs, plus the identity scopes the server
#: always requests. The bootstrap script requests exactly this set so one
#: consent covers the whole e2e suite.
REQUIRED_SCOPES: tuple[str, ...] = tuple(
    dict.fromkeys([*BASE_SCOPES, *DOCS_PREVIEW_SCOPES])
)

#: Filenames in the credentials dir that are never user tokens.
_NON_TOKEN_FILES = {"oauth_states"}

_BOOTSTRAP_STEPS = (
    "The e2e suite never starts an interactive OAuth flow. To enable it:\n"
    "  1. Save the OAuth *Desktop app* client JSON at credentials/oauth_client.json\n"
    "     (human steps: see pending_for_human.md at the repo root)\n"
    "  2. Run once: uv run python e2e/bootstrap_auth.py   (one browser consent)\n"
    "  3. Re-run:   uv run pytest e2e -m e2e_ga"
)


def resolve_credentials_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the token cache dir exactly like the server does.

    Mirrors auth.google_auth.get_default_credentials_dir():
    WORKSPACE_MCP_CREDENTIALS_DIR > GOOGLE_MCP_CREDENTIALS_DIR >
    ~/.google_workspace_mcp/credentials.
    """
    if env is None:
        import os

        env = os.environ
    for var in ("WORKSPACE_MCP_CREDENTIALS_DIR", "GOOGLE_MCP_CREDENTIALS_DIR"):
        value = (env.get(var) or "").strip()
        if value:
            return Path(value).expanduser()
    return Path.home() / ".google_workspace_mcp" / "credentials"


@dataclass
class TokenState:
    """Outcome of the offline token inspection (no network)."""

    status: str  # ok | no_credentials_dir | no_token | unreadable |
    #             no_refresh_token | missing_scopes | refresh_failed
    credentials_dir: Path
    email: str | None = None
    token_path: Path | None = None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "ok"

    def skip_reason(self) -> str:
        """Loud, actionable skip message shown by pytest -ra / -rs."""
        headlines = {
            "no_credentials_dir": "credential directory does not exist",
            "no_token": "no Google OAuth token found",
            "unreadable": "token file exists but cannot be parsed",
            "no_refresh_token": "token has no refresh_token (cannot renew)",
            "missing_scopes": "token lacks scopes the docs_preview tools need",
            "refresh_failed": "token expired and refresh failed",
        }
        headline = headlines.get(self.status, self.status)
        lines = [
            f"E2E SKIPPED - {headline}.",
            f"  credentials dir: {self.credentials_dir}",
        ]
        if self.token_path is not None:
            lines.append(f"  token file:      {self.token_path}")
        if self.detail:
            lines.append(f"  detail:          {self.detail}")
        lines.append(_BOOTSTRAP_STEPS)
        return "\n".join(lines)


def _token_files(credentials_dir: Path) -> list[Path]:
    files = []
    for path in sorted(credentials_dir.glob("*.json")):
        stem = path.name[: -len(".json")]
        email = unquote(stem) if "%" in stem else stem
        if stem in _NON_TOKEN_FILES or email in _NON_TOKEN_FILES:
            continue
        if "@" not in email:
            continue
        files.append(path)
    return files


def inspect_token(credentials_dir: Path) -> TokenState:
    """Classify the credential store contents without touching the network.

    Reads the same ``<url-encoded-email>.json`` layout that
    auth.credential_store.LocalDirectoryCredentialStore writes.
    """
    if not credentials_dir.is_dir():
        return TokenState(
            status="no_credentials_dir",
            credentials_dir=credentials_dir,
            detail="run the bootstrap script to create it",
        )

    candidates = _token_files(credentials_dir)
    if not candidates:
        return TokenState(
            status="no_token",
            credentials_dir=credentials_dir,
            detail="no <email>.json token files present",
        )

    token_path = candidates[0]
    stem = token_path.name[: -len(".json")]
    email = unquote(stem) if "%" in stem else stem

    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return TokenState(
            status="unreadable",
            credentials_dir=credentials_dir,
            email=email,
            token_path=token_path,
            detail=str(exc),
        )

    if not data.get("refresh_token"):
        return TokenState(
            status="no_refresh_token",
            credentials_dir=credentials_dir,
            email=email,
            token_path=token_path,
            detail="re-run the bootstrap script to obtain a refresh token",
        )

    scopes = data.get("scopes") or []
    if not has_required_scopes(scopes, REQUIRED_SCOPES):
        missing = sorted(set(REQUIRED_SCOPES) - set(scopes))
        return TokenState(
            status="missing_scopes",
            credentials_dir=credentials_dir,
            email=email,
            token_path=token_path,
            detail=f"missing: {', '.join(missing)}",
        )

    return TokenState(
        status="ok",
        credentials_dir=credentials_dir,
        email=email,
        token_path=token_path,
    )


def _default_refresher(credentials: Any) -> None:
    from google.auth.transport.requests import Request

    credentials.refresh(Request())


def load_token_credentials(state: TokenState) -> Any:
    """Build google.oauth2 Credentials from an ``ok`` TokenState (no network)."""
    from google.oauth2.credentials import Credentials

    data = json.loads(state.token_path.read_text(encoding="utf-8"))
    expiry = None
    if data.get("expiry"):
        try:
            expiry = datetime.fromisoformat(data["expiry"])
            if expiry.tzinfo is not None:
                expiry = expiry.replace(tzinfo=None)
        except (TypeError, ValueError):
            expiry = None
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
        expiry=expiry,
    )


def prepare_credentials(
    state: TokenState,
    *,
    refresher: Callable[[Any], None] = _default_refresher,
) -> tuple[TokenState, Any | None]:
    """Turn an ``ok`` TokenState into live credentials, refreshing if needed.

    Never interactive: a refresh failure yields a ``refresh_failed`` state
    whose skip_reason() tells the runner to re-run the bootstrap script.
    """
    if not state.ready:
        return state, None

    credentials = load_token_credentials(state)
    if not credentials.valid:
        try:
            refresher(credentials)
        except Exception as exc:  # noqa: BLE001 - any refresh failure gates
            failed = TokenState(
                status="refresh_failed",
                credentials_dir=state.credentials_dir,
                email=state.email,
                token_path=state.token_path,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return failed, None
    return state, credentials
