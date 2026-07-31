"""Golden tests for the pure suggestion-analysis core (Document JSON in,
review-ready structures out). No network, no mocks: fixtures only.

Index discipline: expected start/end indexes are UTF-16 code units taken
straight from the fixture payloads (SUGGESTIONS_INLINE view). The analysis
code must pass API indexes through untouched and must never recompute them
with Python ``len()`` (code points != UTF-16 units for astral chars).
"""

import pytest

from tests.gdocs_preview import fixtures as fx
from gdocs_preview.analysis import (
    AMBIGUOUS_ANCHOR,
    ANCHOR_NOT_FOUND,
    CONTEXT_WINDOW,
    BaseText,
    check_resolution,
    extract_suggestions,
    extract_suggestions_from_tabs,
    render_document,
    render_tabs,
    segment_base_texts,
)
from gdocs_preview.preview_read import suggestion_threads_by_id


def suggestions(doc):
    return extract_suggestions(doc)["suggestions"]


def by_id(doc, sid):
    matches = [s for s in suggestions(doc) if s["suggestion_id"] == sid]
    assert len(matches) == 1, f"expected exactly one record for {sid}"
    return matches[0]


class TestPlainInsertion:
    def test_golden(self):
        s = by_id(fx.DOC_PLAIN_INSERTION, "suggest.ins1")
        assert s == {
            "suggestion_id": "suggest.ins1",
            "type": "insertion",
            "pre_text": "",
            "post_text": " brave",
            "context_before": "Hello",
            "context_after": " world.\n",
            "segment": "body",
            "segment_id": None,
            "tab_id": None,
            "in_table": False,
            "start_index": 6,
            "end_index": 12,
            "author": None,
            "author_source": "unavailable",
            "status": None,
            "create_time": None,
            "summary_text": None,
            "replies": [],
        }

    def test_envelope(self):
        result = extract_suggestions(fx.DOC_PLAIN_INSERTION)
        assert result["document_id"] == "doc-fixture-1"
        assert result["title"] == "Fixture Doc"
        assert result["suggestion_count"] == 1


class TestPlainDeletion:
    def test_golden(self):
        s = by_id(fx.DOC_PLAIN_DELETION, "suggest.del1")
        assert s["type"] == "deletion"
        assert s["pre_text"] == " cruel"
        assert s["post_text"] == ""
        assert s["context_before"] == "Hello"
        assert s["context_after"] == " world.\n"
        assert (s["start_index"], s["end_index"]) == (6, 12)


class TestReplacement:
    def test_same_id_insert_plus_delete_merges_into_replacement(self):
        s = by_id(fx.DOC_REPLACEMENT, "suggest.rep1")
        assert s["type"] == "replacement"
        assert s["pre_text"] == "morning"
        assert s["post_text"] == "evening"
        assert (s["start_index"], s["end_index"]) == (6, 20)
        assert s["context_before"] == "Good "
        assert s["context_after"] == "\n"


class TestMultiRun:
    def test_one_id_spanning_runs_with_base_run_in_between(self):
        s = by_id(fx.DOC_MULTI_RUN, "suggest.multi1")
        assert s["type"] == "insertion"
        # Range spans from the first to the last run carrying the ID; the
        # untouched base run between them appears in both pre and post.
        assert s["pre_text"] == "-mid-"
        assert s["post_text"] == "alpha-mid-omega"
        assert (s["start_index"], s["end_index"]) == (7, 22)


class TestTableNested:
    def test_suggestion_inside_table_cell(self):
        s = by_id(fx.DOC_TABLE, "suggest.tab1")
        assert s["type"] == "insertion"
        assert s["in_table"] is True
        assert s["segment"] == "body"
        assert s["pre_text"] == ""
        assert s["post_text"] == "B-extra"
        assert (s["start_index"], s["end_index"]) == (24, 31)
        # Context comes from base text in traversal order.
        assert s["context_before"] == "Intro.\nCell A\nCell "
        assert s["context_after"] == "\n"


