"""Duck-typed ``docs`` and ``drive`` service objects backed by the mock.

These match the googleapiclient call shapes the repo's tools actually use --
``service.documents().get(...).execute()``,
``service.documents().batchUpdate(documentId=..., body=...).execute()``,
``service.comments().list(...).execute()``, and so on -- so they can be
injected wherever ``@require_google_service`` would inject a real Resource.
Nothing here talks to the network and no credentials are involved.

One store backs both comment surfaces: a comment created through the preview
``insertComment`` request is visible to the GA Drive comment tools, and vice
versa. That is how the real product behaves (there is only one comment on the
document) and it is what makes cross-surface tool ergonomics testable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from mockdocs.adapter import (
    apply_batch_update,
    document_payload,
    http_error,
    tabs_document_payload,
)
from mockdocs.model import FOOTER, FOOTNOTE, HEADER, MockDoc, MockDocsError

_ISO = "2026-07-30T12:00:{:02d}.000Z"

#: ``google.apps.docs.v1.CommentsViewMode`` members the real API accepts
#: (verified 2026-07-30: anything else is a 400 naming the enum type).
_COMMENTS_VIEW_MODES = frozenset(
    {
        "COMMENTS_VIEW_MODE_UNSPECIFIED",
        "COMMENTS_VIEW_MODE_INCLUDED",
        "COMMENTS_VIEW_MODE_OMITTED",
    }
)


class _Call:
    """Stand-in for an ``HttpRequest``: only ``execute()`` is used."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn()


