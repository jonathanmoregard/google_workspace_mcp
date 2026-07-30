#!/usr/bin/env python3
"""One-time OAuth bootstrap for the e2e suite - the ONLY interactive step.

Reads the OAuth *Desktop app* client JSON from
``credentials/oauth_client.json`` (see pending_for_human.md), runs the
installed-app flow (prints the consent URL, waits for the localhost
callback), and writes the token exactly where the MCP server's
credential store expects it (``~/.google_workspace_mcp/credentials`` or
``WORKSPACE_MCP_CREDENTIALS_DIR``), with every scope the docs_preview +
Drive comment tools need.

Usage:
    uv run python e2e/bootstrap_auth.py [--client PATH] [--credentials-dir DIR] [--port N]

The e2e suite itself never triggers this flow: without a stored token it
hard-skips with instructions pointing here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from e2e.gating import (  # noqa: E402 - needs the sys.path bootstrap above
    OAUTH_CLIENT_PATH,
    REQUIRED_SCOPES,
    resolve_credentials_dir,
)


def fail(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def validate_client_file(path: Path) -> None:
    if not path.is_file():
        fail(
            f"OAuth client JSON not found at {path}.\n"
            "Create a Desktop-app OAuth client and save its JSON there "
            "(exact steps: pending_for_human.md at the repo root)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        fail(f"{path} is not valid JSON: {exc}")
    if "installed" not in data:
        found = ", ".join(data) or "nothing"
        fail(
            f"{path} does not look like a *Desktop app* OAuth client "
            f"(expected top-level 'installed' key, found: {found}). "
            "Re-download the client as type 'Desktop app'."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--client",
        type=Path,
        default=OAUTH_CLIENT_PATH,
        help=f"OAuth Desktop client JSON (default: {OAUTH_CLIENT_PATH})",
    )
    parser.add_argument(
        "--credentials-dir",
        type=Path,
        default=None,
        help="Token store dir (default: WORKSPACE_MCP_CREDENTIALS_DIR or "
        "~/.google_workspace_mcp/credentials)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local callback port (default 0 = pick a free port)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Seconds to wait for the browser callback (default 1800). The "
        "library default is short enough that a real consent flow - including "
        "the unverified-app interstitial - can outrun it, and the auth code "
        "is then unusable because its PKCE verifier died with the listener.",
    )
    args = parser.parse_args(argv)

    validate_client_file(args.client)
    credentials_dir = args.credentials_dir or resolve_credentials_dir()

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(args.client), scopes=list(REQUIRED_SCOPES)
    )
    print(f"Requesting {len(REQUIRED_SCOPES)} scopes:")
    for scope in REQUIRED_SCOPES:
        print(f"  - {scope}")
    print()
    credentials = flow.run_local_server(
        host="localhost",
        port=args.port,
        open_browser=False,
        timeout_seconds=args.timeout,
        authorization_prompt_message=(
            "\nOpen this URL in a browser and approve access:\n\n    {url}\n\n"
            "Waiting for the local callback...\n"
        ),
        # offline + consent guarantees a refresh_token even on re-consent.
        access_type="offline",
        prompt="consent",
    )

    if not credentials.refresh_token:
        fail(
            "Google did not return a refresh token. Revoke the app at "
            "https://myaccount.google.com/permissions and re-run this script.",
            code=3,
        )

    from auth.credential_store import LocalDirectoryCredentialStore
    from auth.google_auth import get_user_info

    info = get_user_info(credentials)
    if not info or "email" not in info:
        fail("Could not fetch the authenticated user's email (userinfo).", code=3)
    email = info["email"]

    store = LocalDirectoryCredentialStore(str(credentials_dir))
    if not store.store_credential(email, credentials):
        fail(f"Failed to write token for {email} under {credentials_dir}.", code=3)

    print("\nBootstrap complete.")
    print(f"  account:   {email}")
    print(f"  token dir: {credentials_dir}")
    print("\nNext: uv run pytest e2e -m e2e_ga")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
