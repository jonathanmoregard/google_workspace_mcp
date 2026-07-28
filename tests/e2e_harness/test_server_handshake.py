"""No-creds blackbox smoke: the server spawns and completes the MCP handshake.

Tool registration needs no Google token (only tool CALLS do), so this
runs everywhere - it catches harness/handshake regressions long before
credentials exist.
"""

from e2e.mcp_session import ServerSession

EXPECTED_TOOLS = {
    # generated - Docs methods + batchUpdate members
    "docs_api_documents_create",
    "docs_api_documents_get",
    "docs_api_documents_batch_update",
    "docs_api_insert_text",
    # generated - preview overlay
    "docs_api_accept_suggestion",
    "docs_api_reject_suggestion",
    "docs_api_insert_comment",
    "docs_api_add_comment_reply",
    # generated - Drive v3 comments/replies
    "drive_api_comments_create",
    "drive_api_comments_list",
    "drive_api_replies_create",
    "drive_api_comments_delete",
    # curated
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
    # 61 generated + 3 curated; upstream may add a couple of auth helpers.
    assert len(names) >= 64, f"expected >=64 tools, got {len(names)}"