class FakeBackend:
    """One process-wide mock Google Workspace: documents, comments, identity.

    Args:
        me: author id used for suggestions and comments created through the
            tools ("the authenticated user").
        not_enrolled: simulate a caller without Workspace Developer Preview
            enrollment -- every preview request type then fails with a
            400-shaped ``Unknown name`` error, exercising
            ``gdocs_preview.preview_status.classify_preview_error``.
        fail_comment_updates: force ``commentUpdateState`` to
            ``ALL_FAILED_UNKNOWN_REASON`` so the partial-failure path in
            ``write_tools._execute_preview_batch_update`` is reachable.
    """

    def __init__(
        self,
        me: str = "mockuser",
        not_enrolled: bool = False,
        fail_comment_updates: bool = False,
    ) -> None:
        self.me = me
        self.not_enrolled = not_enrolled
        self.fail_comment_updates = fail_comment_updates
        self.documents: dict[str, MockDoc] = {}
        #: document id -> list of comment-thread records (shared surface).
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self._counters: dict[str, int] = {}

    # -- ids -------------------------------------------------------------
    def _next(self, kind: str) -> int:
        n = self._counters.get(kind, 0) + 1
        self._counters[kind] = n
        return n

    def _author_block(self, author: Optional[str] = None) -> dict[str, Any]:
        who = author or self.me
        return {
            "displayName": who,
            "me": who == self.me,
            "anonymous": False,
            "user": f"users/{who}",
        }

    # -- documents -------------------------------------------------------
    def add_document(
        self,
        text: str = "\n",
        document_id: Optional[str] = None,
        title: str = "Mock Document",
    ) -> MockDoc:
        if document_id is None:
            document_id = f"mockdoc-{self._next('doc')}"
        doc = MockDoc(text=text, document_id=document_id, title=title)
        self.documents[document_id] = doc
        self.comments.setdefault(document_id, [])
        return doc

    def get_document(self, document_id: str) -> MockDoc:
        doc = self.documents.get(document_id)
        if doc is None:
            raise http_error(
                404,
                f"Requested entity was not found: {document_id}.",
                reason="notFound",
            )
        return doc

    # -- comment threads (shared preview + Drive surface) -----------------
    def create_comment_thread(
        self,
        document_id: str,
        content: str,
        quote: str = "",
        assignee: Optional[str] = None,
        author: Optional[str] = None,
    ) -> dict[str, Any]:
        n = self._next("comment")
        stamp = _ISO.format(min(n, 59))
        # Shape verified against the live API 2026-07-30: a comment head post
        # carries content + contentHtml + author + times + commentAction.
        thread = {
            "commentId": f"comment.{n}",
            "anchorId": f"kix.anchor.{n}" if quote else "",
            "headPost": {
                "postId": f"post.{self._next('post')}",
                "content": content,
                "contentHtml": content,
                "author": self._author_block(author),
                "createTime": stamp,
                "updateTime": stamp,
                "commentAction": "NO_COMMENT_ACTION_CHANGE",
            },
            "replies": [],
            "status": "OPEN",
            "plainTextQuote": quote,
        }
        if assignee:
            thread["headPost"]["assigneeEmail"] = assignee
        self.comments.setdefault(document_id, []).append(thread)
        return thread

    def _find_thread(
        self, document_id: str, comment_id: Optional[str], request_index: int
    ) -> dict[str, Any]:
        for thread in self.comments.get(document_id, []):
            if thread["commentId"] == comment_id:
                return thread
        raise http_error(
            400,
            f"Invalid requests[{request_index}]: comment {comment_id} was not found.",
        )

    def add_comment_reply(
        self,
        document_id: str,
        comment_id: str,
        content: str,
        comment_action: Optional[str] = None,
        request_index: int = 0,
        author: Optional[str] = None,
    ) -> dict[str, Any]:
        thread = self._find_thread(document_id, comment_id, request_index)
        n = self._next("post")
        stamp = _ISO.format(min(n, 59))
        post = {
            "postId": f"post.{n}",
            "content": content,
            "contentHtml": content,
            "author": self._author_block(author),
            "createTime": stamp,
            "updateTime": stamp,
            "commentAction": "NO_COMMENT_ACTION_CHANGE",
        }
        if comment_action in ("RESOLVE", "REOPEN"):
            post["commentAction"] = comment_action
            thread["status"] = "RESOLVED" if comment_action == "RESOLVE" else "OPEN"
        thread["replies"].append(post)
        return post

    def add_suggestion_reply(
        self,
        doc: MockDoc,
        suggestion_id: str,
        content: str,
        author: Optional[str] = None,
    ) -> dict[str, Any]:
        from mockdocs.model import Comment

        sug = doc.registry[suggestion_id]
        n = self._next("post")
        sug.thread.append(
            Comment(
                post_id=f"post.{n}",
                author=author or self.me,
                content=content,
                created_at=doc._tick(),
            )
        )
        stamp = _ISO.format(min(n, 59))
        return {
            "postId": f"post.{n}",
            "content": content,
            "contentHtml": content,
            "author": self._author_block(author),
            "createTime": stamp,
            "updateTime": stamp,
            "suggestionAction": "NO_SUGGESTION_ACTION_CHANGE",
        }

    def update_post(
        self,
        doc: MockDoc,
        comment_id: Optional[str],
        suggestion_id: Optional[str],
        post_id: Optional[str],
        content: str,
        request_index: int = 0,
    ) -> None:
        if suggestion_id is not None:
            sug = doc.registry.get(suggestion_id)
            if sug is None:
                raise http_error(
                    400,
                    f"Invalid requests[{request_index}]: the suggestion ID "
                    f"{suggestion_id} is invalid.",
                )
            if post_id == f"{suggestion_id}.head":
                # 400 if the post is the headPost of a SuggestionThread.
                raise http_error(
                    400,
                    f"Invalid requests[{request_index}].updateCommentPost: the head "
                    f"post of a suggestion thread cannot be updated.",
                )
            for post in sug.thread:
                if post.post_id == post_id:
                    post.content = content
                    return
            raise http_error(
                400, f"Invalid requests[{request_index}]: post {post_id} was not found."
            )
        thread = self._find_thread(doc.document_id, comment_id, request_index)
        if thread["headPost"]["postId"] == post_id:
            thread["headPost"]["content"] = content
            return
        for post in thread["replies"]:
            if post["postId"] == post_id:
                post["content"] = content
                return
        raise http_error(
            400, f"Invalid requests[{request_index}]: post {post_id} was not found."
        )

    def delete_comment_thread(
        self, document_id: str, comment_id: Optional[str], request_index: int = 0
    ) -> None:
        thread = self._find_thread(document_id, comment_id, request_index)
        self.comments[document_id].remove(thread)

    def delete_reply(
        self,
        doc: MockDoc,
        comment_id: Optional[str],
        suggestion_id: Optional[str],
        post_id: Optional[str],
        request_index: int = 0,
    ) -> None:
        if suggestion_id is not None:
            sug = doc.registry.get(suggestion_id)
            if sug is None:
                raise http_error(
                    400,
                    f"Invalid requests[{request_index}]: the suggestion ID "
                    f"{suggestion_id} is invalid.",
                )
            for post in list(sug.thread):
                if post.post_id == post_id:
                    sug.thread.remove(post)
                    return
        else:
            thread = self._find_thread(doc.document_id, comment_id, request_index)
            for post in list(thread["replies"]):
                if post["postId"] == post_id:
                    thread["replies"].remove(post)
                    return
        raise http_error(
            400, f"Invalid requests[{request_index}]: post {post_id} was not found."
        )

    # -- Drive projection -------------------------------------------------
    def drive_comments(self, document_id: str) -> list[dict[str, Any]]:
        """The same threads in Drive ``comments.list`` shape."""
        out = []
        for thread in self.comments.get(document_id, []):
            head = thread["headPost"]
            record: dict[str, Any] = {
                "id": thread["commentId"],
                "content": head["content"],
                "author": {
                    "displayName": head["author"]["displayName"],
                    "me": head["author"]["me"],
                },
                "createdTime": head["createTime"],
                "modifiedTime": head["updateTime"],
                "resolved": thread["status"] == "RESOLVED",
                "replies": [
                    {
                        "id": p["postId"],
                        "content": p["content"],
                        "author": {"displayName": p["author"]["displayName"]},
                        "createdTime": p["createTime"],
                        "modifiedTime": p["updateTime"],
                    }
                    for p in thread["replies"]
                ],
            }
            if thread["plainTextQuote"]:
                record["quotedFileContent"] = {
                    "mimeType": "text/html",
                    "value": thread["plainTextQuote"],
                }
            out.append(record)
        return out

    # -- seeding ----------------------------------------------------------
    def _seed_segments(self, doc: MockDoc, spec: dict[str, Any], tab_id: str) -> None:
        """Attach one tab's declared header/footer/footnote segments.

        ``{"headers": {"kix.h1": "Draft — do not circulate\\n"}}``, or
        ``{"headers": ["Draft\\n"]}`` to let the model mint an opaque id the
        way prod does. Values are the segment's base text.
        """
        for kind, field in (
            (HEADER, "headers"),
            (FOOTER, "footers"),
            (FOOTNOTE, "footnotes"),
        ):
            declared = spec.get(field) or {}
            if isinstance(declared, dict):
                items = list(declared.items())
            else:
                items = [(None, text) for text in declared]
            for segment_id, text in items:
                doc.add_segment(
                    kind, text=text or "\n", segment_id=segment_id, tab_id=tab_id
                )

    def _seed_ops(
        self, doc: MockDoc, ops: list[dict[str, Any]], default_tab: Optional[str] = None
    ) -> None:
        """Replay §5 operations, each into the segment it names.

        ``tab_id``/``segment_id`` on an op place it; omitting both puts it in
        the default tab's body, which keeps every pre-tabs seed working
        verbatim. An op declared inside a ``tabs`` entry defaults to that
        tab -- otherwise a scenario would have to repeat the tab id on every
        line and a forgotten one would silently seed the wrong tab, which is
        the very mistake these seeds exist to set up deliberately.
        """
        for op in ops:
            kind = op.get("op")
            author = op.get("author", self.me)
            tab_id = op.get("tab_id", default_tab)
            segment_id = op.get("segment_id")
            try:
                segment = doc.resolve_segment(tab_id=tab_id, segment_id=segment_id).key
            except MockDocsError as exc:
                raise MockDocsError(f"seed op {op!r}: {exc}") from None
            if kind == "insert":
                doc.insert(op["index"], op["text"], author, segment)
            elif kind == "delete":
                doc.delete(op["start"], op["end"], author, segment)
            elif kind == "replace":
                doc.replace(op["start"], op["end"], op["text"], author, segment)
            else:
                raise MockDocsError(f"unknown seed op: {kind!r}")

    def seed(self, spec: dict[str, Any]) -> None:
        """Load documents (and pending suggestions/comments) from a dict.

        Suggestions are replayed through the SPEC §5 operations, so seeded
        suggestion ids are the model's deterministic ones and seeded state is
        reachable state -- a seed can never encode a document the editor
        could not have produced.

        Seed op indexes are MODEL (grapheme-cluster) indexes into the segment
        as it stands when that op runs, NOT the UTF-16 API indexes the tools
        use: ops apply in order, so each one sees its predecessors' edits.

        Tabs and non-body segments are optional and additive, so **every
        pre-tabs seed still means exactly what it meant**: a document spec
        with only ``text`` + ``suggestions`` is single-tab (``t.0``) and
        body-only, and its ops land in that body. To go further::

            {"document_id": "d1",
             "text": "Body of the first tab.\\n",
             "headers": {"kix.h1": "Confidential\\n"},
             "suggestions": [{"op": "insert", "index": 0, "text": "DRAFT ",
                              "segment_id": "kix.h1"}],
             "tabs": [{"tab_id": "t.second", "title": "Appendix",
                       "text": "Body of the second tab.\\n",
                       "suggestions": [{"op": "delete", "start": 0, "end": 4}]}]}

        Both suggestions above sit at index 0/1 of their own segment and are
        by the same author, and they do NOT merge: they are different places.
        """
        if "me" in spec:
            self.me = spec["me"]
        if "not_enrolled" in spec:
            self.not_enrolled = bool(spec["not_enrolled"])
        if "fail_comment_updates" in spec:
            self.fail_comment_updates = bool(spec["fail_comment_updates"])
        for doc_spec in spec.get("documents", []):
            doc = self.add_document(
                text=doc_spec.get("text", "\n"),
                document_id=doc_spec.get("document_id"),
                title=doc_spec.get("title", "Mock Document"),
            )
            self._seed_segments(doc, doc_spec, doc.default_tab_id)
            extra_tabs = []
            for tab_spec in doc_spec.get("tabs", []):
                tab = doc.add_tab(
                    text=tab_spec.get("text", "\n"),
                    tab_id=tab_spec.get("tab_id"),
                    title=tab_spec.get("title"),
                )
                self._seed_segments(doc, tab_spec, tab.tab_id)
                extra_tabs.append((tab, tab_spec))
            # The default tab's ops first, then each extra tab's own, so an
            # index in a tab spec means "this tab as declared" rather than
            # "after whatever a later tab did".
            self._seed_ops(doc, doc_spec.get("suggestions", []))
            for tab, tab_spec in extra_tabs:
                self._seed_ops(doc, tab_spec.get("suggestions", []), tab.tab_id)
            for comment in doc_spec.get("comments", []):
                self.create_comment_thread(
                    doc.document_id,
                    content=comment["content"],
                    quote=comment.get("quote", ""),
                    author=comment.get("author"),
                )

    def seed_from_file(self, path: str | Path) -> None:
        self.seed(json.loads(Path(path).read_text(encoding="utf-8")))

    # -- service factories ------------------------------------------------
    def docs_service(self) -> "FakeDocsService":
        return FakeDocsService(self)

    def drive_service(self) -> "FakeDriveService":
        return FakeDriveService(self)

    def service_for(self, service_name: str) -> Any:
        if service_name == "docs":
            return self.docs_service()
        if service_name == "drive":
            return self.drive_service()
        raise MockDocsError(f"mockdocs has no fake for service {service_name!r}")