class TestEmptyDoc:
    def test_no_suggestions(self):
        result = extract_suggestions(fx.DOC_EMPTY)
        assert result["suggestions"] == []
        assert result["suggestion_count"] == 0


class TestUtf16Indexes:
    def test_emoji_before_suggestion_shifts_utf16_indexes(self):
        s = by_id(fx.DOC_EMOJI, "suggest.emoji1")
        # Two astral emoji + space = 5 UTF-16 units, so the suggestion
        # starts at 6, not the code-point count 4.
        assert s["start_index"] == fx.EMOJI_SUGGESTION_START == 6
        assert s["end_index"] == fx.EMOJI_SUGGESTION_END == 15
        assert s["post_text"] == "\U0001f389 party "
        assert s["context_before"] == "\U0001f600\U0001f600 "
        assert s["context_after"] == "time.\n"


class TestDocBoundaries:
    def test_suggestion_at_document_start_has_empty_before_context(self):
        s = by_id(fx.DOC_AT_START, "suggest.start1")
        assert s["context_before"] == ""
        assert s["post_text"] == "New: "
        assert s["start_index"] == 1

    def test_suggestion_at_document_end(self):
        s = by_id(fx.DOC_AT_END, "suggest.end1")
        assert s["pre_text"] == " not this"
        assert s["post_text"] == ""
        assert s["context_after"] == "\n"


class TestNeighbouringSuggestions:
    def test_context_windows_use_base_text_not_other_insertions(self):
        s_del = by_id(fx.DOC_NEIGHBOURS, "suggest.na")
        # "INSERTED-B " is a pending insertion from another suggestion; it
        # is NOT part of the base text so it must not leak into context.
        assert s_del["context_before"] == "one two "
        assert s_del["context_after"] == " four.\n"
        assert s_del["pre_text"] == "three"

        s_ins = by_id(fx.DOC_NEIGHBOURS, "suggest.nb")
        assert s_ins["context_after"] == "two three four.\n"

    def test_traversal_order(self):
        ids = [s["suggestion_id"] for s in suggestions(fx.DOC_NEIGHBOURS)]
        assert ids == ["suggest.nb", "suggest.na"]


class TestSegments:
    def test_header_suggestion_reports_segment_and_id(self):
        s = by_id(fx.DOC_HEADER, "suggest.hdr1")
        assert s["segment"] == "header"
        assert s["segment_id"] == "kix.h1"
        assert s["post_text"] == " updated"
        assert (s["start_index"], s["end_index"]) == (6, 14)
        assert s["context_before"] == "Header"

    def test_a_suggestion_at_index_zero_is_still_addressable(self):
        """proto3 omits default values, so the API never writes
        ``startIndex: 0`` -- verified against the live API 2026-07-31, where a
        header paragraph came back as ``{"endIndex": 13, "paragraph": ...}``.
        Index 0 is only reachable in a header/footer/footnote, so reading the
        absence as "no index" made exactly those suggestions unwritable:
        null indexes, dropped by every range filter, nothing to hand back to
        suggest_doc_edit."""
        document = fx.build_doc(
            [fx.paragraph(fx.run("Body.\n"))],
            headers={
                "kix.h1": [
                    fx.paragraph(
                        fx.run("DRAFT ", ins=["suggest.hdr0"]), fx.run("header\n")
                    )
                ]
            },
        )
        # The fixture must reproduce prod's omission, or it tests nothing.
        element = document["headers"]["kix.h1"]["content"][0]["paragraph"]["elements"][
            0
        ]
        assert "startIndex" not in element, element

        s = by_id(document, "suggest.hdr0")
        assert (s["start_index"], s["end_index"]) == (0, 6)
        assert s["segment_id"] == "kix.h1"

    def test_a_header_paragraph_at_index_zero_keeps_its_index(self):
        document = fx.build_doc(
            [fx.paragraph(fx.run("Body.\n"))],
            headers={"kix.h1": [fx.paragraph(fx.run("Header.\n"))]},
        )
        (header,) = [
            p
            for p in render_document(document)["paragraphs"]
            if p["segment"] == "header"
        ]
        assert header["start_index"] == 0
        assert header["end_index"] == 8


