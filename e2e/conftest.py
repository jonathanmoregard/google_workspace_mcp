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
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from e2e import gating, quota
from e2e.mcp_session import ServerSession, tool_json, tool_text
from e2e.run_report import REPORT

E2E_DIR = Path(__file__).resolve().parent
SCRATCH_PREFIX = "e2e-gdocs-review"
E2E_MARKERS = {"e2e_ga", "e2e_preview"}

#: create_doc confirms with "Created Google Doc '<title>' (ID: <id>) ..."
#: (human-readable, not JSON) - the doc id is parsed out of it.
_DOC_ID_RE = re.compile(r"\(ID: ([^)\s]+)\)")


def new_scratch_title(suffix: str = "") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{SCRATCH_PREFIX}-{stamp}-{secrets.token_hex(4)}{suffix}"


def create_doc_via_mcp(mcp, email: str, title: str, content: str = "") -> str:
    """Create a doc through the MCP surface (upstream ``create_doc``)."""
    args: dict[str, Any] = {"user_google_email": email, "title": title}
    if content:
        args["content"] = content
    confirmation = tool_text(mcp.call_tool("create_doc", args))
    match = _DOC_ID_RE.search(confirmation)
    assert match, (
        f"create_doc confirmation carries no '(ID: ...)': {confirmation[:300]!r}"
    )
    return match.group(1)


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
    if not REPORT.outcomes:
        return
    snapshot = quota.snapshot()
    REPORT.set_quota(snapshot)
    if snapshot["exhausted"]:
        REPORT.note(
            f"QUOTA: {len(snapshot['exhausted'])} call(s) never got through "
            "the write-quota wall; this run is INCOMPLETE."
        )
    target = REPORT.write()
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"e2e write quota: {snapshot['write_units']} write requests, "
            f"{snapshot['rate_limited']} rate-limited, "
            f"{snapshot['retries']} retries, "
            f"{snapshot['paced_seconds']}s paced + "
            f"{snapshot['backoff_seconds']}s backoff"
            + (
                f", {len(snapshot['exhausted'])} GAVE UP"
                if snapshot["exhausted"]
                else ""
            )
        )
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
    # Write quota is per Google account, so the cross-run pacing window is
    # keyed on the identity we just proved - not on this checkout.
    quota.bind_account(info["email"])
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
        """Trash a scratch doc (idempotent; tolerates already-deleted).

        Retried on 429: teardown that loses a race with a rate limit
        orphans a document in the user's Drive, which is the one failure
        mode the hygiene rules will not tolerate. This path talks to Drive
        directly, so it still has real response headers and can honour
        ``Retry-After`` properly.
        """
        entry = self._entry(doc_id)
        if entry is not None and entry["cleaned"]:
            return
        try:
            quota.retry_google_call(
                lambda: (
                    self.drive.files()
                    .update(fileId=doc_id, body={"trashed": True})
                    .execute()
                ),
                label=f"trash {doc_id}",
                stats=quota.STATS,
            )
            self.mark_cleaned(doc_id, "trash")
        except HttpError as error:
            status = getattr(getattr(error, "resp", None), "status", None)
            if status == 404:
                self.mark_cleaned(doc_id, "already-gone")
            else:
                raise

    def find_docs_titled(self, title: str, exclude: str) -> list[str]:
        """Ids of untrashed docs with this exact title, minus ``exclude``.

        Scratch titles carry 8 hex characters of entropy, so a second
        document with the identical title is not a coincidence - it is the
        abandoned first attempt of a ``create_doc`` that was rate limited
        between its ``documents.create`` and its content ``batchUpdate``.
        The retry produced a new document and the original became garbage
        no one is tracking. This finds it so teardown can trash it.
        """
        escaped = title.replace("\\", "\\\\").replace("'", "\\'")
        try:
            response = quota.retry_google_call(
                lambda: (
                    self.drive.files()
                    .list(
                        q=f"name = '{escaped}' and trashed = false",
                        fields="files(id)",
                        pageSize=25,
                    )
                    .execute()
                ),
                label=f"find duplicates of {title}",
                stats=quota.STATS,
            )
        except Exception as exc:  # noqa: BLE001 - hygiene aid, never a gate
            REPORT.note(f"duplicate-title lookup failed for {title}: {exc}")
            return []
        return [
            f["id"]
            for f in response.get("files", [])
            if f.get("id") and f["id"] != exclude
        ]

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


@pytest.fixture(scope="session")
def degraded_read_mcp(ga_auth, tmp_path_factory) -> ServerSession:
    """A second real server whose PREVIEW read is broken and GA read is not.

    The two reads run on different HTTP stacks: the thread-bearing preview
    read is a raw ``google.auth.transport.requests.AuthorizedSession``
    (requests), while ``documents.get`` and every other API call go through
    googleapiclient (httplib2). ``REQUESTS_CA_BUNDLE`` is honoured by requests
    and ignored by httplib2, so a CA file containing no certificates fails the
    preview read with an SSLError -- caught by ``preview_read`` as a
    ``PreviewReadError`` -- and leaves everything else working. That is the
    exact production shape of a lapsed/unenrolled preview or a proxy that eats
    the raw request: ``read_source: "ga_documents_get"``, one unnamed body, no
    tab ids.

    Nothing in the product knows this variable exists: it is a real
    misconfiguration, not a test hook, which is why the degraded read is
    reachable from a blackbox suite at all. Its own process, so its broken
    TLS cannot leak into the primary session.
    """
    empty_ca = tmp_path_factory.mktemp("degraded-ca") / "no-certificates.pem"
    empty_ca.write_text("")
    session = ServerSession(
        credentials_dir=str(ga_auth.credentials_dir),
        user_email=ga_auth.email,
        extra_env={"REQUESTS_CA_BUNDLE": str(empty_ca)},
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

    def factory(title_suffix: str = "", content: str = "") -> str:
        title = new_scratch_title(title_suffix)
        # create_doc with content is two API requests; a 429 on the second
        # leaves a document behind that the retry's return value knows
        # nothing about. Watch the guard's counter and, only when it moved,
        # go looking for the abandoned twin.
        retries_before = len(quota.STATS.orphan_risk_retries)
        doc_id = create_doc_via_mcp(mcp, ga_auth.email, title, content=content)
        doc_tracker.register(doc_id, title)
        created.append(doc_id)
        if len(quota.STATS.orphan_risk_retries) > retries_before:
            for stray_id in doc_tracker.find_docs_titled(title, exclude=doc_id):
                doc_tracker.register(stray_id, f"{title} (abandoned retry)")
                created.append(stray_id)
                REPORT.note(
                    f"reclaimed {stray_id}: abandoned by a rate-limited "
                    f"create_doc retry for {title!r}"
                )
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
    """One live check_docs_review_capabilities probe per session.

    Uses its own scratch doc, trashed immediately after the probe so the
    teardown audit sees no long-lived documents.
    """
    title = new_scratch_title("-probe")
    doc_id = create_doc_via_mcp(mcp, ga_auth.email, title)
    doc_tracker.register(doc_id, title)
    try:
        report = tool_json(
            mcp.call_tool(
                "check_docs_review_capabilities",
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