# ---------------------------------------------------------------------------
# docs v1
# ---------------------------------------------------------------------------


class _Documents:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def get(
        self,
        documentId: str,
        suggestionsViewMode: Optional[str] = None,
        commentsViewMode: Optional[str] = None,
        includeTabsContent: Optional[bool] = None,
        **_: Any,
    ) -> _Call:
        """``documents.get``, including the thread-bearing preview read.

        Accepting ``commentsViewMode`` here is what lets the mock stand in
        for the raw authorized request
        :mod:`gdocs_preview.preview_read` has to make against the real API
        (the parameter is missing from public discovery, so the real client
        raises ``TypeError`` and the production code falls back to a raw
        request -- the mock simply supports it).
        """

        def run() -> dict[str, Any]:
            doc = self._b.get_document(documentId)
            mode = suggestionsViewMode or "DEFAULT_FOR_CURRENT_ACCESS"
            if commentsViewMode and not includeTabsContent:
                raise http_error(
                    400,
                    "Comments view mode may only be specified if tabs content "
                    "is also requested.",
                )
            if commentsViewMode and commentsViewMode not in _COMMENTS_VIEW_MODES:
                raise http_error(
                    400,
                    "Invalid value at 'comments_view_mode' "
                    "(type.googleapis.com/google.apps.docs.v1.CommentsViewMode), "
                    f'"{commentsViewMode}"',
                )
            try:
                if includeTabsContent:
                    return tabs_document_payload(
                        doc,
                        mode,
                        me=self._b.me,
                        comments=self._b.comments.get(documentId),
                        include_comments=(
                            commentsViewMode == "COMMENTS_VIEW_MODE_INCLUDED"
                        ),
                    )
                return document_payload(doc, mode, me=self._b.me)
            except MockDocsError as exc:
                raise http_error(400, str(exc)) from None

        return _Call(run)

    def batchUpdate(self, documentId: str, body: dict[str, Any]) -> _Call:
        def run() -> dict[str, Any]:
            doc = self._b.get_document(documentId)
            return apply_batch_update(self._b, doc, body or {})

        return _Call(run)

    def create(self, body: Optional[dict[str, Any]] = None) -> _Call:
        def run() -> dict[str, Any]:
            title = (body or {}).get("title") or "Untitled document"
            doc = self._b.add_document(text="\n", title=title)
            return {
                "documentId": doc.document_id,
                "title": doc.title,
                "body": {"content": []},
            }

        return _Call(run)