class TestStyleAndMixed:
    def test_style_only_suggestion(self):
        s = by_id(fx.DOC_STYLE, "suggest.sty1")
        assert s["type"] == "style"
        assert s["pre_text"] == "styled"
        assert s["post_text"] == "styled"
        assert (s["start_index"], s["end_index"]) == (7, 13)

    def test_insertion_plus_style_is_mixed(self):
        s = by_id(fx.DOC_MIXED, "suggest.mix1")
        assert s["type"] == "mixed"
        assert s["pre_text"] == " gamma"
        assert s["post_text"] == "beta gamma"


class TestAuthors:
    def test_thread_join_supplies_author_status_and_summary(self):
        threads = suggestion_threads_by_id(fx.TABS_PAYLOAD)
        result = extract_suggestions(fx.DOC_PLAIN_INSERTION, threads=threads)
        (s,) = result["suggestions"]
        assert s["author"] == {
            "display_name": "Alice Reviewer",
            "me": False,
            "anonymous": None,
            "user": "users/123",
        }
        assert s["author_source"] == "suggestion_thread"
        assert s["status"] == "OPEN"
        assert s["create_time"] == "2026-07-30T10:00:00.000Z"
        assert s["summary_text"] == "Add: “brave”"

    def test_thread_replies_carry_id_and_author(self):
        threads = suggestion_threads_by_id(fx.TABS_PAYLOAD)
        (s,) = extract_suggestions(fx.DOC_PLAIN_INSERTION, threads=threads)[
            "suggestions"
        ]
        (reply,) = s["replies"]
        assert reply["post_id"] == "AAAApost2"
        assert reply["content"] == "looks good"
        assert reply["author"]["display_name"] == "Bob Author"

    def test_author_never_guessed_without_threads(self):
        s = by_id(fx.DOC_PLAIN_INSERTION, "suggest.ins1")
        assert s["author"] is None
        assert s["author_source"] == "unavailable"
        assert s["summary_text"] is None
        assert s["replies"] == []

    def test_unjoined_suggestion_keeps_null_author(self):
        """A suggestion present in the body but absent from the thread list
        must NOT inherit another suggestion's author."""
        threads = suggestion_threads_by_id(fx.TABS_PAYLOAD)
        (s,) = extract_suggestions(fx.DOC_SECOND_TAB, threads=threads)["suggestions"]
        assert s["suggestion_id"] == "suggest.tab2"
        assert s["author"] is None
        assert s["author_source"] == "unavailable"


class TestMultiTab:
    def test_records_are_tagged_with_their_tab(self):
        threads = suggestion_threads_by_id(fx.TABS_PAYLOAD_MULTI)
        result = extract_suggestions_from_tabs(
            [("t.0", fx.DOC_PLAIN_INSERTION), ("t.second", fx.DOC_SECOND_TAB)],
            threads,
        )
        assert result["suggestion_count"] == 2
        assert [(s["suggestion_id"], s["tab_id"]) for s in result["suggestions"]] == [
            ("suggest.ins1", "t.0"),
            ("suggest.tab2", "t.second"),
        ]
        # Indexes stay per-tab: both tabs' bodies start at 1.
        assert result["suggestions"][1]["start_index"] == 9

    def test_render_tabs_marks_each_tab_and_tags_its_paragraphs(self):
        rendered = render_tabs(
            [("t.0", fx.DOC_PLAIN_INSERTION), ("t.second", fx.DOC_SECOND_TAB)]
        )
        assert rendered["body_text"] == (
            "===== tab_id: t.0 =====\n"
            "Hello{+ brave+} world.\n"
            "===== tab_id: t.second =====\n"
            "Tab two {+addition+}.\n"
        )
        assert {p["tab_id"] for p in rendered["paragraphs"]} == {"t.0", "t.second"}
        assert rendered["suggestion_ids"] == ["suggest.ins1", "suggest.tab2"]

    def test_two_tabs_bodies_do_not_fuse_into_a_sentence_neither_contains(self):
        """MEDIUM 6: this string is get_doc_review_view's DEFAULT output.

        Joined with nothing, a tab whose body does not end in a newline runs
        straight into the next tab's first line, producing prose that exists
        in neither tab -- and nothing in the response says where the seam
        is, so an agent reviewing "the introduction" can read across a tab
        boundary and quote indexes numbered from the wrong start.
        """
        first = fx.build_doc([fx.paragraph(fx.run("ends without a newline"))])
        second = fx.build_doc([fx.paragraph(fx.run("starts here.\n"))])

        body = render_tabs([("t.0", first), ("t.second", second)])["body_text"]

        assert "ends without a newlinestarts here." not in body
        assert body.index("t.second") < body.index("starts here.")
        # And the marker carries the id in the form tab_id= takes.
        assert "===== tab_id: t.second =====" in body

    def test_a_single_tab_body_is_unmarked(self):
        """There is no seam, so the marker would be noise on the common
        case -- and every single-tab response would change shape."""
        rendered = render_tabs([("t.0", fx.DOC_PLAIN_INSERTION)])
        assert rendered["body_text"] == "Hello{+ brave+} world.\n"

    def test_single_tab_render_matches_render_document(self):
        merged = render_tabs([(None, fx.DOC_HEADER)])
        assert merged == render_document(fx.DOC_HEADER)


