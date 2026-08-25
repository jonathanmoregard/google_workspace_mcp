"""No-creds blackbox smoke: the server spawns and completes the MCP handshake.

Tool registration needs no Google token (only tool CALLS do), so this
runs everywhere - it catches harness/handshake regressions long before
credentials exist.
"""

from e2e.mcp_session import ServerSession

# The full hand-written docs_preview surface (3 curated read/diagnostic
# tools + 4 native write tools) - exactly these, nothing else.
DOCS_PREVIEW_TOOLS = {
    "list_document_suggestions",
    "get_doc_review_view",
    "check_docs_review_capabilities",
    "suggest_doc_edit",
    "manage_document_suggestion",
    "reply_to_doc_thread",
    "create_anchored_doc_comment",
}

#: Always-registered core tools, present regardless of --tools selection.
#: Both live in core/server.py, which every spawn imports.
CORE_TOOLS = {"start_google_auth", "list_google_accounts"}

#: GA docs-service tools the e2e scenarios rely on. Presence-only: the
#: docs service's total surface may drift with upstream, so no exact
#: count there.
DOCS_SERVICE_TOOLS_USED = {
    "create_doc",
    "modify_doc_text",
    "list_document_comments",
    "manage_document_comment",
}


def _tool_names(tmp_path, tools=None) -> set[str]:
    kwargs = {} if tools is None else {"tools": tools}
    session = ServerSession(
        credentials_dir=str(tmp_path), user_email="e2e-smoke@example.com", **kwargs
    )
    session.start()
    try:
        return set(session.list_tool_names())
    finally:
        session.stop()


def test_docs_preview_service_registers_exactly_its_seven_tools(tmp_path):
    """Exact count for the docs_preview service: the 7 review tools and
    nothing else (modulo the always-on core auth tool)."""
    names = _tool_names(tmp_path, tools=("docs_preview",))
    assert names - CORE_TOOLS == DOCS_PREVIEW_TOOLS


def test_default_spawn_registers_review_and_docs_surfaces(tmp_path):
    """The suite's spawn args (--tools docs docs_preview) must expose the
    review surface plus every docs-service tool the scenarios call."""
    names = _tool_names(tmp_path)
    missing = (DOCS_PREVIEW_TOOLS | DOCS_SERVICE_TOOLS_USED) - names
    assert not missing, f"tools missing from --tools docs docs_preview: {missing}"
