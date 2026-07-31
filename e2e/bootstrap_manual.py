#!/usr/bin/env python3
"""Two-step OAuth bootstrap that never runs a local callback server.

The server-based flow in ``bootstrap_auth.py`` assumes the browser can reach
a short-lived listener on localhost, and that nothing *else* reaches it. Both
assumptions break in practice:

* the library's callback wait can be outrun by a real consent flow (the
  unverified-app interstitial alone can take longer), and the auth code is
  then unusable because its PKCE verifier died with the listener;
* stale ``localhost:<port>`` tabs from earlier attempts re-hit the new
  listener on reload, and the first one to arrive kills the flow with
  ``MismatchingStateError`` before the good callback lands;
* the browser may not even be on this host.

This module splits the flow in two, persisting the PKCE verifier and the CSRF
state to disk between the halves, so the code the user copies out of the
browser can always be exchanged - regardless of timing, tabs, or which
machine ran the browser.

Usage:
    uv run python e2e/bootstrap_manual.py start
    # approve in any browser, copy the localhost URL it lands on (it will
    # show a connection error - that is expected and harmless)
    uv run python e2e/bootstrap_manual.py finish '<pasted localhost URL>'

The redirect URI is the client's registered ``http://localhost``; nothing
listens there, which is exactly the point - the browser's failure to connect
still leaves ``code`` and ``state`` visible in the address bar.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from e2e.gating import (  # noqa: E402 - needs the sys.path bootstrap above
    OAUTH_CLIENT_PATH,
    REQUIRED_SCOPES,
    resolve_credentials_dir,
)

#: Where the in-flight PKCE verifier + CSRF state live between the two steps.
PENDING_PATH = REPO_ROOT / "credentials" / ".oauth_pending.json"

REDIRECT_URI = "http://localhost"


def fail(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _flow(client: Path):
    from google_auth_oauthlib.flow import Flow

    if not client.is_file():
        fail(
            f"OAuth client JSON not found at {client}. "
            "See pending_for_human.md for how to create it."
        )
    flow = Flow.from_client_secrets_file(
        str(client), scopes=list(REQUIRED_SCOPES), redirect_uri=REDIRECT_URI
    )
    return flow


def start(client: Path) -> int:
    flow = _flow(client)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": flow.code_verifier,
                "client": str(client),
            }
        ),
        encoding="utf-8",
    )
    PENDING_PATH.chmod(0o600)
    print(f"Requesting {len(REQUIRED_SCOPES)} scopes:")
    for scope in REQUIRED_SCOPES:
        print(f"  - {scope}")
    print(f"\nstate: {state}")
    print(f"pending file: {PENDING_PATH}")
    print("\nOpen this URL, approve, then copy the localhost URL you land on")
    print("(the browser will show a connection error - that is expected):\n")
    print(f"    {url}\n")
    print("Then run:")
    print("    uv run python e2e/bootstrap_manual.py finish '<pasted URL>'")
    return 0


def finish(pasted: str) -> int:
    if not PENDING_PATH.is_file():
        fail(f"No pending flow at {PENDING_PATH}. Run 'start' first.")
    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))

    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
    if "error" in query:
        fail(f"Authorization server returned an error: {query['error'][0]}")
    if "code" not in query:
        fail(
            "No 'code' parameter in the pasted URL. Paste the FULL localhost "
            "URL from the address bar, including everything after '?'."
        )
    code = query["code"][0]
    got_state = (query.get("state") or [None])[0]
    if got_state != pending["state"]:
        fail(
            "State mismatch - the pasted URL belongs to a different (older) "
            f"authorization attempt.\n  expected: {pending['state']}\n"
            f"  received: {got_state}\n"
            "Re-run 'start' and use ONLY the URL it prints."
        )

    flow = _flow(Path(pending["client"]))
    flow.code_verifier = pending["code_verifier"]
    flow.fetch_token(code=code)
    credentials = flow.credentials

    if not credentials.refresh_token:
        fail(
            "Google did not return a refresh token. Revoke the app at "
            "https://myaccount.google.com/permissions and re-run 'start'.",
            code=3,
        )

    from auth.credential_store import LocalDirectoryCredentialStore
    from auth.google_auth import get_user_info

    info = get_user_info(credentials)
    if not info or "email" not in info:
        fail("Could not fetch the authenticated user's email (userinfo).", code=3)
    email = info["email"]

    credentials_dir = resolve_credentials_dir()
    store = LocalDirectoryCredentialStore(str(credentials_dir))
    if not store.store_credential(email, credentials):
        fail(f"Failed to write token for {email} under {credentials_dir}.", code=3)

    PENDING_PATH.unlink(missing_ok=True)
    print("\nBootstrap complete.")
    print(f"  account:   {email}")
    print(f"  token dir: {credentials_dir}")
    print("\nNext: uv run pytest e2e -m e2e_ga")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="print the consent URL")
    p_start.add_argument("--client", type=Path, default=OAUTH_CLIENT_PATH)

    p_finish = sub.add_parser("finish", help="exchange the pasted callback URL")
    p_finish.add_argument("url", help="the full localhost URL from the browser")

    args = parser.parse_args(argv)
    if args.command == "start":
        return start(args.client)
    return finish(args.url)


if __name__ == "__main__":
    raise SystemExit(main())
