#!/usr/bin/env python3
"""Run the repo's real MCP server against the in-memory mock.

    python mockdocs/serve.py --transport stdio --single-user \\
        --tools docs docs_preview

This is ``main.py`` -- the same server, the same tool registration, the same
FastMCP transport -- with exactly one thing swapped: the seam where
``@require_google_service`` injects a googleapiclient Resource now yields a
:class:`~mockdocs.fake_services.FakeBackend` service instead. Authentication
is bypassed entirely; no token, no credentials file, no network.

**Zero diffs to upstream files.** The patch is applied from this script
before ``main.main()`` runs. ``auth.service_decorator._authenticate_service``
is resolved as a module global inside the decorator's wrapper, so rebinding
it here covers both ``@require_google_service`` and
``@require_multiple_services``, whenever the tool modules were imported.

Environment:
    MOCKDOCS_SEED           path to a seed JSON file (see FakeBackend.seed)
    MOCKDOCS_ME             author id for the authenticated user
    MOCKDOCS_NOT_ENROLLED   "1" to simulate a caller without Developer
                            Preview enrollment (preview requests 400 with
                            "Unknown name")
    MOCKDOCS_FAIL_COMMENTS  "1" to force commentUpdateState=ALL_FAILED_...
    MOCKDOCS_STATE_DUMP     path to keep a JSON snapshot of the backend at
                            (see mockdocs.state) -- refreshed after every API
                            call and on shutdown, so an out-of-process harness
                            can grade the end state of a run.
    MOCKDOCS_INTERFERENCE   path to an interference script (see
                            mockdocs.concurrency): a scripted second editor
                            whose operations fire at deterministic points in
                            the agent's own call sequence.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mockdocs.fake_services import FakeBackend  # noqa: E402


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_backend() -> FakeBackend:
    """Construct the process-wide backend from the environment."""
    backend = FakeBackend(
        me=os.getenv("MOCKDOCS_ME", "mockuser"),
        not_enrolled=_truthy(os.getenv("MOCKDOCS_NOT_ENROLLED")),
        fail_comment_updates=_truthy(os.getenv("MOCKDOCS_FAIL_COMMENTS")),
    )
    seed = os.getenv("MOCKDOCS_SEED")
    if seed:
        backend.seed_from_file(seed)
    if not backend.documents:
        backend.add_document(
            text="Hello world.\nThe quick brown fox 🎉 jumps.\n",
            document_id="mockdoc-demo",
            title="Mock Demo Document",
        )
    return backend


def install(backend: FakeBackend) -> None:
    """Patch the service-injection seam so tools receive mock services."""
    import auth.service_decorator as service_decorator

    async def _mock_authenticate_service(
        use_oauth21: bool,
        service_name: str,
        service_version: str,
        tool_name: str,
        user_google_email: str,
        resolved_scopes: list[str],
        mcp_session_id: Optional[str],
        authenticated_user: Optional[str],
    ) -> tuple[Any, str]:
        email = user_google_email or f"{backend.me}@example.com"
        return backend.service_for(service_name), email

    service_decorator._authenticate_service = _mock_authenticate_service
    service_decorator._MOCKDOCS_BACKEND = backend


def prepare_environment() -> None:
    """Pin the server into the simplest auth mode and give it a scratch
    credentials directory (main() checks that the directory is writable)."""
    os.environ.setdefault("USER_GOOGLE_EMAIL", "mockuser@example.com")
    os.environ["MCP_ENABLE_OAUTH21"] = "false"
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_KEY_FILE", None)
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_KEY_JSON", None)
    if not os.getenv("WORKSPACE_MCP_CREDENTIALS_DIR"):
        os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"] = tempfile.mkdtemp(
            prefix="mockdocs-creds-"
        )


def install_state_dump_if_requested(backend: FakeBackend) -> Optional[str]:
    """Honour ``MOCKDOCS_STATE_DUMP``; no-op (and no import) when unset."""
    path = os.getenv("MOCKDOCS_STATE_DUMP")
    if not path:
        return None
    from mockdocs.state import install_state_dump

    install_state_dump(backend, path)
    return path


def install_interference_if_requested(backend: FakeBackend) -> Optional[Any]:
    """Honour ``MOCKDOCS_INTERFERENCE``; no-op (and no import) when unset."""
    path = os.getenv("MOCKDOCS_INTERFERENCE")
    if not path:
        return None
    from mockdocs.concurrency import install_interference, load_script

    return install_interference(backend, load_script(path))


def main(argv: Optional[list[str]] = None) -> None:
    prepare_environment()
    backend = build_backend()
    install(backend)
    engine = install_interference_if_requested(backend)
    dump_path = install_state_dump_if_requested(backend)
    if engine is not None and dump_path:
        # An interference fires between API calls, so nothing would otherwise
        # rewrite the snapshot until the agent's next call -- and a run whose
        # last event is an interference would grade against a state that
        # predates it.
        from mockdocs.state import write_state

        engine.on_change = lambda: write_state(backend, dump_path)
        engine.on_change()

    import main as server_main

    if argv is not None:
        sys.argv = ["mockdocs/serve.py", *argv]
    server_main.main()


if __name__ == "__main__":
    main()
