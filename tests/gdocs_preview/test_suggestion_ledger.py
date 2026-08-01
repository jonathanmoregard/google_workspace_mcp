"""Unit tests for the suggestion ledger's bookkeeping and honesty ladder.

The ledger is what lets a "suggestion X does not exist" error say WHY, so
the tests are mostly about what it must NOT claim: no cause it did not
observe, no attribution across users, and no unbounded growth in a
long-lived server process. The tool-level wiring is covered in
tests/gdocs_preview/test_write_tools.py.
"""

import pytest

from gdocs_preview import suggestion_ledger as ledger

USER = "reviewer@example.com"
OTHER = "someone.else@example.com"
DOC = "doc-1"

RECORD_A = {
    "suggestion_id": "sug.a",
    "type": "replacement",
    "pre_text": "morning",
    "post_text": "evening",
    "context_before": "Good ",
    "context_after": "\n",
    "start_index": 6,
    "end_index": 20,
    "summary_text": "Replace: “morning” with “evening”",
    "status": "OPEN",
    # Dropped by the cache: never worth an LLM context window.
    "replies": [{"post_id": "p1", "content": "x" * 500}],
    "author": {"display_name": "Alice"},
}
RECORD_B = {"suggestion_id": "sug.b", "pre_text": " cruel"}


@pytest.fixture(autouse=True)
def _reset():
    ledger.reset()
    yield
    ledger.reset()


