"""Smoke test: the real MCP server, over real stdio, backed by the mock.

Spawns ``python mockdocs/serve.py --transport stdio --single-user --tools docs
docs_preview`` as a subprocess and drives it with the fastmcp client, exactly
as ``e2e/mcp_session.py`` drives ``main.py``. Nothing is mocked in-process:
this exercises tool registration, the MCP protocol, argument validation and
JSON serialisation. The only difference from a live run is that the service
objects behind ``@require_google_service`` are the in-memory fakes.

This is the gate for the later chunks: if a headless agent is going to drive
these tools against the mock, the mock has to be reachable over the wire the
same way the real server is.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

fastmcp = pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

SEED = {
    "me": "alice",
    "documents": [
        {
            "document_id": "seeded-doc",
            "title": "Seeded Review Doc",
            # Astral-plane emoji on purpose: UTF-16 indexes diverge from
            # Python code-point arithmetic from here on.
            "text": "Hello 🎉 world.\nSecond paragraph here.\n",
            # Seed ops are applied in order and use MODEL (grapheme) indices,
            # so the delete below is relative to the document *after* bob's
            # insertion: "Second " starts at grapheme 21 by then.
            "suggestions": [
                {"op": "insert", "index": 5, "text": " brave", "author": "bob"},
                {"op": "delete", "start": 21, "end": 28, "author": "carol"},
            ],
            "comments": [{"content": "please review", "quote": "Hello"}],
        }
    ],
}


class _Session:
    """Owns the server subprocess plus a client on a background event loop."""

    def __init__(
        self, seed_path: Path, env_extra: dict[str, str] | None = None
    ) -> None:
        self.seed_path = seed_path
        self.env_extra = env_extra or {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Client | None = None

    def __enter__(self) -> "_Session":
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mockdocs-loop", daemon=True
        )
        self._thread.start()

        env = dict(os.environ)
        for var in (
            "WORKSPACE_MCP_TOOLS",
            "WORKSPACE_MCP_TOOL_TIER",
            "WORKSPACE_MCP_READ_ONLY",
            "WORKSPACE_MCP_PERMISSIONS",
            "WORKSPACE_MCP_TRANSPORT",
            "MCP_ENABLE_OAUTH21",
        ):
            env.pop(var, None)
        env["MOCKDOCS_SEED"] = str(self.seed_path)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.update(self.env_extra)

        transport = StdioTransport(
            command=sys.executable,
            args=[
                "mockdocs/serve.py",
                "--transport",
                "stdio",
                "--single-user",
                "--tools",
                "docs",
                "docs_preview",
            ],
            env=env,
            cwd=str(REPO_ROOT),
        )
        self._client = Client(transport, init_timeout=120)
        self._run(self._client.__aenter__(), 120)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._client is not None:
            try:
                self._run(self._client.__aexit__(None, None, None), 30)
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self, coro: Any, timeout: float) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def tools(self) -> list[str]:
        return sorted(t.name for t in self._run(self._client.list_tools(), 30))

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._run(self._client.call_tool(name, arguments, timeout=60), 90)
        return "\n".join(b.text for b in result.content if getattr(b, "text", None))

    def call_json(self, name: str, arguments: dict[str, Any]) -> Any:
        text = self.call(name, arguments)
        try:
            return json.loads(text)
        except ValueError as exc:  # pragma: no cover - failure diagnostics
            raise AssertionError(f"tool did not return JSON: {text[:400]!r}") from exc


@pytest.fixture(scope="module")
def seed_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("mockdocs") / "seed.json"
    path.write_text(json.dumps(SEED), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def session(seed_file):
    with _Session(seed_file) as s:
        yield s


EMAIL = "alice@example.com"


def test_server_registers_the_review_tools(session):
    names = session.tools()
    for tool in (
        "get_doc_review_view",
        "list_document_suggestions",
        "check_docs_review_capabilities",
        "suggest_doc_edit",
        "manage_document_suggestion",
        "reply_to_doc_thread",
        "create_anchored_doc_comment",
    ):
        assert tool in names, f"{tool} missing from {names}"


def test_get_doc_review_view_against_the_seeded_document(session):
    view = session.call_json(
        "get_doc_review_view",
        {"user_google_email": EMAIL, "document_id": "seeded-doc"},
    )
    assert view["view_mode"] == "SUGGESTIONS_INLINE"
    assert view["title"] == "Seeded Review Doc"
    # bob's insertion and carol's deletion both render as CriticMarkup.
    assert "{+ brave+}" in view["body_text"]
    assert "{-" in view["body_text"]
    assert len(view["suggestion_ids"]) == 2


def test_clean_view_modes_round_trip(session):
    original = session.call_json(
        "get_doc_review_view",
        {
            "user_google_email": EMAIL,
            "document_id": "seeded-doc",
            "view_mode": "PREVIEW_WITHOUT_SUGGESTIONS",
        },
    )
    assert original["body_text"] == "Hello 🎉 world.\nSecond paragraph here.\n"
    assert original["suggestion_ids"] == []

    accepted = session.call_json(
        "get_doc_review_view",
        {
            "user_google_email": EMAIL,
            "document_id": "seeded-doc",
            "view_mode": "PREVIEW_SUGGESTIONS_ACCEPTED",
        },
    )
    assert accepted["body_text"] == "Hello brave 🎉 world.\nparagraph here.\n"


def test_list_document_suggestions_reports_authors_and_utf16_indexes(session):
    result = session.call_json(
        "list_document_suggestions",
        {"user_google_email": EMAIL, "document_id": "seeded-doc"},
    )
    assert result["suggestion_count"] == 2
    by_author = {r["author"]["display_name"]: r for r in result["suggestions"]}
    assert set(by_author) == {"bob", "carol"}

    bob = by_author["bob"]
    assert bob["type"] == "insertion"
    assert bob["post_text"] == " brave"
    assert bob["author"]["me"] is False

    carol = by_author["carol"]
    assert carol["type"] == "deletion"
    assert carol["pre_text"] == "Second "
    # The deletion sits after the emoji, so its UTF-16 start index is exactly
    # one greater than Python code-point arithmetic would give (grapheme 21 ->
    # UTF-16 23, not 22). This is the index discipline the mock exists to test.
    assert carol["start_index"] == 23


def test_suggest_then_accept_over_mcp(session):
    """Full write round trip: create a suggestion, see it listed, accept it,
    see the document change."""
    created = session.call_json(
        "suggest_doc_edit",
        {
            "user_google_email": EMAIL,
            "document_id": "seeded-doc",
            "start_index": 1,
            "text": "PREFIX ",
        },
    )
    assert created["mode"] == "insertion"
    (sid,) = created["created_suggestion_ids"]

    listed = session.call_json(
        "list_document_suggestions",
        {"user_google_email": EMAIL, "document_id": "seeded-doc"},
    )
    assert sid in {r["suggestion_id"] for r in listed["suggestions"]}

    accepted = session.call_json(
        "manage_document_suggestion",
        {
            "user_google_email": EMAIL,
            "document_id": "seeded-doc",
            "action": "accept",
            "suggestion_id": sid,
        },
    )
    assert accepted["accepted_suggestion_ids"] == [sid]

    view = session.call_json(
        "get_doc_review_view",
        {
            "user_google_email": EMAIL,
            "document_id": "seeded-doc",
            "view_mode": "PREVIEW_WITHOUT_SUGGESTIONS",
        },
    )
    assert view["body_text"].startswith("PREFIX Hello")


def test_capabilities_probe_reports_preview_available(session):
    report = session.call_json(
        "check_docs_review_capabilities",
        {"user_google_email": EMAIL, "document_id": "seeded-doc", "probe": True},
    )
    assert report["preview"]["availability"] == "available"
    assert report["tools"]["total"] == 7


def test_not_enrolled_server_surfaces_an_actionable_error(seed_file):
    """The not-enrolled flag must reach the client as the enrollment message
    ``write_tools`` raises, not as an opaque 400."""
    with _Session(seed_file, env_extra={"MOCKDOCS_NOT_ENROLLED": "1"}) as session:
        result = session._run(
            session._client.call_tool(
                "suggest_doc_edit",
                {
                    "user_google_email": EMAIL,
                    "document_id": "seeded-doc",
                    "start_index": 1,
                    "text": "nope",
                },
                timeout=60,
                raise_on_error=False,
            ),
            90,
        )
        assert result.is_error
        text = "\n".join(b.text for b in result.content if getattr(b, "text", None))
        assert "Developer Preview" in text

        report = session.call_json(
            "check_docs_review_capabilities",
            {"user_google_email": EMAIL, "document_id": "seeded-doc", "probe": True},
        )
        assert report["preview"]["availability"] == "unavailable"
        assert report["preview"]["evidence"]["reason"] == "not_enrolled"
