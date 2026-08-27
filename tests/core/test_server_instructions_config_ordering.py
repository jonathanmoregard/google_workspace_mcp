"""The server instructions must reflect `.env`, not the env before it loads.

``main.py`` imports :mod:`core.server` — which builds the FastMCP
``instructions`` string — BEFORE it calls ``load_dotenv()`` and
``reload_oauth_config()``. Anything configured only in ``.env`` was therefore
invisible to the guard that decides whether to enumerate the credential store,
so a server put into OAuth 2.1 mode by ``.env`` alone answered the MCP
handshake with one principal's email address for every caller, while the
runtime ``list_google_accounts`` tool in the same process correctly reported
``accounts_enumerated: false``.

The blackbox tests here drive the real path: a real ``main.py`` subprocess, a
real ``.env``, a real MCP ``initialize`` handshake. ``main.py`` resolves
``.env`` from its OWN location (``os.path.dirname(os.path.abspath(__file__))``),
so each spawn runs a SYMLINK to ``main.py`` placed in a tmp directory next to
the ``.env`` under test — the repo's own ``.env`` is never written to.
``os.path.abspath`` does not resolve symlinks, so the child reads the tmp
``.env`` while importing the repo's modules through ``PYTHONPATH``.

The in-process tests then cover the other two vectors (trusted gateway, and a
credentials directory that only ``.env`` names) without paying for a spawn.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ACCOUNT = "alice@example.com"
OTHER_ACCOUNT = "bob@example.com"

#: Host env vars that would change the child server's mode if inherited.
_STRIPPED_ENV_VARS = (
    "WORKSPACE_MCP_TOOLS",
    "WORKSPACE_MCP_TOOL_TIER",
    "WORKSPACE_MCP_READ_ONLY",
    "WORKSPACE_MCP_PERMISSIONS",
    "WORKSPACE_MCP_TRANSPORT",
    "WORKSPACE_MCP_CREDENTIALS_DIR",
    "GOOGLE_MCP_CREDENTIALS_DIR",
    "MCP_SINGLE_USER_MODE",
    "MCP_ENABLE_OAUTH21",
    "TRUST_GATEWAY_IDENTITY",
    "EXTERNAL_OAUTH21_PROVIDER",
    "WORKSPACE_MCP_STATELESS_MODE",
    "USER_GOOGLE_EMAIL",
)

EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS = (
    f"Connected Google account: {DEFAULT_ACCOUNT}\n"
    "\n"
    f"When using Google Workspace tools, always use `{DEFAULT_ACCOUNT}` as the "
    "`user_google_email` parameter. Do not ask the user for their email address."
)


def _store(tmp_path: Path, name: str, emails: tuple[str, ...]) -> str:
    """A credentials directory holding one file per account."""
    base_dir = tmp_path / name
    base_dir.mkdir(parents=True, exist_ok=True)
    for email in emails:
        (base_dir / f"{email}.json").write_text("{}", encoding="utf-8")
    return str(base_dir)


# --------------------------------------------------------------------------
# Blackbox: a real main.py subprocess, a real .env, a real MCP handshake
# --------------------------------------------------------------------------


def _server_dir(tmp_path: Path, dotenv: str) -> Path:
    """A directory holding ``.env`` and a symlink to the repo's ``main.py``."""
    server_dir = tmp_path / "server"
    server_dir.mkdir(parents=True, exist_ok=True)
    (server_dir / "main.py").symlink_to(REPO_ROOT / "main.py")
    (server_dir / ".env").write_text(dotenv, encoding="utf-8")
    return server_dir


def _child_env(**pins: str) -> dict[str, str]:
    env = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(pins)
    return env


