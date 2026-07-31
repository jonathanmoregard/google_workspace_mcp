"""Unit tests for the scenario algebra.

The primitives are the part of the generator that a scenario author reasons
about directly, so the tests here pin the behaviours a wrong scenario would
silently rely on: grapheme-space addressing (not code-point), merge-following
names, and predicates that separate the real thing from the decoy.
"""

from __future__ import annotations

import pytest

from llmux.scenarios.primitives import (
    ScenarioError,
    SeedBuilder,
    add,
    adds_text,
    after,
    always,
    before,
    by_author,
    decide,
    deletes_part_of,
    grapheme_spans,
    is_noop,
    kind_is,
    ordered_suggestion_ids,
    p_and,
    p_not,
    p_or,
    remove,
    rewrite,
    Rule,
    seeded_backend,
    select,
    span,
    spans_paragraph_break,
)
from mockdocs.model import MockDoc

#: 'e' + combining acute: one grapheme, two code points -- so every index
#: after it differs between grapheme space and Python string space.
EMOJI_TEXT = "Release \U0001f389 notes for the café team\n"


def _doc(text: str = EMOJI_TEXT) -> MockDoc:
    return MockDoc(text=text, document_id="d", title="t")


class TestLocators:
    def test_spans_are_grapheme_indexes_not_code_point_indexes(self):
        doc = _doc()
        ((start, end),) = grapheme_spans(doc, "team")
        assert MockDoc.text_of(doc.chars[start:end]) == "team"
        # Python string space counts the combining acute separately, so a
        # scenario that located ranges with str.index would be one off here.
        assert start == doc.display_text().index("team") - 1

    def test_zwj_sequence_is_one_grapheme(self):
        doc = _doc("a \U0001f468‍\U0001f469‍\U0001f467 b\n")
        ((start, end),) = grapheme_spans(doc, "b")
        # 'a', ' ', family, ' ', 'b' -> the family is a single array slot.
        assert (start, end) == (4, 5)

    def test_before_and_after_collapse(self):
        doc = _doc()
        assert before("notes").resolve(doc) == after("Release \U0001f389 ").resolve(doc)
        start, end = span("notes").resolve(doc)
        assert before("notes").resolve(doc) == (start, start)
        assert after("notes").resolve(doc) == (end, end)

    def test_missing_occurrence_is_an_error(self):
        doc = _doc()
        with pytest.raises(ScenarioError):
            span("notes", 1).resolve(doc)
        with pytest.raises(ScenarioError):
            span("nowhere").resolve(doc)


class TestSeedBuilder:
    def test_ops_carry_concrete_model_indexes(self):
        b = SeedBuilder(base_text="hello world\n", document_id="d")
        b.move("ins", add("alice", before("world"), "big "))
        (op,) = b.ops
        assert op == {"op": "insert", "index": 6, "text": "big ", "author": "alice"}

    def test_named_ids_follow_a_merge(self):
        """§6 absorbs the earlier suggestion; both names must point at the
        survivor, or every later reference addresses a dead id."""
        b = SeedBuilder(base_text="keep guidance here\n", document_id="d")
        first = b.move("insert", add("carol", before("guidance"), "stale "))
        second = b.move("delete", remove("carol", span("guidance")))
        assert b.doc.merge_log, "abutting same-author edits must merge"
        assert (first, second) in b.doc.merge_log or (
            second,
            first,
        ) in b.doc.merge_log
        assert b.named["insert"] == b.named["delete"]
        assert b.named["insert"] in b.doc.registry
        assert len(b.doc.registry) == 1

    def test_seed_spec_round_trips_through_the_backend(self):
        b = SeedBuilder(base_text="alpha beta\n", document_id="d-1", title="T")
        b.move("a", remove("alice", span("alpha ")))
        b.move("b", add("bob", after("beta"), " gamma"))
        _, doc = seeded_backend(b.seed_spec())
        assert sorted(doc.registry) == sorted(b.doc.registry)
        assert doc.display_text() == b.doc.display_text()

    def test_seeded_comment_quote_is_derived_from_a_locator(self):
        b = SeedBuilder(base_text="alpha beta\n", document_id="d-1")
        b.move("a", remove("alice", span("alpha ")))
        b.comment("look here", quote=span("beta"), author="carol")
        backend, doc = seeded_backend(b.seed_spec())
        (thread,) = backend.comments[doc.document_id]
        assert thread["plainTextQuote"] == "beta"


