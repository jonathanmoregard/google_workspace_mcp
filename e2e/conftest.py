"""Fixtures + hooks for the blackbox e2e suite.

Gating chain (all session-scoped, evaluated once):
  ga_auth       -> hard-skips loudly when no usable OAuth token exists
  mcp           -> real server subprocess + MCP client (blackbox)
  preview_probe -> one live capabilities probe per session (cached)
  preview_ready -> skips e2e_preview tests with classification evidence

Scratch-doc hygiene: every doc is created through the MCP surface with a
unique ``e2e-gdocs-review-<timestamp>-<rand>`` title and trashed in
fixture teardown via a harness-side Drive client (runs even when the
test failed or the server died). test_zz_teardown_audit.py verifies it.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from e2e import gating
from e2e.mcp_session import ServerSession, tool_json
from e2e.run_report import REPORT

E2E_DIR = Path(__file__).resolve().parent
SCRATCH_PREFIX = "e2e-gdocs-review"
E2E_MARKERS = {"e2e_ga", "e2e_preview"}


def new_scratch_title(suffix: str = "") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{SCRATCH_PREFIX}-{stamp}-{secrets.token_hex(4)}{suffix}"


# ---------------------------------------------------------------------------
# pytest hooks: marker enforcement + run report
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    unmarked = [
        item.nodeid
        for item in items
        if E2E_DIR in Path(item.fspath).parents
        and not (E2E_MARKERS & {m.name for m in item.iter_markers()})
    ]
    if unmarked:
        raise pytest.UsageError(
            "e2e tests must carry the e2e_ga or e2e_preview marker: "
            + ", ".join(unmarked)
        )


def pytest_runtest_logreport(report):
    # Directory-scoped: pytest routes runtest hooks through the item's
    # conftest chain, so only e2e items land here.
    markers = {name for name in getattr(report, "keywords", {}) if name in E2E_MARKERS}
    outcome = report.outcome  # passed | failed | skipped
    skip_reason = None
    if report.skipped and isinstance(report.longrepr, tuple):
        skip_reason = report.longrepr[2]
    REPORT.observe(report.nodeid, markers, report.when, outcome, skip_reason)


def pytest_sessionfinish(session, exitstatus):
    if REPORT.outcomes:
        target = REPORT.write()
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"e2e run report written to {target}")


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


@dataclass
class AuthContext:
    email: str
    credentials: Any
    credentials_dir: Path


@pytest.fixture(scope="session")
def ga_auth() -> AuthContext:
    """Usable OAuth token or loud skip. NEVER starts an interactive flow."""
    credentials_dir = gating.resolve_credentials_dir()
    state = gating.inspect_token(credentials_dir)
    if not state.ready:
        REPORT.set_gating_note(state.skip_reason())
        pytest.skip(state.skip_reason())

    state, credentials = gating.prepare_credentials(state)
    if not state.ready:
        REPORT.set_gating_note(state.skip_reason())
        pytest.skip(state.skip_reason())

    # Live identity check (userinfo == tokeninfo equivalent): proves the
    # token actually works before we spawn the server.
    from auth.google_auth import get_user_info

    info = get_user_info(credentials)
    if not info or "email" not in info:
        reason = (
            "E2E SKIPPED - stored token was refreshed but userinfo lookup "
            f"failed (revoked?). Re-run: uv run python e2e/bootstrap_auth.py\n"
            f"  token file: {state.token_path}"
        )
        REPORT.set_gating_note(reason)
        pytest.skip(reason)

    REPORT.set_identity(info["email"], str(credentials_dir), "oauth2/v2 userinfo")
    return AuthContext(
        email=info["email"],
        credentials=credentials,
        credentials_dir=credentials_dir,
    )


# ---------------------------------------------------------------------------
# Harness-side Google clients (teardown/verification only - NOT under test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def harness_drive(ga_auth):
    """Direct Drive v3 client for scratch-doc teardown + audits.

    Deliberately not the MCP surface: cleanup must work even when the
    server under test is broken or dead.
    """
    service = build("drive", "v3", credentials=ga_auth.credentials)
    yield service
    service.close()


@dataclass
class DocTracker:
    drive: Any
    entries: list[dict[str, Any]] = field(default_factory=list)

    def register(self, doc_id: str, title: str) -> None:
        self.entries.append(
            {"doc_id": doc_id, "title": title, "cleaned": False, "method": None}
        )
        REPORT.record_doc(doc_id, title)

    def _entry(self, doc_id: str) -> dict[str, Any] | None:
        for entry in self.entries:
            if entry["doc_id"] == doc_id:
                return entry
        return None

    def mark_cleaned(self, doc_id: str, method: str) -> None:
        entry = self._entry(doc_id)
        if entry is not None:
            entry["cleaned"] = True
            entry["method"] = method
        REPORT.mark_doc_cleaned(doc_id, method)

    def cleanup(self, doc_id: str) -> None:
        """Trash a scratch doc (idempotent; tolerates already-deleted)."""
        entry = self._entry(doc_id)
        if entry is not None and entry["cleaned"]:
            return
        try:
            self.drive.files().update(fileId=doc_id, body={"trashed": True}).execute()
            self.mark_cleaned(doc_id, "trash")
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status == 404:
                self.mark_cleaned(doc_id, "already-gone")
            else:
                raise

    def force_cleanup(self) -> None:
        for entry in self.entries:
            if not entry["cleaned"]:
                try:
                    self.cleanup(entry["doc_id"])
                except Exception as exc:  # noqa: BLE001 - best-effort net
                    REPORT.note(f"session cleanup failed for {entry['doc_id']}: {exc}")


@pytest.fixture(scope="session")
def doc_tracker(harness_drive) -> DocTracker:
    tracker = DocTracker(drive=harness_drive)
    yield tracker
    # Session safety net: anything a function teardown missed.
    tracker.force_cleanup()


# ---------------------------------------------------------------------------
# The server under test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mcp(ga_auth) -> ServerSession:
    session = ServerSession(
        credentials_dir=str(ga_auth.credentials_dir),
        user_email=ga_auth.email,
    )
    session.start()
    yield session
    session.stop()


@pytest.fixture
def make_scratch_doc(mcp, ga_auth, doc_tracker):
    """Factory creating scratch docs through the MCP surface.

    Teardown trashes every created doc - even on test failure - via the
    harness Drive client.
    """
    created: list[str] = []

    def factory(title_suffix: str = "") -> str:
        title = new_scratch_title(title_suffix)
        doc = tool_json(
            mcp.call_tool(
                "docs_api_documents_create",
                {"user_google_email": ga_auth.email, "body": {"title": title}},
            )
        )
        doc_id = doc["documentId"]
        doc_tracker.register(doc_id, title)
        created.append(doc_id)
        return doc_id

    yield factory
    for doc_id in created:
        doc_tracker.cleanup(doc_id)


@pytest.fixture
def scratch_doc(make_scratch_doc) -> str:
    return make_scratch_doc()


# ---------------------------------------------------------------------------
# Preview enrollment detection (once per session, cached)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def preview_probe(mcp, ga_auth, doc_tracker) -> dict[str, Any]:
    """One live docs_review_capabilities probe per session.

    Uses its own scratch doc, trashed immediately after the probe so the
    teardown audit sees no long-lived documents.
    """
    title = new_scratch_title("-probe")
    doc = tool_json(
        mcp.call_tool(
            "docs_api_documents_create",
            {"user_google_email": ga_auth.email, "body": {"title": title}},
        )
    )
    doc_id = doc["documentId"]
    doc_tracker.register(doc_id, title)
    try:
        report = tool_json(
            mcp.call_tool(
                "docs_review_capabilities",
                {
                    "user_google_email": ga_auth.email,
                    "document_id": doc_id,
                    "probe": True,
                },
            )
        )
    finally:
        doc_tracker.cleanup(doc_id)
    REPORT.set_preview_classification(report["preview"])
    return report


@pytest.fixture
def preview_ready(preview_probe) -> dict[str, Any]:
    """Skips (with the probe's classification evidence) unless the
    Developer Preview surface is available to these credentials."""
    preview = preview_probe["preview"]
    if preview.get("availability") != "available":
        pytest.skip(
            "E2E PREVIEW SKIPPED - Developer Preview not available for these "
            "credentials.\nCapabilities probe classification:\n"
            + json.dumps(preview, indent=2, ensure_ascii=False)
            + "\nEnrollment steps: pending_for_human.md at the repo root."
        )
    return preview
