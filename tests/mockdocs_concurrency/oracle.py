"""A scripted agent, for validating interference scenarios without an LLM.

An interference scenario proves nothing until two things are true: a correct
solution scores 1.0, and a solution that ignores the interference FAILS. This
module is what makes that checkable.

The scripted agent is deliberately not a re-implementation of anything. It
drives the same :class:`~mockdocs.fake_services.FakeBackend` through the same
``mockdocs.adapter`` call shapes the real MCP tools use, wrapped in the same
:meth:`~mockdocs.concurrency.InterferenceEngine.around` that the FastMCP
middleware uses in a live run. So a scenario validated here is validated
against the machinery that will actually run it -- the only thing swapped out
is the model.

The end state is round-tripped through ``mockdocs.state`` before grading,
because that is what the harness does: the grader never sees the live backend,
only a snapshot of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from mockdocs.adapter import utf16_offsets
from mockdocs.concurrency import InterferenceEngine, find_clusters
from mockdocs.fake_services import FakeBackend
from mockdocs.graphemes import split_graphemes, utf16_len
from mockdocs.state import dump_backend, load_backend

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "llmux" / "interference"


def api_range(doc: Any, needle: str) -> tuple[int, int]:
    """UTF-16 ``(start, end)`` of ``needle`` in the CURRENT model state."""
    start = find_clusters(doc, needle)
    offsets = utf16_offsets(doc.chars)
    return offsets[start], offsets[start + len(split_graphemes(needle))]


def api_range_in_payload(payload: dict[str, Any], needle: str) -> tuple[int, int]:
    """UTF-16 ``(start, end)`` of ``needle`` in a ``documents.get`` RESPONSE.

    The honest version, and the one the stale-index oracle needs: an agent
    computes its indexes from the payload it was handed, not from the live
    document. Holding on to a range derived from an earlier response is
    exactly what goes wrong when somebody else edits in between.
    """
    tabs = payload.get("tabs") or []
    content = (
        tabs[0]["documentTab"]["body"]["content"]
        if tabs
        else payload["body"]["content"]
    )
    base: Optional[int] = None
    text = ""
    for element in content:
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run is None:
                continue
            if base is None:
                base = run["startIndex"]
            text += text_run["content"]
    if base is None:  # pragma: no cover - empty document
        raise AssertionError("payload carries no text runs")
    position = text.index(needle)
    start = base + utf16_len(text[:position])
    return start, start + utf16_len(needle)


class ScriptedAgent:
    """Makes the API calls a tool would make, under the interference engine."""

    def __init__(
        self, backend: FakeBackend, engine: InterferenceEngine, document_id: str
    ):
        self.backend = backend
        self.engine = engine
        self.document_id = document_id
        self.docs = backend.docs_service()

    # -- reads ------------------------------------------------------------
    def _read(self, tool: str) -> dict[str, Any]:
        return self.engine.around(
            tool,
            {"document_id": self.document_id},
            lambda: (
                self.docs.documents()
                .get(
                    documentId=self.document_id,
                    suggestionsViewMode="SUGGESTIONS_INLINE",
                    includeTabsContent=True,
                    commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
                )
                .execute()
            ),
        )

    def list_suggestions(self) -> dict[str, Any]:
        return self._read("list_document_suggestions")

    def list_comments(self) -> dict[str, Any]:
        return self._read("list_document_comments")

    def review_view(self) -> dict[str, Any]:
        return self._read("get_doc_review_view")

    # -- writes -----------------------------------------------------------
    def _batch(self, tool: str, args: dict[str, Any], body: dict[str, Any]) -> Any:
        return self.engine.around(
            tool,
            args,
            lambda: (
                self.docs.documents()
                .batchUpdate(documentId=self.document_id, body=body)
                .execute()
            ),
        )

    def resolve(self, suggestion_id: str, action: str) -> Any:
        key = "acceptSuggestion" if action == "accept" else "rejectSuggestion"
        return self._batch(
            "manage_document_suggestion",
            {
                "document_id": self.document_id,
                "action": action,
                "suggestion_id": suggestion_id,
            },
            {"requests": [{key: {"suggestionId": suggestion_id}}]},
        )

    def suggest(
        self,
        start_index: int,
        end_index: Optional[int] = None,
        text: Optional[str] = None,
    ) -> Any:
        """``suggest_doc_edit``: insertion, deletion or replacement.

        The request decomposition mirrors ``gdocs_preview.write_tools`` --
        a replacement is deleteContentRange then insertText at start_index,
        in one SUGGEST batch.
        """
        requests: list[dict[str, Any]] = []
        if end_index is not None:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": start_index, "endIndex": end_index}
                    }
                }
            )
        if text is not None:
            requests.append(
                {"insertText": {"location": {"index": start_index}, "text": text}}
            )
        args: dict[str, Any] = {
            "document_id": self.document_id,
            "start_index": start_index,
        }
        if end_index is not None:
            args["end_index"] = end_index
        if text is not None:
            args["text"] = text
        return self._batch(
            "suggest_doc_edit",
            args,
            {"requests": requests, "writeControl": {"writeMode": "SUGGEST"}},
        )

    def reply(
        self,
        content: str,
        *,
        comment_id: Optional[str] = None,
        suggestion_id: Optional[str] = None,
    ) -> Any:
        post: dict[str, Any] = {"post": {"content": content}}
        args: dict[str, Any] = {
            "document_id": self.document_id,
            "reply_content": content,
        }
        if comment_id is not None:
            post["commentId"] = comment_id
            args["comment_id"] = comment_id
        else:
            post["suggestionId"] = suggestion_id
            args["suggestion_id"] = suggestion_id
        return self._batch(
            "reply_to_doc_thread", args, {"requests": [{"addCommentReply": post}]}
        )

    # -- convenience ------------------------------------------------------
    @property
    def doc(self) -> Any:
        return self.backend.documents[self.document_id]

    def pending_by(self, author: str) -> list[str]:
        return sorted(sid for sid, s in self.doc.registry.items() if s.author == author)


def build(scenario_id: str) -> tuple[FakeBackend, InterferenceEngine, ScriptedAgent]:
    """Seed a backend from a corpus scenario and arm its interferences."""
    from llmux.runner.interference import declared_interferences
    from llmux.runner.scenarios import load_scenario

    scenario = load_scenario(CORPUS / scenario_id)
    backend = FakeBackend(me=scenario.me)
    backend.seed(scenario.seed)
    engine = InterferenceEngine(backend, declared_interferences(scenario))
    document_id = scenario.expected["document_id"]
    return backend, engine, ScriptedAgent(backend, engine, document_id)


def grade(scenario_id: str, backend: FakeBackend) -> dict[str, Any]:
    """Grade an end state exactly as the runner would: via a state snapshot."""
    from llmux.runner.scenarios import load_grader, load_scenario

    scenario = load_scenario(CORPUS / scenario_id)
    return load_grader(scenario)(load_backend(dump_backend(backend)))
