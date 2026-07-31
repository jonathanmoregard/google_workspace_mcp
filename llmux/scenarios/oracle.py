"""Replay a scenario's ``solution.json`` through the real MCP tools.

This is what makes "solvable" a claim rather than a hope. The oracle calls
the same tool functions the agent under test calls -- ``suggest_doc_edit``,
``manage_document_suggestion``, ``reply_to_doc_thread``,
``create_anchored_doc_comment`` -- against a :class:`FakeBackend`, so a
scenario that cannot be expressed on the current tool surface fails to
build instead of quietly becoming an impossible task.

The tools are unwrapped past ``@server.tool`` / ``@handle_http_errors`` /
``@require_google_service`` and handed a mock ``service`` directly, which is
the same seam ``mockdocs/serve.py`` patches. Errors therefore propagate as
``HttpError`` / ``UserInputError`` rather than being formatted into a
string, which is what we want: a solution step that 400s must fail the
build loudly.

``solution.json`` is the oracle's, not the agent's. It names suggestion ids
and precomputed indexes; a runner that showed it to the model under test
would be measuring nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

#: Any email works -- the mock never authenticates. Kept constant so
#: transcripts are diffable.
ORACLE_EMAIL = "reviewer@example.com"


def _unwrap(tool: Any) -> Callable[..., Any]:
    """Strip the FastMCP tool object and the decorator stack."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def tool_table() -> dict[str, Callable[..., Any]]:
    """The tool surface under test, callable with an injected service.

    Imported lazily: importing the tool modules constructs the FastMCP
    server, which scenario *definition* has no need of.
    """
    from gdocs_preview import curated_tools, write_tools

    return {
        "suggest_doc_edit": _unwrap(write_tools.suggest_doc_edit),
        "manage_document_suggestion": _unwrap(write_tools.manage_document_suggestion),
        "reply_to_doc_thread": _unwrap(write_tools.reply_to_doc_thread),
        "create_anchored_doc_comment": _unwrap(write_tools.create_anchored_doc_comment),
        "list_document_suggestions": _unwrap(curated_tools.list_document_suggestions),
        "get_doc_review_view": _unwrap(curated_tools.get_doc_review_view),
    }


async def _run(
    backend: Any, document_id: str, calls: list[dict[str, Any]]
) -> list[str]:
    tools = tool_table()
    outputs: list[str] = []
    for i, call in enumerate(calls):
        name = call["tool"]
        if name not in tools:
            raise KeyError(f"solution step {i} names unknown tool {name!r}")
        try:
            outputs.append(
                await tools[name](
                    service=backend.docs_service(),
                    user_google_email=ORACLE_EMAIL,
                    document_id=document_id,
                    **call.get("args", {}),
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"oracle step {i} ({name} {call.get('args')}) failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return outputs


def run_solution(
    backend: Any, document_id: str, calls: list[dict[str, Any]]
) -> list[str]:
    """Execute every call in order; raises on the first failure."""
    return asyncio.run(_run(backend, document_id, calls))