def _handshake_instructions(
    server_dir: Path, env: dict[str, str], timeout: float = 120.0
) -> str | None:
    """Spawn the server over stdio and return the ``initialize`` instructions.

    Raw JSON-RPC over pipes rather than the fastmcp client: that keeps the test
    fully synchronous, so it cannot disturb the ambient asyncio event-loop state
    that other modules in this suite still depend on.
    """
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "instructions-ordering-test", "version": "0"},
        },
    }
    process = subprocess.Popen(
        [
            sys.executable,
            str(server_dir / "main.py"),
            "--transport",
            "stdio",
            "--tools",
            "drive",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(REPO_ROOT),
        text=True,
        bufsize=1,
    )
    watchdog = threading.Timer(timeout, process.kill)
    watchdog.start()
    try:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue  # non-JSON chatter on stdout is not our business
            if message.get("id") == 1:
                assert "error" not in message, message["error"]
                return message["result"].get("instructions")
        raise AssertionError("the server never answered initialize")
    finally:
        watchdog.cancel()
        process.kill()
        process.wait(timeout=30)


def test_handshake_enumerates_when_nothing_is_configured_in_dotenv(tmp_path):
    """Control: with an empty ``.env`` the two stored accounts ARE named.

    Without this the leak assertion below would pass for the wrong reason (a
    server that failed to start, or a store that held one account).
    """
    server_dir = _server_dir(tmp_path, "# nothing here\n")
    env = _child_env(
        USER_GOOGLE_EMAIL=DEFAULT_ACCOUNT,
        WORKSPACE_MCP_CREDENTIALS_DIR=_store(
            tmp_path, "creds", (DEFAULT_ACCOUNT, OTHER_ACCOUNT)
        ),
    )

    instructions = _handshake_instructions(server_dir, env)

    assert instructions is not None
    assert OTHER_ACCOUNT in instructions


def test_handshake_does_not_name_a_second_account_when_dotenv_enables_oauth21(
    tmp_path,
):
    """``MCP_ENABLE_OAUTH21`` in ``.env`` only must still suppress enumeration.

    In OAuth 2.1 mode the credential store is shared across principals, so
    naming an account out of it in the handshake hands one principal's address
    to every caller.
    """
    server_dir = _server_dir(tmp_path, "MCP_ENABLE_OAUTH21=true\n")
    env = _child_env(
        USER_GOOGLE_EMAIL=DEFAULT_ACCOUNT,
        WORKSPACE_MCP_CREDENTIALS_DIR=_store(
            tmp_path, "creds", (DEFAULT_ACCOUNT, OTHER_ACCOUNT)
        ),
    )

    instructions = _handshake_instructions(server_dir, env)

    assert OTHER_ACCOUNT not in (instructions or ""), (
        "the handshake named another principal's account because the "
        "instructions were built before .env was loaded"
    )
    # OAuth 2.1 mode has no configured default account at all, so the whole
    # string is dropped — exactly what the same config in the process env does.
    assert instructions is None


# --------------------------------------------------------------------------
# In-process: the rebuild covers all three vectors
# --------------------------------------------------------------------------


def _reload_oauth_config() -> None:
    """Rebuild the OAuth config singleton that :mod:`core.account_directory` reads.

    ``tests/auth/test_port_resolver.py`` re-imports ``auth.oauth_config`` by
    dropping it from ``sys.modules``, which leaves every module that did
    ``from auth.oauth_config import ...`` bound to the PREVIOUS module object,
    with its own ``_oauth_config`` singleton. Reloading through the very
    function ``core.account_directory`` will call keeps these tests honest
    whatever order the suite runs in.
    """
    import core.account_directory as account_directory

    account_directory.is_oauth21_enabled.__globals__["reload_oauth_config"]()


@pytest.fixture
def rebuildable_server(monkeypatch):
    """Give each test a clean env for the rebuild, and restore what it mutates."""
    import auth.credential_store as credential_store
    import core.server as core_server

    original_instructions = core_server.server.instructions
    original_environ = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # peek_credential_store() returns an installed global in preference to a
    # fresh one; an earlier test may have installed one pointing elsewhere.
    monkeypatch.setattr(credential_store, "_credential_store", None)
    try:
        yield core_server
    finally:
        # The environment is restored here rather than being left to
        # monkeypatch: this fixture is torn down BEFORE monkeypatch undoes its
        # own changes, so reloading the OAuth config below would otherwise
        # freeze this test's mode flags into the singleton for the rest of the
        # session.
        os.environ.clear()
        os.environ.update(original_environ)
        _reload_oauth_config()
        core_server.server.instructions = original_instructions


def _refresh(core_server) -> str | None:
    """Reload the OAuth config from the current env, then rebuild the string."""
    _reload_oauth_config()
    return core_server.refresh_server_instructions()


def test_rebuild_stops_enumerating_when_oauth21_arrives_late(
    rebuildable_server, monkeypatch, tmp_path
):
    core_server = rebuildable_server
    monkeypatch.setenv("USER_GOOGLE_EMAIL", DEFAULT_ACCOUNT)
    monkeypatch.setenv(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        _store(tmp_path, "creds", (DEFAULT_ACCOUNT, OTHER_ACCOUNT)),
    )

    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "false")
    assert OTHER_ACCOUNT in (_refresh(core_server) or "")

    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    assert _refresh(core_server) is None
    assert core_server.server.instructions is None


