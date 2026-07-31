"""The real server, over real stdio, with a second editor in the document.

Everything else in this package exercises the engine directly. This one
proves the seam: that ``mockdocs/serve.py`` registers the interference
middleware on the shared FastMCP server, that the middleware sees whole agent
tool calls with their names and arguments, and that the resulting record
reaches the harness through the state snapshot.

It also records a finding rather than only a mechanism. The preview tools
already carry a suggestion ledger, so when the agent's target is resolved by
somebody else the error it gets back names the cause -- "removed between that
read and this call -- most likely by another editor" -- instead of a bare 400.
That message is the tool surface's answer to "can an agent notice concurrent
change at all", and the assertion below is what stops it regressing silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from llmux.runner.interference import (  # noqa: E402
    ENV_VAR,
    InterferenceReport,
    build_script,
    declared_interferences,
)
from llmux.runner.scenarios import load_scenario  # noqa: E402
from mockdocs.state import read_state  # noqa: E402
from tests.mockdocs.test_serve_smoke import _Session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "llmux" / "interference"
EMAIL = "mockuser@example.com"
DOCUMENT_ID = "ix-doc-launch"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A server armed with ix-vanished-id's scripted second editor."""
    scenario = load_scenario(CORPUS / "ix-vanished-id")
    root = tmp_path_factory.mktemp("ix-live")
    state_path = root / "state.json"
    script_path = root / "interference.json"
    script_path.write_text(
        json.dumps(build_script(declared_interferences(scenario))), encoding="utf-8"
    )
    with _Session(
        scenario.seed_path.resolve(),
        env_extra={
            ENV_VAR: str(script_path),
            "MOCKDOCS_STATE_DUMP": str(state_path),
            "MOCKDOCS_ME": scenario.me,
        },
    ) as session:
        yield session, state_path


def test_the_other_editor_fires_inside_a_real_tool_call(live):
    session, state_path = live

    listed = session.call_json(
        "list_document_suggestions",
        {"user_google_email": EMAIL, "document_id": DOCUMENT_ID},
    )
    # The listing answers with the document as it was: the interference fires
    # on the way out, which is what makes the agent's picture stale rather
    # than merely wrong.
    assert "sug.bob.2" in {s["suggestion_id"] for s in listed["suggestions"]}

    report = InterferenceReport.from_backend(read_state(state_path))
    assert report is not None
    assert report.fired_names == ["bob-accepts-own-fix"]
    fired = report.fire("bob-accepts-own-fix")
    assert fired.at_call == 1
    assert fired.at_tool == "list_document_suggestions"
    assert fired.effect["existed"] is True
    assert report.violations == []
    assert report.vanished_ids == {"sug.bob.2": 1}


def test_the_error_the_agent_gets_back_blames_the_other_editor(live):
    """Can an agent notice at all? On this path, yes -- the tool says so."""
    session, state_path = live

    result = session._run(
        session._client.call_tool(
            "manage_document_suggestion",
            {
                "user_google_email": EMAIL,
                "document_id": DOCUMENT_ID,
                "action": "accept",
                "suggestion_id": "sug.bob.2",
            },
            timeout=60,
            raise_on_error=False,
        ),
        90,
    )
    assert result.is_error
    text = "\n".join(b.text for b in result.content if getattr(b, "text", None))
    assert "no longer exists" in text
    assert "another editor" in text, (
        "the suggestion ledger no longer distinguishes a concurrent removal "
        "from the agent's own; that distinction is the only in-band signal an "
        "agent gets that the document moved"
    )

    report = InterferenceReport.from_backend(read_state(state_path))
    calls = {c.ordinal: c for c in report.calls}
    assert calls[2].tool == "manage_document_suggestion"
    assert calls[2].ok is False
    assert calls[2].args["suggestion_id"] == "sug.bob.2"


def test_the_snapshot_stays_in_step_with_the_interleaving(live):
    """The grader reads a file, so the file has to be current after a firing."""
    session, state_path = live

    session.call_json(
        "list_document_suggestions",
        {"user_google_email": EMAIL, "document_id": DOCUMENT_ID},
    )
    backend = read_state(state_path)
    doc = backend.documents[DOCUMENT_ID]
    assert "sug.bob.2" not in doc.registry
    assert "team" in doc.display_text()
    assert backend.concurrency.declared == ["bob-accepts-own-fix"]
