"""Oracle steps: the intended solution, one MCP tool call per step.

A step does three things when it is *emitted*, and doing all three in one
pass is the point:

1. reads the mirror model to compute the call's arguments -- including the
   UTF-16 conversion the agent under test has to get right, and including
   indexes that only exist *after* the previous steps ran (accepting a
   deletion physically removes characters, so a step's arguments cannot be
   precomputed);
2. applies the equivalent operation to the mirror, so the next step sees the
   document the agent would see;
3. returns the serialisable call for ``solution.json`` plus any grading
   expectation the step creates (a reply that must exist, a comment whose
   quote pins the anchor arithmetic).

The emitted calls are replayed through the real tool functions by
:mod:`llmux.scenarios.oracle`; the mirror is what ``expected.json`` is read
off. The generator cross-checks the two, so a bug in either path is a build
failure rather than a corrupt corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from mockdocs.adapter import utf16_offsets
from mockdocs.fake_services import FakeBackend
from mockdocs.model import MockDoc

from llmux.scenarios.primitives import Locator, ScenarioError


@dataclass
class OracleContext:
    """The mirror a step reads and mutates while it is being emitted."""

    backend: FakeBackend
    document_id: str

    @property
    def doc(self) -> MockDoc:
        return self.backend.get_document(self.document_id)

    def utf16(self, grapheme_index: int) -> int:
        """Grapheme index -> the UTF-16 code-unit index the API speaks."""
        return utf16_offsets(self.doc.chars)[grapheme_index]


Emission = tuple[dict[str, Any], list[dict[str, Any]]]


class Step:
    """One tool call. ``emit`` returns ``(call, thread_expectations)``."""

    def emit(self, ctx: OracleContext) -> Emission:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def decision(self) -> Optional[tuple[str, str]]:
        """``(suggestion_id, "accepted"|"rejected")`` for resolution steps."""
        return None


@dataclass
class Accept(Step):
    """§7 accept via ``manage_document_suggestion``."""

    suggestion_id: str

    def emit(self, ctx: OracleContext) -> Emission:
        if self.suggestion_id not in ctx.doc.registry:
            raise ScenarioError(
                f"oracle would accept {self.suggestion_id}, which is no longer live "
                f"(a previous step garbage-collected it; reorder the solution)"
            )
        ctx.doc.accept(self.suggestion_id)
        return (
            {
                "tool": "manage_document_suggestion",
                "args": {"action": "accept", "suggestion_id": self.suggestion_id},
            },
            [],
        )

    @property
    def decision(self) -> Optional[tuple[str, str]]:
        return (self.suggestion_id, "accepted")


@dataclass
class Reject(Step):
    """§7 reject via ``manage_document_suggestion``."""

    suggestion_id: str

    def emit(self, ctx: OracleContext) -> Emission:
        if self.suggestion_id not in ctx.doc.registry:
            raise ScenarioError(
                f"oracle would reject {self.suggestion_id}, which is no longer live "
                f"(a previous step garbage-collected it; reorder the solution)"
            )
        ctx.doc.reject(self.suggestion_id)
        return (
            {
                "tool": "manage_document_suggestion",
                "args": {"action": "reject", "suggestion_id": self.suggestion_id},
            },
            [],
        )

    @property
    def decision(self) -> Optional[tuple[str, str]]:
        return (self.suggestion_id, "rejected")


@dataclass
class ReplyToSuggestion(Step):
    """``reply_to_doc_thread(suggestion_id=...)``.

    The expectation is only gradeable while the suggestion is still live:
    §7 deletes the registry entry, and the thread with it (§10 recommends a
    resolved-comments log; the mock does not keep one). The generator drops
    expectations for suggestions the solution goes on to resolve.
    """

    suggestion_id: str
    content: str
    content_regex: Optional[str] = None

    def emit(self, ctx: OracleContext) -> Emission:
        if self.suggestion_id not in ctx.doc.registry:
            raise ScenarioError(
                f"oracle would reply to {self.suggestion_id}, which is not live"
            )
        ctx.backend.add_suggestion_reply(ctx.doc, self.suggestion_id, self.content)
        expectation: dict[str, Any] = {
            "kind": "suggestion_reply",
            "suggestion_id": self.suggestion_id,
            "author": ctx.backend.me,
            "min_count": 1,
        }
        if self.content_regex:
            expectation["content_regex"] = self.content_regex
        return (
            {
                "tool": "reply_to_doc_thread",
                "args": {
                    "suggestion_id": self.suggestion_id,
                    "reply_content": self.content,
                },
            },
            [expectation],
        )


@dataclass
class ReplyToComment(Step):
    """``reply_to_doc_thread(comment_id=...)`` on a seeded comment thread."""

    comment_index: int
    content: str
    content_contains: Optional[str] = None

    def emit(self, ctx: OracleContext) -> Emission:
        threads = ctx.backend.comments.get(ctx.document_id) or []
        if self.comment_index >= len(threads):
            raise ScenarioError(
                f"comment index {self.comment_index} out of range ({len(threads)})"
            )
        thread = threads[self.comment_index]
        comment_id = thread["commentId"]
        ctx.backend.add_comment_reply(ctx.document_id, comment_id, self.content)
        expectation = {
            "kind": "comment_thread",
            "quote": thread["plainTextQuote"],
            "reply_contains": self.content_contains or self.content,
            "min_count": 1,
        }
        return (
            {
                "tool": "reply_to_doc_thread",
                "args": {"comment_id": comment_id, "reply_content": self.content},
            },
            [expectation],
        )


@dataclass
class AnchorComment(Step):
    """``create_anchored_doc_comment`` over a computed range.

    The recorded expectation is the *quote* the backend derives from the
    range, not the range itself: that is what makes this a UTF-16 arithmetic
    check with a single right answer, and it stays meaningful however the
    agent arrived at the indexes.
    """

    target: Locator
    content: str
    content_contains: Optional[str] = None

    def emit(self, ctx: OracleContext) -> Emission:
        start, end = self.target.resolve(ctx.doc)
        if start == end:
            raise ScenarioError("anchored comments need a non-empty range")
        quote = MockDoc.text_of(ctx.doc.chars[start:end])
        offsets = utf16_offsets(ctx.doc.chars)
        ctx.backend.create_comment_thread(
            ctx.document_id, content=self.content, quote=quote
        )
        expectation = {
            "kind": "comment_thread",
            "quote": quote,
            "content_contains": self.content_contains or self.content,
            "min_count": 1,
        }
        return (
            {
                "tool": "create_anchored_doc_comment",
                "args": {
                    "content": self.content,
                    "start_index": offsets[start],
                    "end_index": offsets[end],
                },
            },
            [expectation],
        )


@dataclass
class SuggestEdit(Step):
    """``suggest_doc_edit`` -- a new pending suggestion by the agent.

    ``mode`` is inferred exactly as the tool infers it: text only ->
    insertion, range only -> deletion, both -> replacement.
    """

    target: Locator
    text: Optional[str] = None
    delete: bool = False

    def emit(self, ctx: OracleContext) -> Emission:
        start, end = self.target.resolve(ctx.doc)
        offsets = utf16_offsets(ctx.doc.chars)
        author = ctx.backend.me
        args: dict[str, Any] = {"start_index": offsets[start]}
        if self.delete and self.text is not None:
            args["end_index"] = offsets[end]
            args["text"] = self.text
            ctx.doc.replace(start, end, self.text, author)
        elif self.delete:
            args["end_index"] = offsets[end]
            ctx.doc.delete(start, end, author)
        elif self.text is not None:
            args["text"] = self.text
            ctx.doc.insert(start, self.text, author)
        else:
            raise ScenarioError("SuggestEdit needs text, delete=True, or both")
        return ({"tool": "suggest_doc_edit", "args": args}, [])