class TestObservation:
    def test_never_looked_is_distinguishable_from_looked_and_empty(self):
        assert ledger.snapshot(USER, DOC) is None
        ledger.observe(USER, DOC, [], complete=True)
        assert ledger.snapshot(USER, DOC).ids == frozenset()

    def test_observe_replaces_the_whole_set(self):
        ledger.observe(USER, DOC, [RECORD_A, RECORD_B], complete=True)
        assert ledger.snapshot(USER, DOC).ids == {"sug.a", "sug.b"}
        ledger.observe(USER, DOC, [RECORD_B], complete=True)
        assert ledger.snapshot(USER, DOC).ids == {"sug.b"}

    def test_only_the_echo_fields_are_cached(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        cached = ledger.record_of(USER, DOC, "sug.a")
        assert cached["pre_text"] == "morning"
        assert cached["summary_text"] == "Replace: “morning” with “evening”"
        assert "replies" not in cached
        assert "author" not in cached

    def test_records_without_an_id_are_ignored(self):
        ledger.observe(
            USER, DOC, [{"pre_text": "orphan"}, None, RECORD_A], complete=True
        )
        assert ledger.snapshot(USER, DOC).ids == {"sug.a"}

    def test_record_of_returns_a_copy(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_of(USER, DOC, "sug.a")["pre_text"] = "mutated"
        assert ledger.record_of(USER, DOC, "sug.a")["pre_text"] == "morning"


class TestHonestyLadder:
    def test_proven_when_we_resolved_that_very_id(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", landed=True)
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "You accepted it yourself" in message
        assert "MAY" not in message

    def test_collateral_states_the_observation_then_the_rule(self):
        ledger.observe(USER, DOC, [RECORD_A, RECORD_B], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", ["sug.b"], landed=True)
        message = ledger.explain_missing(USER, DOC, "sug.b")
        assert "still listed before you accepted 'sug.a'" in message
        assert "gone from the read right after" in message
        assert "last marked character" in message

    def test_may_have_been_removed_never_asserts_a_cause(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", landed=True)
        message = ledger.explain_missing(USER, DOC, "sug.unrelated")
        assert "not proven" in message
        assert "MAY have removed it" in message
        assert "'sug.a'" in message

    def test_never_seen_points_at_the_id(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        message = ledger.explain_missing(USER, DOC, "sug.typo")
        assert "most likely the id is wrong" in message

    def test_no_read_at_all_says_so(self):
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "has not read this document" in message

    def test_present_in_the_last_read_blames_nobody(self):
        """Still listed a moment ago and gone now, with no write of ours in
        between: the only honest answer is "someone else"."""
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "WAS present in the last read" in message
        assert "another editor" in message

    def test_merge_collateral_reads_as_a_merge_not_a_gc(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(
            USER, DOC, "suggest_doc_edit", "sug.new", ["sug.a"], landed=True
        )
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "merges into the new one" in message
        assert "'sug.new'" in message

    def test_a_degraded_read_does_not_diagnose_the_callers_id(self):
        """Round 6, the ledger's own instance of the class: a GA-fallback read
        REPLACES the record set with the one unnamed body it can see, so a
        card in another tab silently leaves the ledger. Rung 4 then answered
        "most likely the id is wrong" -- a diagnosis of the caller drawn from
        a read that never looked where the id lives."""
        ledger.observe(USER, DOC, [RECORD_A], complete=False)
        message = ledger.explain_missing(USER, DOC, "sug.elsewhere")
        assert "most likely the id is wrong" not in message, message
        assert "could not see every tab" in message
        assert "not evidence about the id" in message

    def test_a_complete_read_still_says_the_id_is_probably_wrong(self):
        """The control: coverage is what licenses rung 4."""
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        assert "most likely the id is wrong" in ledger.explain_missing(
            USER, DOC, "sug.elsewhere"
        )

    def test_a_snapshot_carries_the_coverage_of_the_read_behind_it(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=False)
        assert ledger.snapshot(USER, DOC).complete is False
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        assert ledger.snapshot(USER, DOC).complete is True

    def test_a_resolution_that_did_not_land_is_never_answered_as_proven(self):
        """The rung-1 answer is causation, and it needs the write to have
        worked. An HTTP 200 that resolves NOTHING is a shape prod returns:
        ``manage_document_suggestion`` derives ``still_pending: true`` for it
        and used to file the id as resolved anyway, so every later "does not
        exist" was answered "You accepted it yourself" -- the one explanation
        this module has evidence against, pointing the agent away from the
        real cause."""
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", landed=False)
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "You accepted it yourself" not in message, message
        assert "still listed it as pending" in message
        assert "did not remove it" in message
        assert "another editor" in message

    def test_an_unverified_resolution_is_offered_as_likely_not_proven(self):
        """``verify=false`` and a failed post-write read both buy the same
        thing: the request was accepted and nothing observed its effect. The
        memory is still worth keeping -- it just may not be stated as fact."""
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", landed=None)
        message = ledger.explain_missing(USER, DOC, "sug.a")
        assert "You accepted it yourself" not in message, message
        assert "nothing here verified" in message
        assert "rather than a proven one" in message

    def test_collateral_of_a_write_that_did_not_land_is_not_blamed_on_it(self):
        """The GC rule explains a removal caused by OUR resolution. If that
        resolution did not take effect, it cannot be what removed the
        neighbour, and the note may not say it was."""
        ledger.observe(USER, DOC, [RECORD_A, RECORD_B], complete=True)
        resolutions = ledger.record_resolution(
            USER, DOC, "accept", "sug.a", ["sug.b"], landed=False
        )
        (collateral,) = [r for r in resolutions if not r.direct]
        note = ledger.collateral_note(collateral)
        assert "also removed it" not in note, note
        assert "did NOT take effect" in note
        assert "another editor" in note

    def test_landed_is_carried_on_the_record(self):
        (resolution,) = ledger.record_resolution(
            USER, DOC, "reject", "sug.a", landed=False
        )
        assert resolution.landed is False
        assert resolution.as_dict()["landed"] is False

    def test_collateral_without_a_known_cause_names_none(self):
        """The API can omit createdSuggestionIds; the note must not invent
        an id to blame."""
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        (resolution,) = ledger.record_resolution(
            USER, DOC, "suggest_doc_edit", "", ["sug.a"], landed=True
        )
        assert resolution.cause is None
        assert resolution.direct is False
        assert "''" not in ledger.collateral_note(resolution)
        assert "merges." in ledger.collateral_note(resolution)


class TestIsolationAndBounds:
    def test_users_do_not_see_each_others_resolutions(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.record_resolution(USER, DOC, "accept", "sug.a", landed=True)
        assert "has not read this document" in ledger.explain_missing(
            OTHER, DOC, "sug.a"
        )

    def test_documents_are_tracked_separately(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        assert ledger.snapshot(USER, "doc-2") is None

    def test_document_count_is_bounded(self):
        for n in range(ledger.MAX_DOCUMENTS + 10):
            ledger.observe(USER, f"doc-{n}", [RECORD_A], complete=True)
        assert len(ledger._entries) == ledger.MAX_DOCUMENTS
        # The oldest went first; the newest is still there.
        assert ledger.snapshot(USER, "doc-0") is None
        assert ledger.snapshot(USER, f"doc-{ledger.MAX_DOCUMENTS + 9}").ids == {"sug.a"}

    def test_touching_a_document_keeps_it_alive(self):
        ledger.observe(USER, "doc-keep", [RECORD_A], complete=True)
        for n in range(ledger.MAX_DOCUMENTS - 1):
            ledger.observe(USER, f"doc-{n}", [RECORD_A], complete=True)
        ledger.observe(USER, "doc-keep", [RECORD_A], complete=True)  # re-touch
        ledger.observe(USER, "doc-overflow", [RECORD_A], complete=True)
        assert ledger.snapshot(USER, "doc-keep").ids == {"sug.a"}

    def test_resolution_count_is_bounded(self):
        for n in range(ledger.MAX_RESOLUTIONS + 5):
            ledger.record_resolution(USER, DOC, "accept", f"sug.{n}", landed=True)
        entry = ledger._entries[(USER, DOC)]
        assert len(entry.resolutions) == ledger.MAX_RESOLUTIONS
        assert "sug.0" not in entry.resolutions

    def test_reset_forgets_everything(self):
        ledger.observe(USER, DOC, [RECORD_A], complete=True)
        ledger.reset()
        assert ledger.snapshot(USER, DOC) is None
