"""Unit tests for the shared end-state grader.

Two things have to hold for the corpus to be worth running: a correct end
state scores 1.0 whatever path reached it, and a partly-correct one scores
strictly between 0 and 1. A grader that only ever returns pass/fail throws
away most of the signal an LLM-UX run produces.
"""

from __future__ import annotations

from llmux.scenarios.grading import grade_against
from llmux.scenarios.primitives import (
    SeedBuilder,
    add,
    after,
    remove,
    seeded_backend,
    span,
)


def _fixture():
    b = SeedBuilder(base_text="one two three\n", document_id="d-grade")
    b.move("cut", remove("alice", span(" two")))
    b.move("ins", add("bob", after("three"), " four"))
    return b


def _expected(display, surviving, resolved, **extra):
    payload = {
        "document_id": "d-grade",
        "final_text": display,
        "surviving_suggestion_ids": surviving,
        "resolved": resolved,
        "thread_expectations": [],
        "invariant_checks": [],
    }
    payload.update(extra)
    return payload


class TestEndStateGrading:
    def test_correct_end_state_scores_one(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        doc.accept(b.named["cut"])
        doc.reject(b.named["ins"])
        verdict = grade_against(
            backend,
            _expected(
                "one three\n",
                [],
                {b.named["cut"]: "accepted", b.named["ins"]: "rejected"},
            ),
        )
        assert verdict == {"pass": True, "score": 1.0, "failures": []}

    def test_order_does_not_matter(self):
        """L3: the click order cannot change the verdict."""
        b = _fixture()
        expected = _expected(
            "one three\n", [], {b.named["cut"]: "accepted", b.named["ins"]: "rejected"}
        )
        forwards, doc_f = seeded_backend(b.seed_spec())
        doc_f.accept(b.named["cut"])
        doc_f.reject(b.named["ins"])
        backwards, doc_b = seeded_backend(b.seed_spec())
        doc_b.reject(b.named["ins"])
        doc_b.accept(b.named["cut"])
        assert grade_against(forwards, expected) == grade_against(backwards, expected)

    def test_half_done_earns_partial_credit(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        doc.accept(b.named["cut"])  # the other one is left pending
        verdict = grade_against(
            backend,
            _expected(
                "one three\n",
                [],
                {b.named["cut"]: "accepted", b.named["ins"]: "rejected"},
            ),
        )
        assert verdict["pass"] is False
        assert 0.0 < verdict["score"] < 1.0
        assert any("still pending" in f for f in verdict["failures"])

    def test_untouched_document_scores_low_but_not_zero(self):
        b = _fixture()
        backend, _ = seeded_backend(b.seed_spec())
        verdict = grade_against(
            backend,
            _expected(
                "one three\n",
                [],
                {b.named["cut"]: "accepted", b.named["ins"]: "rejected"},
            ),
        )
        assert verdict["pass"] is False
        assert verdict["score"] < 0.5

    def test_missing_document_is_zero(self):
        b = _fixture()
        backend, _ = seeded_backend(b.seed_spec())
        backend.documents.clear()
        verdict = grade_against(backend, _expected("x", [], {}))
        assert verdict == {
            "pass": False,
            "score": 0.0,
            "failures": [
                "document d-grade is not in the backend (have: [])",
            ],
        }


class TestInvariantChecks:
    def test_projection_checks_pin_the_mark_state(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        doc.accept(b.named["cut"])
        expected = _expected(
            "one three four\n",
            [b.named["ins"]],
            {b.named["cut"]: "accepted"},
            invariant_checks=[
                {"check": "model_invariants"},
                {
                    "check": "projection_text",
                    "projection": "original",
                    "equals": "one three\n",
                },
                {
                    "check": "projection_text",
                    "projection": "final",
                    "equals": "one three four\n",
                },
                {"check": "suggestion_count", "equals": 1},
                {
                    "check": "authored_suggestion_count",
                    "author": "bob",
                    "equals": 1,
                },
                {"check": "text_present", "text": "three"},
                {"check": "text_absent", "text": "two"},
                {"check": "comment_count", "equals": 0},
            ],
        )
        assert grade_against(backend, expected)["pass"] is True

    def test_unknown_check_is_a_failure_not_a_crash(self):
        b = _fixture()
        backend, _ = seeded_backend(b.seed_spec())
        verdict = grade_against(
            backend,
            _expected(
                b.doc.display_text(),
                sorted(b.doc.registry),
                {},
                invariant_checks=[{"check": "does-not-exist"}],
            ),
        )
        assert verdict["pass"] is False
        assert any("unknown invariant check" in f for f in verdict["failures"])

    def test_malformed_check_is_caught(self):
        b = _fixture()
        backend, _ = seeded_backend(b.seed_spec())
        verdict = grade_against(
            backend,
            _expected(
                b.doc.display_text(),
                sorted(b.doc.registry),
                {},
                invariant_checks=[{"check": "suggestion_count"}],  # no "equals"
            ),
        )
        assert any("KeyError" in f for f in verdict["failures"])


class TestThreadExpectations:
    def test_reply_expectation_matches_author_and_regex(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        sid = b.named["ins"]
        backend.add_suggestion_reply(doc, sid, "Where does this come from?")
        expected = _expected(
            b.doc.display_text(),
            sorted(doc.registry),
            {},
            thread_expectations=[
                {
                    "kind": "suggestion_reply",
                    "suggestion_id": sid,
                    "author": backend.me,
                    "content_regex": r"\?\s*$",
                    "min_count": 1,
                }
            ],
        )
        assert grade_against(backend, expected)["pass"] is True

        missing = dict(expected)
        missing["thread_expectations"] = [
            {**expected["thread_expectations"][0], "content_regex": r"^Hello"}
        ]
        assert grade_against(backend, missing)["pass"] is False

    def test_reply_on_a_resolved_suggestion_reports_the_lost_thread(self):
        """§7 takes the thread with the suggestion, and the mock keeps no
        resolved-comments log, so this is unverifiable by construction."""
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        sid = b.named["ins"]
        backend.add_suggestion_reply(doc, sid, "why?")
        doc.accept(sid)
        verdict = grade_against(
            backend,
            _expected(
                doc.display_text(),
                sorted(doc.registry),
                {},
                thread_expectations=[
                    {"kind": "suggestion_reply", "suggestion_id": sid, "min_count": 1}
                ],
            ),
        )
        assert any("no longer pending" in f for f in verdict["failures"])

    def test_comment_thread_quote_must_match_exactly(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        backend.create_comment_thread(doc.document_id, content="Check", quote="three")
        expected = _expected(
            b.doc.display_text(),
            sorted(doc.registry),
            {},
            thread_expectations=[
                {"kind": "comment_thread", "quote": "three", "min_count": 1},
                {"kind": "comment_count", "equals": 1},
            ],
        )
        assert grade_against(backend, expected)["pass"] is True

        off_by_one = dict(expected)
        off_by_one["thread_expectations"] = [
            {"kind": "comment_thread", "quote": " three", "min_count": 1}
        ]
        verdict = grade_against(backend, off_by_one)
        assert verdict["pass"] is False
        assert "quotes present" in verdict["failures"][0]

    def test_no_reply_expectation(self):
        b = _fixture()
        backend, doc = seeded_backend(b.seed_spec())
        sid = b.named["ins"]
        expected = _expected(
            b.doc.display_text(),
            sorted(doc.registry),
            {},
            thread_expectations=[{"kind": "no_suggestion_reply", "suggestion_id": sid}],
        )
        assert grade_against(backend, expected)["pass"] is True
        backend.add_suggestion_reply(doc, sid, "chatty")
        assert grade_against(backend, expected)["pass"] is False
