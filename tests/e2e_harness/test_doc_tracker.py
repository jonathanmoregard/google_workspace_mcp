"""Scratch-doc teardown behaviour under rate limiting.

Covers the two places where a 429 could leave a real document behind in
the user's Drive: the trash call itself, and the abandoned twin left by a
retried ``create_doc``.
"""

from __future__ import annotations

import pytest

from e2e import quota
from e2e.conftest import DocTracker


class _FakeHttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"<HttpError {status} when requesting ...>")
        self.resp = type("Resp", (), {"status": status})()


class FakeRequest:
    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self):
        if callable(self._outcome):
            return self._outcome()
        return self._outcome


class FakeFiles:
    def __init__(self, drive):
        self._drive = drive

    def update(self, fileId, body):  # noqa: N803 - googleapiclient's spelling
        self._drive.update_calls.append((fileId, body))

        def run():
            failures = self._drive.fail_updates.get(fileId, 0)
            if failures:
                self._drive.fail_updates[fileId] = failures - 1
                raise _FakeHttpError(429, "rateLimitExceeded")
            return {"id": fileId}

        return FakeRequest(run)

    def list(self, q, fields, pageSize):  # noqa: N803 - googleapiclient's spelling
        self._drive.list_queries.append(q)
        return FakeRequest({"files": self._drive.list_result})


class FakeDrive:
    def __init__(self, list_result=None, fail_updates=None):
        self.update_calls: list = []
        self.list_queries: list[str] = []
        self.list_result = list_result or []
        self.fail_updates = dict(fail_updates or {})

    def files(self):
        return FakeFiles(self)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(quota.time, "sleep", lambda _: None)


def test_trashing_retries_through_a_rate_limit():
    """Teardown that loses to a 429 would orphan a real document."""
    drive = FakeDrive(fail_updates={"doc-1": 1})
    tracker = DocTracker(drive=drive)
    tracker.register("doc-1", "e2e-gdocs-review-x")

    tracker.cleanup("doc-1")

    assert len(drive.update_calls) == 2
    assert tracker.entries[0]["cleaned"] is True
    assert tracker.entries[0]["method"] == "trash"


def test_trashing_treats_a_404_as_already_gone():
    drive = FakeDrive()

    class Gone(FakeFiles):
        def update(self, fileId, body):  # noqa: N803
            def run():
                raise _FakeHttpError(404)

            return FakeRequest(run)

    drive.files = lambda: Gone(drive)  # type: ignore[method-assign]
    tracker = DocTracker(drive=drive)
    tracker.register("doc-1", "t")

    with pytest.raises(Exception):  # noqa: B017 - HttpError shape is faked
        tracker.cleanup("doc-1")


def test_duplicate_lookup_finds_the_abandoned_twin():
    drive = FakeDrive(list_result=[{"id": "keeper"}, {"id": "orphan"}])
    tracker = DocTracker(drive=drive)

    found = tracker.find_docs_titled("e2e-gdocs-review-1-abc", exclude="keeper")

    assert found == ["orphan"]
    assert drive.list_queries == ["name = 'e2e-gdocs-review-1-abc' and trashed = false"]


def test_duplicate_lookup_escapes_quotes_in_the_title():
    drive = FakeDrive(list_result=[])
    tracker = DocTracker(drive=drive)

    tracker.find_docs_titled("it's-a-title", exclude="keeper")

    assert drive.list_queries == ["name = 'it\\'s-a-title' and trashed = false"]


def test_duplicate_lookup_never_gates_the_run(monkeypatch):
    """A failed hygiene lookup must not take a passing test down with it."""

    class Exploding(FakeDrive):
        def files(self):
            raise RuntimeError("drive is down")

    tracker = DocTracker(drive=Exploding())
    assert tracker.find_docs_titled("t", exclude="keeper") == []
