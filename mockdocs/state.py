"""JSON snapshots of a :class:`~mockdocs.fake_services.FakeBackend`.

The mock backend lives in the memory of the MCP server subprocess, so an
out-of-process harness (``llmux/runner``) cannot inspect the end state of a
run by importing anything -- it has to be handed the state. This module is
that hand-off: a lossless dict/JSON representation of the whole backend plus
a loader that rebuilds a real ``FakeBackend`` from it, so graders can be
written against the ordinary backend API rather than against a wire format.

``install_state_dump(backend, path)`` wires the snapshot into a running
server: every API call (``_Call.execute``) rewrites the file atomically, and
SIGTERM/atexit flush it once more. Writing on reads as well as writes is
deliberate -- it costs a few hundred microseconds on a document that is
kilobytes at most, and it removes the need to know which call shapes mutate.

Nothing here is imported by the server unless ``MOCKDOCS_STATE_DUMP`` is set.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import tempfile
from pathlib import Path
from typing import Any, Optional

from mockdocs.concurrency import ConcurrencyRecord
from mockdocs.fake_services import FakeBackend
from mockdocs.model import Char, Comment, MockDoc, Suggestion

#: Bumped when the snapshot shape changes incompatibly.
#:
#: The ``concurrency`` key added for interleaved runs is deliberately NOT a
#: version bump: it is optional and absent from every single-writer snapshot,
#: so old dumps still load and batches stay comparable.
SCHEMA_VERSION = 1


class StateFormatError(Exception):
    """A snapshot could not be read (missing, truncated, wrong version)."""


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


def dump_char(char: Char) -> dict[str, Any]:
    return {
        "cp": char.cp,
        "ins": sorted(char.ins),
        "dels": sorted(char.dels),
        "colour": char.colour,
    }


def dump_suggestion(sug: Suggestion) -> dict[str, Any]:
    return {
        "id": sug.id,
        "author": sug.author,
        "created_at": sug.created_at,
        "touched_at": sug.touched_at,
        "thread": [
            {
                "post_id": post.post_id,
                "author": post.author,
                "content": post.content,
                "created_at": post.created_at,
            }
            for post in sug.thread
        ],
    }


def dump_document(doc: MockDoc) -> dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "title": doc.title,
        "chars": [dump_char(c) for c in doc.chars],
        "registry": {sid: dump_suggestion(s) for sid, s in doc.registry.items()},
        "clock": doc._clock,
        "counters": dict(doc._counters),
        "merge_log": [list(pair) for pair in doc.merge_log],
        "gc_log": list(doc.gc_log),
        # Convenience projections: a grader that only cares about text does
        # not have to replay the char array. Ignored on load.
        "text": {
            "display": doc.display_text(),
            "original": doc.original_text(),
            "final": doc.final_text(),
        },
    }


def dump_backend(backend: FakeBackend) -> dict[str, Any]:
    """Whole-backend snapshot: documents, comment threads, identity, flags.

    Under interference the snapshot also carries the interleaving itself
    (``concurrency``): the agent's call log, which interference fired at
    which call, and any invariant violation. The grader runs in the harness
    process and the interleaving happened in the server's, so this is the
    only way it can tell "the agent's target was accepted away by someone
    else" from "the agent got it wrong".
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "me": backend.me,
        "not_enrolled": backend.not_enrolled,
        "fail_comment_updates": backend.fail_comment_updates,
        "counters": dict(backend._counters),
        "documents": [dump_document(d) for d in backend.documents.values()],
        "comments": {k: v for k, v in backend.comments.items()},
    }
    record = getattr(backend, "concurrency", None)
    if record is not None:
        payload["concurrency"] = record.as_dict()
    return payload


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def load_document(state: dict[str, Any]) -> MockDoc:
    doc = MockDoc.__new__(MockDoc)
    doc.document_id = state["document_id"]
    doc.title = state.get("title", "Mock Document")
    doc.chars = [
        Char(
            cp=c["cp"],
            ins=set(c.get("ins") or []),
            dels=set(c.get("dels") or []),
            colour=c.get("colour"),
        )
        for c in state.get("chars", [])
    ]
    doc.registry = {
        sid: Suggestion(
            id=s["id"],
            author=s["author"],
            created_at=s["created_at"],
            touched_at=s["touched_at"],
            thread=[
                Comment(
                    post_id=p["post_id"],
                    author=p["author"],
                    content=p["content"],
                    created_at=p["created_at"],
                )
                for p in s.get("thread", [])
            ],
        )
        for sid, s in (state.get("registry") or {}).items()
    }
    doc._clock = state.get("clock", 0)
    doc._counters = dict(state.get("counters") or {})
    doc.merge_log = [tuple(pair) for pair in state.get("merge_log") or []]
    doc.gc_log = list(state.get("gc_log") or [])
    return doc