class FakeDocsService:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def documents(self) -> _Documents:
        return _Documents(self._b)

    def close(self) -> None:  # the service decorator closes injected services
        return None


# ---------------------------------------------------------------------------
# drive v3
# ---------------------------------------------------------------------------


class _Files:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def get(self, fileId: str, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            doc = self._b.get_document(fileId)
            return {
                "id": doc.document_id,
                "name": doc.title,
                "mimeType": "application/vnd.google-apps.document",
                "webViewLink": f"https://docs.google.com/document/d/{doc.document_id}/edit",
                "trashed": False,
            }

        return _Call(run)

    def delete(self, fileId: str, **_: Any) -> _Call:
        def run() -> str:
            self._b.get_document(fileId)
            self._b.documents.pop(fileId, None)
            self._b.comments.pop(fileId, None)
            return ""

        return _Call(run)

    def update(self, fileId: str, body: Optional[dict] = None, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            doc = self._b.get_document(fileId)
            if (body or {}).get("trashed"):
                self._b.documents.pop(fileId, None)
                self._b.comments.pop(fileId, None)
                return {"id": fileId, "trashed": True}
            if (body or {}).get("name"):
                doc.title = body["name"]
            return {"id": fileId, "name": doc.title, "trashed": False}

        return _Call(run)

    def list(self, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            return {
                "files": [
                    {
                        "id": d.document_id,
                        "name": d.title,
                        "mimeType": "application/vnd.google-apps.document",
                    }
                    for d in self._b.documents.values()
                ]
            }

        return _Call(run)


class _Comments:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def list(self, fileId: str, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            self._b.get_document(fileId)
            return {"comments": self._b.drive_comments(fileId)}

        return _Call(run)

    def create(self, fileId: str, body: dict, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            self._b.get_document(fileId)
            # Drive-created comments are document-level: no anchor, no quote.
            thread = self._b.create_comment_thread(
                fileId, content=body.get("content", "")
            )
            return self._b.drive_comments(fileId)[
                [t["commentId"] for t in self._b.comments[fileId]].index(
                    thread["commentId"]
                )
            ]

        return _Call(run)

    def update(self, fileId: str, commentId: str, body: dict, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            doc = self._b.get_document(fileId)
            self._b.update_post(
                doc,
                commentId,
                None,
                self._b._find_thread(fileId, commentId, 0)["headPost"]["postId"],
                body.get("content", ""),
            )
            return next(
                c for c in self._b.drive_comments(fileId) if c["id"] == commentId
            )

        return _Call(run)

    def delete(self, fileId: str, commentId: str, **_: Any) -> _Call:
        def run() -> str:
            self._b.get_document(fileId)
            self._b.delete_comment_thread(fileId, commentId)
            return ""

        return _Call(run)


class _Replies:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def create(self, fileId: str, commentId: str, body: dict, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            self._b.get_document(fileId)
            # Drive resolves a comment by posting a reply with action=resolve.
            action = "RESOLVE" if (body or {}).get("action") == "resolve" else None
            post = self._b.add_comment_reply(
                fileId, commentId, (body or {}).get("content", ""), action
            )
            return {
                "id": post["postId"],
                "content": post["content"],
                "author": {"displayName": post["author"]["displayName"]},
                "createdTime": post["createTime"],
                "modifiedTime": post["updateTime"],
            }

        return _Call(run)

    def list(self, fileId: str, commentId: str, **_: Any) -> _Call:
        def run() -> dict[str, Any]:
            self._b.get_document(fileId)
            thread = self._b._find_thread(fileId, commentId, 0)
            return {
                "replies": [
                    {"id": p["postId"], "content": p["content"]}
                    for p in thread["replies"]
                ]
            }

        return _Call(run)

    def delete(self, fileId: str, commentId: str, replyId: str, **_: Any) -> _Call:
        def run() -> str:
            doc = self._b.get_document(fileId)
            self._b.delete_reply(doc, commentId, None, replyId)
            return ""

        return _Call(run)


class FakeDriveService:
    def __init__(self, backend: FakeBackend) -> None:
        self._b = backend

    def files(self) -> _Files:
        return _Files(self._b)

    def comments(self) -> _Comments:
        return _Comments(self._b)

    def replies(self) -> _Replies:
        return _Replies(self._b)

    def close(self) -> None:
        return None