class TestRenderDocument:
    def test_insertion_markers(self):
        r = render_document(fx.DOC_PLAIN_INSERTION)
        assert r["document_id"] == "doc-fixture-1"
        assert r["body_text"] == "Hello{+ brave+} world.\n"
        assert r["suggestion_ids"] == ["suggest.ins1"]

    def test_deletion_markers(self):
        r = render_document(fx.DOC_PLAIN_DELETION)
        assert r["body_text"] == "Hello{- cruel-} world.\n"

    def test_paragraph_map(self):
        r = render_document(fx.DOC_PLAIN_INSERTION)
        paras = r["paragraphs"]
        assert len(paras) == 1
        p = paras[0]
        assert p["segment"] == "body"
        assert p["segment_id"] is None
        assert p["tab_id"] is None
        assert p["start_index"] == 1
        assert p["end_index"] == 20
        assert p["text"] == "Hello{+ brave+} world.\n"
        assert p["named_style"] == "NORMAL_TEXT"
        assert p["is_list_item"] is False
        assert p["in_table"] is False
        assert p["suggestion_ids"] == ["suggest.ins1"]

    def test_table_paragraphs_flagged(self):
        r = render_document(fx.DOC_TABLE)
        cell_paras = [p for p in r["paragraphs"] if p["in_table"]]
        assert len(cell_paras) == 2
        assert cell_paras[1]["text"] == "Cell {+B-extra+}\n"

    def test_header_segment_rendered(self):
        r = render_document(fx.DOC_HEADER)
        assert r["headers"] == {"kix.h1": "Header{+ updated+}\n"}
        header_paras = [p for p in r["paragraphs"] if p["segment"] == "header"]
        assert len(header_paras) == 1
        assert header_paras[0]["segment_id"] == "kix.h1"

    def test_empty_doc(self):
        r = render_document(fx.DOC_EMPTY)
        assert r["body_text"] == "\n"
        assert r["suggestion_ids"] == []


class TestSegmentBaseTexts:
    """The projection a resolution is verified against: base text, per space."""

    def test_base_text_strips_insertions_and_keeps_deletions(self):
        texts = segment_base_texts(fx.DOC_REPLACEMENT)
        assert texts[(None, None)] == "Good morning\n"
        # ...and it is base text, so it carries none of the markers the
        # reviewer view puts around the same runs.
        assert "{" not in texts[(None, None)]
        assert render_document(fx.DOC_REPLACEMENT)["body_text"] == (
            "Good {-morning-}{+evening+}\n"
        )

    def test_every_segment_is_keyed_by_its_own_coordinate_space(self):
        texts = segment_base_texts(fx.DOC_HEADER, tab_id="t.0")
        assert texts[("t.0", None)] == "Body text.\n"
        assert texts[("t.0", "kix.h1")] == "Header\n"

    def test_the_values_are_the_only_minted_BaseText(self):
        (value,) = set(segment_base_texts(fx.DOC_EMPTY).values())
        assert isinstance(value, BaseText)
        with pytest.raises(TypeError, match="minted only by"):
            BaseText("anything")


