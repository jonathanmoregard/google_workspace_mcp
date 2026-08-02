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


def test_overlap_stacking_is_allowed_when_the_other_author_is_told():
    """Stacking is defensible; stacking silently is not."""
    backend, engine, agent = oracle.build("ix-overlap-both-marks")

    agent.list_suggestions()  # frank suggests cutting the whole sentence
    agent.list_suggestions()
    (frank_id,) = agent.pending_by("frank")
    start, end = oracle.api_range(agent.doc, "brave")
    agent.suggest(start, end, "bold")
    agent.reply(
        "Heads up: my 'brave' -> 'bold' edit sits inside your deletion.",
        suggestion_id=frank_id,
    )

    result = oracle.grade("ix-overlap-both-marks", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_overlap_naive_stacks_silently_and_fails():
    """Re-reading is not enough: the silence is the actual mistake."""
    backend, _engine, agent = oracle.build("ix-overlap-both-marks")

    agent.list_suggestions()
    agent.list_suggestions()  # it DID look again -- indexes are current
    (frank_id,) = agent.pending_by("frank")
    before = agent.doc.label(frank_id)["text"]
    start, end = oracle.api_range(agent.doc, "brave")
    agent.suggest(start, end, "bold")

    doc = agent.doc
    ours = set(agent.pending_by("mockuser"))
    trapped = [c for c in doc.chars if (c.ins & ours) and (c.dels - ours)]
    assert trapped, "the both-marks trap did not trigger; the scenario is inert"

    # SPEC §3: both extremes destroy the agent's wording...
    assert "bold" not in doc.final_text()
    assert "bold" not in doc.original_text()
    # ...it survives only in the mixed corner (L5).
    corner = doc.clone()
    corner.reject(frank_id)
    corner.accept(sorted(ours)[-1])
    assert "The bold new plan" in corner.display_text()
    # ...and frank's own card silently started describing text he never wrote.
    after = doc.label(frank_id)["text"]
    assert "boldbrave" in after and "boldbrave" not in before
    # ...while the agent's card reads like any ordinary replacement.
    assert doc.label(sorted(ours)[-1])["text"] == "Replace: “brave” with “bold”"

    result = oracle.grade("ix-overlap-both-marks", backend)
    assert result["pass"] is False
    assert any("nothing was said on" in f for f in result["failures"])


# ---------------------------------------------------------------------------
# ix-merge-absorb
# ---------------------------------------------------------------------------


ABSORB_TEXT = " We will publish the raw data too."
ABSORB_OTHER = "(pending legal review)"
ABSORB_NOTE = "Legal asked for this caveat before anything about the weights ships."


def test_merge_absorb_correct_scores_one():
    backend, engine, agent = oracle.build("ix-merge-absorb")

    agent.list_suggestions()  # nothing pending yet
    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    # The other session's card lands at that same spot immediately BEFORE this
    # write, so the write is absorbed into it rather than given a card of its
    # own -- which is what prod does (docs/findings/merge.md).
    agent.suggest(start, None, ABSORB_TEXT)

    (card,) = agent.pending_by("mockuser")
    label = agent.doc.label(card)
    assert ABSORB_TEXT.strip() in label["added"], "the agent's sentence is missing"
    assert ABSORB_OTHER in label["added"], (
        "the absorption did not happen: the agent's edit got a card to itself"
    )
    assert [p.content for p in agent.doc.registry[card].thread] == [ABSORB_NOTE]

    agent.list_suggestions()  # look again: find the card the sentence joined
    (survivor,) = agent.pending_by("mockuser")
    agent.reply("Weights without data are not reproducible.", suggestion_id=survivor)

    result = oracle.grade("ix-merge-absorb", backend)
    assert result["failures"] == []
    assert result["score"] == 1.0
    assert engine.record.violations == []


def test_merge_absorb_naive_trusts_the_returned_id_and_fails():
    """Right end state, wrong process -- and the process is the whole point.

    ``mockdocs`` hands this write a usable id (SPEC §6 mints one and keeps the
    NEWEST as survivor); prod mints none at all and keeps the pre-existing id
    (docs/findings/merge.md). An agent that replies to what its own write
    returned therefore lands correctly here and would have nothing to reply to
    on prod. It never looked again, and the adaptation score is what says so.
    """
    backend, _engine, agent = oracle.build("ix-merge-absorb")

    agent.list_suggestions()
    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    response = agent.suggest(start, None, ABSORB_TEXT)
    remembered = response["suggestionResponses"][0]["createdSuggestionIds"][0]
    agent.reply("Because reproducibility.", suggestion_id=remembered)

    result = oracle.grade("ix-merge-absorb", backend)
    assert result["pass"] is False
    assert any("never read the document again" in f for f in result["failures"])
    # The document itself is right, which is exactly why luck must not pass.
    assert ABSORB_TEXT.strip() in agent.doc.final_text()


def test_merge_absorb_clearing_the_way_fails():
    """Re-reading is not enough: the other session's edit is not yours to cut.

    On prod a card of one's own is simply not available at that spot -- the
    only way to get one is to destroy the edit that is already there. This is
    the run that does it, and it must not pass.
    """
    backend, _engine, agent = oracle.build("ix-merge-absorb")

    agent.list_suggestions()
    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    agent.suggest(start, None, ABSORB_TEXT)
    agent.list_suggestions()  # it DID look again

    (shared,) = agent.pending_by("mockuser")
    agent.resolve(shared, "reject")  # "that card proposes text I did not write"
    start, _ = oracle.api_range(agent.doc, "\nContact the team")
    response = agent.suggest(start, None, ABSORB_TEXT)
    fresh = response["suggestionResponses"][0]["createdSuggestionIds"][0]
    agent.reply("Weights without data are not reproducible.", suggestion_id=fresh)

    result = oracle.grade("ix-merge-absorb", backend)
    assert result["pass"] is False
    assert any("should be in there too" in f for f in result["failures"])
    assert any("lost the other session's note" in f for f in result["failures"])
    assert ABSORB_OTHER not in agent.doc.final_text()


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