class TestPredicates:
    def _doc_with_moves(self, base, moves):
        b = SeedBuilder(base_text=base, document_id="d")
        for i, move in enumerate(moves):
            b.move(f"m{i}", move)
        _, doc = seeded_backend(b.seed_spec())
        return doc

    def test_kind_is_reads_the_card_not_the_ops(self):
        """A merged insert+delete presents as one Replace card even though it
        was authored as two separate edits (§8 / L8)."""
        doc = self._doc_with_moves(
            "keep guidance here\n",
            [
                add("carol", before("guidance"), "stale "),
                remove("carol", span("guidance")),
            ],
        )
        (sid,) = doc.registry
        assert kind_is("Replace")(doc, sid)
        assert not kind_is("Delete")(doc, sid)

    def test_deletes_part_of_ignores_adjacency(self):
        doc = self._doc_with_moves(
            "The legacy exporter still runs.\n",
            [
                remove("alice", span("legacy ")),
                remove("carol", span(" still")),
                add("bob", after("runs"), " on the legacy host"),
            ],
        )
        matched = select(doc, deletes_part_of("legacy"))
        assert len(matched) == 1
        assert doc.registry[matched[0]].author == "alice"

    def test_is_noop_only_matches_identical_sides(self):
        doc = self._doc_with_moves(
            "send the summary today\n",
            [
                rewrite("alice", span("summary"), "summary"),
                rewrite("bob", span("today"), "today,"),
            ],
        )
        noops = select(doc, is_noop())
        assert len(noops) == 1
        assert (
            doc.label(noops[0])["struck"] == doc.label(noops[0])["added"] == "summary"
        )

    def test_adds_text_and_author_and_paragraph_break(self):
        doc = self._doc_with_moves(
            "one\ntwo\n",
            [
                add("alice", after("one"), " and a half"),
                remove("bob", span("\ntwo")),
            ],
        )
        assert len(select(doc, adds_text("half"))) == 1
        assert len(select(doc, by_author("bob"))) == 1
        assert len(select(doc, spans_paragraph_break())) == 1

    def test_combinators(self):
        doc = self._doc_with_moves(
            "one two\n", [add("alice", after("one"), " x"), remove("bob", span(" two"))]
        )
        assert len(select(doc, p_and(always(), by_author("alice")))) == 1
        assert len(select(doc, p_or(by_author("alice"), by_author("bob")))) == 2
        assert len(select(doc, p_not(by_author("alice")))) == 1


class TestDecide:
    def test_first_matching_rule_wins_and_none_leaves_it_pending(self):
        b = SeedBuilder(base_text="one two three\n", document_id="d")
        b.move("a", add("alice", after("one"), " x"))
        b.move("b", remove("bob", span(" two")))
        b.move("c", remove("carol", span(" three")))
        _, doc = seeded_backend(b.seed_spec())
        decisions = decide(
            doc,
            [
                Rule(by_author("carol"), "none"),
                Rule(kind_is("Delete"), "accept"),
                Rule(always(), "reject"),
            ],
        )
        assert decisions[b.named["a"]] == "reject"
        assert decisions[b.named["b"]] == "accept"
        assert b.named["c"] not in decisions

    def test_ordering_is_by_document_position(self):
        b = SeedBuilder(base_text="one two three\n", document_id="d")
        b.move("late", remove("bob", span(" three")))
        b.move("early", remove("alice", span("one")))
        _, doc = seeded_backend(b.seed_spec())
        assert ordered_suggestion_ids(doc) == [b.named["early"], b.named["late"]]
