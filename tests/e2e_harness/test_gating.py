"""Unit tests for e2e.gating - the creds gate must be trustworthy offline."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from e2e import gating


def _utc_iso(delta: timedelta) -> str:
    """Naive-UTC isoformat expiry, matching the credential store format."""
    return (datetime.now(timezone.utc).replace(tzinfo=None) + delta).isoformat()


def write_token(
    directory: Path,
    email: str = "e2e-tester@example.com",
    *,
    encode: bool = True,
    **overrides,
) -> Path:
    from urllib.parse import quote

    data = {
        "token": "ya29.fake-access-token",
        "refresh_token": "1//fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake.apps.googleusercontent.com",
        "client_secret": "fake-secret",
        "scopes": list(gating.REQUIRED_SCOPES),
        "expiry": _utc_iso(timedelta(hours=1)),
    }
    data.update(overrides)
    name = quote(email, safe="@._-") if encode else email
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestResolveCredentialsDir:
    def test_workspace_env_wins(self):
        env = {
            "WORKSPACE_MCP_CREDENTIALS_DIR": "/custom/creds",
            "GOOGLE_MCP_CREDENTIALS_DIR": "/legacy/creds",
        }
        assert gating.resolve_credentials_dir(env) == Path("/custom/creds")

    def test_legacy_env_fallback(self):
        env = {"GOOGLE_MCP_CREDENTIALS_DIR": "/legacy/creds"}
        assert gating.resolve_credentials_dir(env) == Path("/legacy/creds")

    def test_default_is_server_store(self):
        resolved = gating.resolve_credentials_dir({})
        assert resolved == Path.home() / ".google_workspace_mcp" / "credentials"

    def test_blank_env_ignored(self):
        env = {"WORKSPACE_MCP_CREDENTIALS_DIR": "  "}
        resolved = gating.resolve_credentials_dir(env)
        assert resolved == Path.home() / ".google_workspace_mcp" / "credentials"


class TestRequiredScopes:
    def test_covers_docs_drive_and_identity(self):
        scopes = set(gating.REQUIRED_SCOPES)
        assert "https://www.googleapis.com/auth/documents" in scopes
        assert "https://www.googleapis.com/auth/drive" in scopes
        assert "https://www.googleapis.com/auth/userinfo.email" in scopes


class TestInspectToken:
    def test_missing_dir(self, tmp_path):
        state = gating.inspect_token(tmp_path / "nope")
        assert state.status == "no_credentials_dir"
        assert not state.ready

    def test_empty_dir(self, tmp_path):
        state = gating.inspect_token(tmp_path)
        assert state.status == "no_token"

    def test_ignores_non_token_files(self, tmp_path):
        (tmp_path / "oauth_states.json").write_text("{}")
        (tmp_path / "not-an-email.json").write_text("{}")
        state = gating.inspect_token(tmp_path)
        assert state.status == "no_token"

    def test_unreadable_token(self, tmp_path):
        (tmp_path / "user@example.com.json").write_text("{not json")
        state = gating.inspect_token(tmp_path)
        assert state.status == "unreadable"
        assert state.email == "user@example.com"

    def test_missing_refresh_token(self, tmp_path):
        write_token(tmp_path, refresh_token=None)
        state = gating.inspect_token(tmp_path)
        assert state.status == "no_refresh_token"

    def test_missing_scopes(self, tmp_path):
        write_token(tmp_path, scopes=["https://www.googleapis.com/auth/documents"])
        state = gating.inspect_token(tmp_path)
        assert state.status == "missing_scopes"
        assert "drive" in state.detail

    def test_scope_hierarchy_accepted(self, tmp_path):
        # Full drive + docs write imply the readonly/file scopes.
        write_token(
            tmp_path,
            scopes=[
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "openid",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        assert gating.inspect_token(tmp_path).status == "ok"

    def test_good_token_decodes_urlencoded_email(self, tmp_path):
        write_token(tmp_path, email="jonathan+e2e@example.com")
        state = gating.inspect_token(tmp_path)
        assert state.status == "ok"
        assert state.ready
        assert state.email == "jonathan+e2e@example.com"
        assert state.token_path is not None


class TestSkipReason:
    @pytest.mark.parametrize(
        "status",
        [
            "no_credentials_dir",
            "no_token",
            "unreadable",
            "no_refresh_token",
            "missing_scopes",
            "refresh_failed",
        ],
    )
    def test_actionable_for_every_status(self, tmp_path, status):
        state = gating.TokenState(status=status, credentials_dir=tmp_path)
        reason = state.skip_reason()
        assert reason.startswith("E2E SKIPPED")
        assert str(tmp_path) in reason
        assert "bootstrap_auth.py" in reason
        assert "pending_for_human.md" in reason
        assert "never starts an interactive OAuth flow" in reason


class TestPrepareCredentials:
    def test_valid_token_skips_refresh(self, tmp_path):
        write_token(tmp_path)
        state = gating.inspect_token(tmp_path)
        calls = []
        state, creds = gating.prepare_credentials(
            state, refresher=lambda c: calls.append(c)
        )
        assert state.ready
        assert creds is not None
        assert calls == []

    def test_expired_token_refreshes(self, tmp_path):
        write_token(
            tmp_path,
            expiry=_utc_iso(timedelta(hours=-1)),
        )
        state = gating.inspect_token(tmp_path)
        calls = []
        state, creds = gating.prepare_credentials(
            state, refresher=lambda c: calls.append(c)
        )
        assert state.ready
        assert len(calls) == 1
        assert creds is calls[0]

    def test_refresh_failure_gates_with_instructions(self, tmp_path):
        write_token(
            tmp_path,
            expiry=_utc_iso(timedelta(hours=-1)),
        )
        state = gating.inspect_token(tmp_path)

        def boom(_creds):
            raise RuntimeError("invalid_grant: Token has been revoked")

        state, creds = gating.prepare_credentials(state, refresher=boom)
        assert state.status == "refresh_failed"
        assert creds is None
        assert "invalid_grant" in state.detail
        assert "bootstrap_auth.py" in state.skip_reason()

    def test_not_ready_state_passes_through(self, tmp_path):
        state = gating.TokenState(status="no_token", credentials_dir=tmp_path)
        out_state, creds = gating.prepare_credentials(
            state, refresher=lambda c: pytest.fail("must not refresh")
        )
        assert out_state is state
        assert creds is None
