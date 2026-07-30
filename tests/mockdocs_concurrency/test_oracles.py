"""Oracle validation: every interference scenario must be decidable.

Two claims per scenario, and both have to hold or the scenario proves nothing:

* a scripted **correct** solution scores exactly 1.0 -- so a passing run is
  reachable, and a failing agent is failing for a reason;
* a scripted **naive** solution, which does the sensible single-writer thing
  and ignores the concurrent change, **FAILS** -- so the scenario is actually
  measuring the concurrency, not the base task.

The naive solutions are written to be *only* as wrong as the interference
makes them. Where that matters they even re-read the document, so the failure
that remains is the one the scenario is about rather than general sloppiness.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from tests.mockdocs_concurrency import oracle

SCENARIOS = (
    "ix-vanished-id",
    "ix-stale-indexes",
    "ix-thread-gone",
    "ix-overlap-both-marks",
    "ix-merge-absorb",
)


def _expect_http_error(fn):
    """Run ``fn`` expecting the mock to reject it; return the error text."""
    with pytest.raises(HttpError) as caught:
        fn()
    return str(caught.value)


# ---------------------------------------------------------------------------
# ix-vanished-id
# ---------------------------------------------------------------------------


def test_vanished_id_correct_scores_one():
    backend, engine, agent = oracle.build("ix-vanished-id")

    agent.list_suggestions()  # bob accepts sug.bob.2 right after this
    message = _expect_http_error(lambda: agent.resolve("sug.bob.2", "accept"))
    assert "sug.bob.2" in message

    agent.list_suggestions()  # look again: the fix is already in the text
    assert "sug.bob.2" not in agent.doc.registry
    agent.resolve("sug.carol.1", "reject")

    result = oracle.grade("ix-vanished-id", backend)
    assert result["failures"] == []
    assert result["pass"] is True
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_vanished_id_naive_blind_retry_fails():
    """Right end state, wrong process: the dead id is simply hammered again."""
    backend, _engine, agent = oracle.build("ix-vanished-id")

    agent.list_suggestions()
    _expect_http_error(lambda: agent.resolve("sug.bob.2", "accept"))
    _expect_http_error(lambda: agent.resolve("sug.bob.2", "accept"))
    agent.resolve("sug.carol.1", "reject")

    result = oracle.grade("ix-vanished-id", backend)
    assert result["pass"] is False
    assert any("blind retry" in f for f in result["failures"])
    # The text is right -- which is the point: persistence must not buy a pass.
    assert not agent.doc.registry
    assert "team" in agent.doc.final_text()


# ---------------------------------------------------------------------------
# ix-stale-indexes
# ---------------------------------------------------------------------------


def test_stale_indexes_correct_scores_one():
    backend, engine, agent = oracle.build("ix-stale-indexes")

    agent.list_suggestions()  # dana's banner lands right after this
    fresh = agent.list_suggestions()  # re-read, and use THIS payload's indexes
    start, end = oracle.api_range_in_payload(fresh, "200 users")
    agent.suggest(start, end, "2,000 users")

    result = oracle.grade("ix-stale-indexes", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_stale_indexes_naive_write_succeeds_and_still_fails():
    """The silent case: no error at all, and the edit lands in the banner."""
    backend, engine, agent = oracle.build("ix-stale-indexes")

    # The read answers with the pre-change document; the banner lands on the
    # way out. Indexes taken from THIS payload are the ones a single-writer
    # agent would carry into its write, and they are already stale.
    payload = agent.list_suggestions()
    start, end = oracle.api_range_in_payload(payload, "200 users")

    shift = engine.record.fired[0].effect["utf16_shift"]
    assert shift == 27, "the banner should move every index by 27 UTF-16 units"
    assert oracle.api_range(agent.doc, "200 users") == (start + shift, end + shift)

    agent.suggest(start, end, "2,000 users")  # must NOT raise: that is the point

    assert "200 users" in agent.doc.final_text(), (
        "the figure was never corrected, and nothing errored to say so"
    )
    result = oracle.grade("ix-stale-indexes", backend)
    assert result["pass"] is False
    assert any("wrote with indexes computed before" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# ix-thread-gone
# ---------------------------------------------------------------------------


def test_thread_gone_correct_scores_one():
    backend, engine, agent = oracle.build("ix-thread-gone")

    agent.list_comments()  # erin deletes her thread right after this
    message = _expect_http_error(
        lambda: agent.reply("I would keep it.", comment_id="comment.1")
    )
    assert "comment.1" in message
    agent.list_comments()  # find out what happened, and stop

    result = oracle.grade("ix-thread-gone", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_thread_gone_naive_retries_and_fails():
    backend, _engine, agent = oracle.build("ix-thread-gone")

    agent.list_comments()
    _expect_http_error(lambda: agent.reply("I would keep it.", comment_id="comment.1"))
    _expect_http_error(lambda: agent.reply("I would keep it.", comment_id="comment.1"))

    result = oracle.grade("ix-thread-gone", backend)
    assert result["pass"] is False
    assert any("never listed the comments again" in f for f in result["failures"])
    assert any("blind retry" in f for f in result["failures"])


def test_thread_gone_penalises_routing_around_the_problem():
    """Editing the document because the comment route broke is not adaptation."""
    backend, _engine, agent = oracle.build("ix-thread-gone")

    agent.list_comments()
    _expect_http_error(lambda: agent.reply("I would keep it.", comment_id="comment.1"))
    agent.list_comments()
    start, end = oracle.api_range(agent.doc, "Risks are unchanged.\n")
    agent.suggest(start, end, None)  # "I could not reply, so I cut the line"

    result = oracle.grade("ix-thread-gone", backend)
    assert result["pass"] is False
    assert any("comment-only task" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# ix-overlap-both-marks
# ---------------------------------------------------------------------------


def test_overlap_correct_scores_one():
    backend, engine, agent = oracle.build("ix-overlap-both-marks")

    agent.list_suggestions()  # frank suggests cutting the whole sentence
    agent.list_suggestions()
    (frank_id,) = agent.pending_by("frank")
    agent.reply(
        "If this sentence stays, 'brave' should read 'bold'.",
        suggestion_id=frank_id,
    )

    result = oracle.grade("ix-overlap-both-marks", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_overlap_naive_stacks_inside_the_deletion_and_fails():
    """Re-reading is not enough: stacking the edit is the actual mistake."""
    backend, _engine, agent = oracle.build("ix-overlap-both-marks")

    agent.list_suggestions()
    agent.list_suggestions()  # it DID look again -- indexes are current
    start, end = oracle.api_range(agent.doc, "brave")
    agent.suggest(start, end, "bold")

    doc = agent.doc
    ours = set(agent.pending_by("mockuser"))
    trapped = [c for c in doc.chars if (c.ins & ours) and (c.dels - ours)]
    assert trapped, "the both-marks trap did not trigger; the scenario is inert"
    assert "bold" not in doc.final_text()
    assert "bold" not in doc.original_text()

    result = oracle.grade("ix-overlap-both-marks", backend)
    assert result["pass"] is False
    assert any("neither the accept-everything" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# ix-merge-absorb
# ---------------------------------------------------------------------------


def test_merge_absorb_correct_scores_one():
    backend, engine, agent = oracle.build("ix-merge-absorb")

    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    response = agent.suggest(start, None, " We will publish the raw data too.")
    created = [
        sid
        for entry in response["suggestionResponses"]
        for sid in entry["createdSuggestionIds"]
    ]
    assert created, "the agent's own write reported no suggestion id"
    remembered = created[0]

    # The second session's edit merged; the remembered id has been absorbed.
    message = _expect_http_error(
        lambda: agent.reply(
            "Weights without data are not reproducible.", suggestion_id=remembered
        )
    )
    assert remembered in message

    agent.list_suggestions()
    (survivor,) = agent.pending_by("mockuser")
    assert survivor != remembered
    agent.reply("Weights without data are not reproducible.", suggestion_id=survivor)

    result = oracle.grade("ix-merge-absorb", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_merge_absorb_naive_retries_the_absorbed_id_and_fails():
    backend, _engine, agent = oracle.build("ix-merge-absorb")

    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    response = agent.suggest(start, None, " We will publish the raw data too.")
    remembered = response["suggestionResponses"][0]["createdSuggestionIds"][0]

    for _ in range(2):
        _expect_http_error(
            lambda: agent.reply("Because reproducibility.", suggestion_id=remembered)
        )

    result = oracle.grade("ix-merge-absorb", backend)
    assert result["pass"] is False
    assert any("carries no reply from the agent" in f for f in result["failures"])
    assert any("blind retry" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# corpus-wide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_scenario_satisfies_the_corpus_contract(scenario_id):
    """Same frozen contract as the generated corpus, plus a live script."""
    from llmux.runner.interference import declared_interferences
    from llmux.runner.scenarios import load_scenario

    scenario = load_scenario(oracle.CORPUS / scenario_id)
    assert scenario.id == scenario_id
    assert scenario.brief.strip()
    assert scenario.expected.get("document_id")
    declared = declared_interferences(scenario)
    assert declared, f"{scenario_id} declares no interference"
    assert len({i.name for i in declared}) == len(declared)


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_seed_state_alone_never_passes(scenario_id):
    """The untouched seed must fail, or the scenario measures nothing."""
    backend, _engine, _agent = oracle.build(scenario_id)
    result = oracle.grade(scenario_id, backend)
    assert result["pass"] is False
    # ...and it fails as a HARNESS fault, because nothing fired.
    assert any(f.startswith("HARNESS:") for f in result["failures"])
