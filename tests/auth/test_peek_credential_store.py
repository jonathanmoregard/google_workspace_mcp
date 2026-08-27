"""``peek_credential_store`` reads without claiming the process-wide singleton.

``core.account_directory`` resolves a credential store at import time (FastMCP's
constructor needs the server instructions string), which is *before* ``main.py``
calls ``load_dotenv``. Installing a store built from that environment would hand
every later caller — including the startup permissions check — a store pointed
at the wrong credentials directory.
"""

import auth.credential_store as credential_store
from auth.credential_store import (
    LocalDirectoryCredentialStore,
    build_credential_store,
    get_credential_store,
    peek_credential_store,
)


def test_peek_does_not_install_a_global_store(monkeypatch, tmp_path):
    monkeypatch.setattr(credential_store, "_credential_store", None)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path / "early"))

    peeked = peek_credential_store()

    assert isinstance(peeked, LocalDirectoryCredentialStore)
    assert credential_store._credential_store is None

    # A later caller, after the real configuration has landed, still wins.
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path / "configured"))
    assert get_credential_store().base_dir == str(tmp_path / "configured")


def test_peek_returns_the_installed_store_when_there_is_one(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(credential_store, "_credential_store", sentinel)

    assert peek_credential_store() is sentinel


def test_build_credential_store_never_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(credential_store, "_credential_store", None)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIALS_DIR", str(tmp_path / "creds"))

    first = build_credential_store()
    second = build_credential_store()

    assert first is not second
    assert credential_store._credential_store is None


def test_build_credential_store_rejects_an_unknown_backend(monkeypatch):
    monkeypatch.setattr(credential_store, "_credential_store", None)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", "postgres")

    try:
        build_credential_store()
    except ValueError as exc:
        assert "postgres" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("an unknown backend must be rejected")


def test_gcs_backend_still_requires_oauth21(monkeypatch):
    monkeypatch.setattr(credential_store, "_credential_store", None)
    monkeypatch.setenv("WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND", "gcs")
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "false")

    try:
        build_credential_store()
    except ValueError as exc:
        assert "MCP_ENABLE_OAUTH21" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("gcs without OAuth 2.1 must be rejected")