class TestCheckResolution:
    """Positional, one representation, and never fail-open."""

    HEAD = "Head "
    TAIL = " tail.\n"

    def _base(self, middle: str) -> BaseText:
        document = fx.build_doc([fx.paragraph(fx.run(self.HEAD + middle + self.TAIL))])
        return segment_base_texts(document)[(None, None)]

    def _check(self, middle: str, expected: str, removed: str):
        return check_resolution(
            self._base(middle),
            context_before=self.HEAD,
            context_after=self.TAIL,
            expected_text=expected,
            removed_text=removed,
        )

    def test_accepted_deletion_that_landed(self):
        assert self._check("", "", "gone").matches is True

    def test_accepted_deletion_that_did_not_land(self):
        assert self._check("gone", "", "gone").matches is False

    def test_accepted_replacement_that_landed(self):
        assert self._check("new", "new", "old").matches is True

    def test_accepted_replacement_that_did_not_land(self):
        assert self._check("old", "new", "old").matches is False

    def test_a_range_reading_something_else_entirely_is_false(self):
        """Not "cannot tell": the range does not read what was promised."""
        assert self._check("neither", "new", "old").matches is False

    def test_a_style_only_resolution_expects_the_text_unchanged(self):
        """pre_text == post_text, so landing means nothing moved."""
        assert self._check("same", "same", "same").matches is True
        assert self._check("moved", "same", "same").matches is False

    def test_an_empty_expectation_at_the_end_of_a_segment_is_not_vacuous(self):
        """context_after ran out, so the prediction must consume the rest.

        Without that rule ``startswith("")`` is true everywhere and accepting
        a deletion at the end of a segment always "matched".
        """
        base = segment_base_texts(
            fx.build_doc([fx.paragraph(fx.run("Keep this gone"))])
        )[(None, None)]
        landed = check_resolution(
            base,
            context_before="Keep this ",
            context_after="",
            expected_text="",
            removed_text="gone",
        )
        assert landed.matches is False

    def test_a_missing_anchor_is_reported_not_guessed(self):
        check = check_resolution(
            self._base("new"),
            context_before="Somewhere else ",
            context_after=self.TAIL,
            expected_text="new",
            removed_text="old",
        )
        assert check.matches is None
        assert check.reason == ANCHOR_NOT_FOUND
        assert check.window is None

    def test_a_repeating_full_width_anchor_that_disagrees_gets_no_verdict(self):
        anchor = "A" * CONTEXT_WINDOW
        tail = "B" * CONTEXT_WINDOW
        base = segment_base_texts(
            fx.build_doc([fx.paragraph(fx.run(anchor + tail + anchor + "old" + tail))])
        )[(None, None)]
        check = check_resolution(
            base,
            context_before=anchor,
            context_after=tail,
            expected_text="",
            removed_text="old",
        )
        assert check.matches is None
        assert check.reason == AMBIGUOUS_ANCHOR

    def test_a_repeating_anchor_that_agrees_still_answers(self):
        anchor = "A" * CONTEXT_WINDOW
        tail = "B" * CONTEXT_WINDOW
        base = segment_base_texts(
            fx.build_doc(
                [fx.paragraph(fx.run(anchor + "old" + tail + anchor + "old" + tail))]
            )
        )[(None, None)]
        check = check_resolution(
            base,
            context_before=anchor,
            context_after=tail,
            expected_text="",
            removed_text="old",
        )
        assert check.matches is False

    def test_marked_text_is_a_TypeError(self):
        with pytest.raises(TypeError, match="segment_base_texts"):
            check_resolution(
                "Head {-old-} tail.\n",
                context_before=self.HEAD,
                context_after=self.TAIL,
                expected_text="",
                removed_text="old",
            )
