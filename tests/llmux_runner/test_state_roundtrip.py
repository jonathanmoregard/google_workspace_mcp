"""The state dump is the only channel out of the server process.

If a snapshot loses a mark, a suggestion id or a comment thread, every grade
built on it is wrong -- so the round trip is asserted structurally (dump ==
dump of the reloaded backend) as well as behaviourally (the reloaded backend
still accepts/rejects correctly).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mockdocs import state
from mockdocs.fake_services import FakeBackend

REPO_ROOT = Path(__file__).resolve().parents[2]

SEED = {
    "me": "alice",
    "documents": [
        {
            "document_id": "doc-1",
            "title": "Round Trip",
            "text": "Hello 🎉 world.\nSecond paragraph here.\n",
            "suggestions": [
                {"op": "insert", "index": 5, "text": " brave", "author": "bob"},
                {"op": "delete", "start": 21, "end": 28, "author": "carol"},
            ],
            "comments": [{"content": "please review", "quote": "Hello"}],
        }
    ],
}


def seeded() -> FakeBackend:
    backend = FakeBackend()
    backend.seed(SEED)
    return backend


def test_dump_load_is_lossless():
    original = seeded()
    reloaded = state.load_backend(state.dump_backend(original))
    assert state.dump_backend(reloaded) == state.dump_backend(original)


def test_reloaded_backend_preserves_marks_authors_and_threads():
    reloaded = state.load_backend(state.dump_backend(seeded()))
    doc = reloaded.documents["doc-1"]
    doc.check_invariants()
    assert reloaded.me == "alice"
    assert doc.display_text() == "Hello brave 🎉 world.\nSecond paragraph here.\n"
    assert doc.original_text() == "Hello 🎉 world.\nSecond paragraph here.\n"
    assert {s.author for s in doc.registry.values()} == {"bob", "carol"}
    assert reloaded.comments["doc-1"][0]["plainTextQuote"] == "Hello"


def test_reloaded_backend_still_resolves_suggestions():
    reloaded = state.load_backend(state.dump_backend(seeded()))
    doc = reloaded.documents["doc-1"]
    bob = next(s for s in doc.registry.values() if s.author == "bob").id
    carol = next(s for s in doc.registry.values() if s.author == "carol").id
    assert doc.accept(bob) is True
    assert doc.reject(carol) is True
    assert doc.registry == {}
    assert doc.display_text() == "Hello brave 🎉 world.\nSecond paragraph here.\n"


def test_write_state_is_atomic_and_readable(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state.write_state(seeded(), path)
    assert path.is_file()
    assert not list(path.parent.glob(".mockdocs-state-*")), "temp file left behind"
    assert state.read_state(path).documents["doc-1"].title == "Round Trip"


def test_unknown_schema_version_is_refused():
    payload = state.dump_backend(seeded())
    payload["schema_version"] = 999
    with pytest.raises(state.StateFormatError, match="schema_version"):
        state.load_backend(payload)


def test_missing_or_corrupt_dump_raises_state_format_error(tmp_path):
    with pytest.raises(state.StateFormatError, match="no state dump"):
        state.read_state(tmp_path / "absent.json")
    corrupt = tmp_path / "half.json"
    corrupt.write_text('{"schema_version": 1, "docum', encoding="utf-8")
    with pytest.raises(state.StateFormatError, match="not valid JSON"):
        state.read_state(corrupt)


def test_install_state_dump_refreshes_after_every_api_call(tmp_path):
    """Patching ``_Call.execute`` is what makes the end state observable."""
    from mockdocs import fake_services

    backend = seeded()
    path = tmp_path / "live.json"
    original_execute = fake_services._Call.execute
    try:
        state.install_state_dump(backend, path)
        assert path.is_file(), "install must flush an initial snapshot"
        before = state.read_state(path)
        assert len(before.comments["doc-1"]) == 1

        service = backend.drive_service()
        service.comments().create(
            fileId="doc-1", body={"content": "from a tool call"}
        ).execute()

        after = state.read_state(path)
        assert len(after.comments["doc-1"]) == 2
        assert after.comments["doc-1"][1]["headPost"]["content"] == "from a tool call"
    finally:
        fake_services._Call.execute = original_execute


def test_serve_py_writes_a_dump_when_the_env_var_is_set(tmp_path):
    """End to end through the real server entry point, over real stdio."""
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(SEED), encoding="utf-8")
    dump_path = tmp_path / "dump.json"

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "MOCKDOCS_SEED": str(seed_path),
        "MOCKDOCS_STATE_DUMP": str(dump_path),
        "MCP_ENABLE_OAUTH21": "false",
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(tmp_path / "creds"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "mockdocs" / "serve.py"),
            "--transport",
            "stdio",
            "--single-user",
            "--tools",
            "docs",
            "docs_preview",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        assert process.stdout.readline(), "server did not answer initialize"
    finally:
        process.kill()
        process.wait(timeout=30)

    backend = state.read_state(dump_path)
    assert backend.me == "alice"
    assert backend.documents["doc-1"].display_text().startswith("Hello brave")
