"""No-creds blackbox smoke: the server spawns and completes the MCP handshake.

Tool registration needs no Google token (only tool CALLS do), so this
runs everywhere - it catches harness/handshake regressions long before
credentials exist.
"""

from e2e.mcp_session import ServerSession

# The hand-written curated surface. Native suggestion/comment tools are
# added by the native-integration redirect (R4), which also restores an
# exact-count assertion here.
EXPECTED_TOOLS = {
    "docs_review_capabilities",
    "docs_review_list_suggestions",
    "docs_review_read_document",
}


def test_stdio_server_registers_docs_preview_surface(tmp_path):
    session = ServerSession(
        credentials_dir=str(tmp_path), user_email="e2e-smoke@example.com"
    )
    session.start()
    try:
        names = set(session.list_tool_names())
    finally:
        session.stop()

    missing = EXPECTED_TOOLS - names
    assert not missing, f"tools missing from --tools docs_preview: {missing}"
