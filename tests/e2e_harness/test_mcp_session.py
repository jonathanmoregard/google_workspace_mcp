"""Unit tests for e2e.mcp_session server-spawn construction (pure parts)."""

from e2e.mcp_session import build_server_args, build_server_env


class TestBuildServerArgs:
    def test_stdio_single_user_docs_and_docs_preview(self):
        assert build_server_args() == [
            "main.py",
            "--transport",
            "stdio",
            "--single-user",
            "--tools",
            "docs",
            "docs_preview",
        ]

    def test_extra_services_appended(self):
        assert build_server_args(("docs_preview", "drive"))[-2:] == [
            "docs_preview",
            "drive",
        ]


class TestBuildServerEnv:
    def test_pins_credentials_dir_and_email(self):
        env = build_server_env("/tmp/creds", "user@example.com", base_env={})
        assert env["WORKSPACE_MCP_CREDENTIALS_DIR"] == "/tmp/creds"
        assert env["USER_GOOGLE_EMAIL"] == "user@example.com"

    def test_strips_mode_flipping_host_vars(self):
        hostile = {
            "WORKSPACE_MCP_TOOLS": "gmail",
            "WORKSPACE_MCP_TOOL_TIER": "core",
            "WORKSPACE_MCP_READ_ONLY": "true",
            "WORKSPACE_MCP_PERMISSIONS": "gmail:full",
            "WORKSPACE_MCP_TRANSPORT": "streamable-http",
            "WORKSPACE_MCP_CREDENTIALS_DIR": "/somewhere/else",
            "GOOGLE_MCP_CREDENTIALS_DIR": "/legacy",
            "MCP_SINGLE_USER_MODE": "0",
            "MCP_ENABLE_OAUTH21": "true",
            "USER_GOOGLE_EMAIL": "other@example.com",
            "PATH": "/usr/bin",
            "HOME": "/home/x",
        }
        env = build_server_env("/tmp/creds", "user@example.com", base_env=hostile)
        for var in (
            "WORKSPACE_MCP_TOOLS",
            "WORKSPACE_MCP_TOOL_TIER",
            "WORKSPACE_MCP_READ_ONLY",
            "WORKSPACE_MCP_PERMISSIONS",
            "WORKSPACE_MCP_TRANSPORT",
            "GOOGLE_MCP_CREDENTIALS_DIR",
            "MCP_SINGLE_USER_MODE",
            "MCP_ENABLE_OAUTH21",
        ):
            assert var not in env, var
        # Pinned, not inherited:
        assert env["WORKSPACE_MCP_CREDENTIALS_DIR"] == "/tmp/creds"
        assert env["USER_GOOGLE_EMAIL"] == "user@example.com"
        # Subprocess env is a full replacement - essentials must survive.
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/x"