def load_backend(state: dict[str, Any]) -> FakeBackend:
    """Rebuild a live :class:`FakeBackend` from :func:`dump_backend` output."""
    version = state.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StateFormatError(
            f"unsupported state schema_version {version!r} "
            f"(this build reads {SCHEMA_VERSION})"
        )
    backend = FakeBackend(
        me=state.get("me", "mockuser"),
        not_enrolled=bool(state.get("not_enrolled")),
        fail_comment_updates=bool(state.get("fail_comment_updates")),
    )
    for doc_state in state.get("documents", []):
        doc = load_document(doc_state)
        backend.documents[doc.document_id] = doc
        backend.comments.setdefault(doc.document_id, [])
    for doc_id, threads in (state.get("comments") or {}).items():
        backend.comments[doc_id] = list(threads)
    backend._counters = dict(state.get("counters") or {})
    if "concurrency" in state:
        backend.concurrency = ConcurrencyRecord.from_dict(state["concurrency"])
    return backend


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def write_state(backend: FakeBackend, path: str | Path) -> None:
    """Atomically write a snapshot (temp file + rename, same directory).

    Atomic because the harness polls/reads the file while the server is still
    alive; a half-written snapshot would look like a corrupt end state.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dump_backend(backend), ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".mockdocs-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state_dict(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise StateFormatError(f"no state dump at {source}")
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StateFormatError(
            f"state dump at {source} is not valid JSON: {exc}"
        ) from exc


def read_state(path: str | Path) -> FakeBackend:
    """Read a snapshot file back into a live backend."""
    return load_backend(read_state_dict(path))


# ---------------------------------------------------------------------------
# server-side hook
# ---------------------------------------------------------------------------


def install_state_dump(backend: FakeBackend, path: str | Path) -> None:
    """Keep ``path`` in sync with ``backend`` for the life of the process.

    Patches ``fake_services._Call.execute`` (every docs/drive API call goes
    through it) so the snapshot is refreshed after each call, successful or
    not, and flushes once at install time, at exit, and on SIGTERM -- the
    signal a headless agent's parent sends when it tears the server down.
    """
    from mockdocs import fake_services

    target = Path(path)
    original = fake_services._Call.execute

    def execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return original(self, *args, **kwargs)
        finally:
            try:
                write_state(backend, target)
            except OSError:  # pragma: no cover - disk failure, not our concern
                pass

    execute.__mockdocs_state_dump__ = True  # type: ignore[attr-defined]
    fake_services._Call.execute = execute  # type: ignore[assignment]

    def flush(*_: Any) -> None:
        try:
            write_state(backend, target)
        except OSError:  # pragma: no cover
            pass

    atexit.register(flush)

    previous: Optional[Any] = None
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):  # pragma: no cover - non-main thread
        previous = None

    def on_sigterm(signum: int, frame: Any) -> None:  # pragma: no cover - signal
        flush()
        if callable(previous) and previous not in (
            signal.SIG_DFL,
            signal.SIG_IGN,
        ):
            previous(signum, frame)
        else:
            raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError):  # pragma: no cover - non-main thread
        pass

    flush()