def test_rebuild_stops_enumerating_when_the_gateway_flag_arrives_late(
    rebuildable_server, monkeypatch, tmp_path
):
    core_server = rebuildable_server
    monkeypatch.setenv("USER_GOOGLE_EMAIL", DEFAULT_ACCOUNT)
    monkeypatch.setenv(
        "WORKSPACE_MCP_CREDENTIALS_DIR",
        _store(tmp_path, "creds", (DEFAULT_ACCOUNT, OTHER_ACCOUNT)),
    )

    assert OTHER_ACCOUNT in (_refresh(core_server) or "")

    monkeypatch.setenv("TRUST_GATEWAY_IDENTITY", "true")
    monkeypatch.setenv(
        "GATEWAY_IDENTITY_JWKS_URL", "https://gateway.example.com/.well-known/jwks.json"
    )
    monkeypatch.setenv("GATEWAY_IDENTITY_AUDIENCE", "workspace-mcp")
    assert _refresh(core_server) is None
    assert core_server.server.instructions is None


def test_rebuild_follows_a_credentials_directory_that_arrives_late(
    rebuildable_server, monkeypatch, tmp_path
):
    """The third vector: no mode flag, just a different store."""
    core_server = rebuildable_server
    monkeypatch.setenv("USER_GOOGLE_EMAIL", DEFAULT_ACCOUNT)
    two_accounts = _store(tmp_path, "shared", (DEFAULT_ACCOUNT, OTHER_ACCOUNT))
    one_account = _store(tmp_path, "solo", (DEFAULT_ACCOUNT,))

    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", two_accounts)
    assert OTHER_ACCOUNT in (_refresh(core_server) or "")

    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", one_account)
    assert _refresh(core_server) == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS


def test_the_import_time_value_never_enumerates_the_store(monkeypatch, tmp_path):
    """The value FastMCP is constructed with must be safe on its own.

    Whatever the environment says at import, the string built there names only
    the configured default. The rebuild is what may name other accounts.
    """
    import core.account_directory as account_directory
    from auth.credential_store import LocalDirectoryCredentialStore

    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)

    def fail_if_called():
        raise AssertionError("the import-time build must not enumerate the store")

    monkeypatch.setattr(
        account_directory,
        "peek_credential_store",
        lambda: LocalDirectoryCredentialStore(
            base_dir=_store(tmp_path, "creds", (DEFAULT_ACCOUNT, OTHER_ACCOUNT))
        ),
    )

    assert (
        account_directory.build_server_instructions(
            DEFAULT_ACCOUNT, enumerate_store=False
        )
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )

    monkeypatch.setattr(account_directory, "peek_credential_store", fail_if_called)
    assert (
        account_directory.build_server_instructions(
            DEFAULT_ACCOUNT, enumerate_store=False
        )
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )
