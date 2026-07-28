"""No-creds blackbox smoke: the server spawns and completes the MCP handshake.

Tool registration needs no Google token (only tool CALLS do), so this
runs everywhere - it catches harness/handshake regressions long before
credentials exist.
"""

from e2e.mcp_session import ServerSession

# The full hand-written docs_preview surface (3 curated read/diagnostic
# tools + 4 native write tools). The e2e retarget (R4) restores an
# exact-count assertion here.
EXPECTED_TOOLS = {
    "list_document_suggestions",
    "get_doc_review_view",
    "check_docs_review_capabilities",
    "suggest_doc_edit",
    "manage_document_suggestion",
    "reply_to_doc_thread",
    "create_anchored_doc_comment",
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
