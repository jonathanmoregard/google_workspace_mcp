"""Golden tests for the pure suggestion-analysis core (Document JSON in,
review-ready structures out). No network, no mocks: fixtures only.

Index discipline: expected start/end indexes are UTF-16 code units taken
straight from the fixture payloads (SUGGESTIONS_INLINE view). The analysis
code must pass API indexes through untouched and must never recompute them
with Python ``len()`` (code points != UTF-16 units for astral chars).
"""

from tests.gdocs_preview import fixtures as fx
from gdocs_preview.analysis import extract_suggestions, render_document


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
            "in_table": False,
            "start_index": 6,
            "end_index": 12,
            "author": None,
            "author_source": "unavailable",
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
    def test_author_from_suggestion_thread_when_present(self):
        s = by_id(fx.DOC_INSERTION_WITH_THREADS, "suggest.ins1")
        assert s["author"] == {
            "display_name": "Alice Reviewer",
            "me": False,
            "anonymous": None,
            "user": "users/123",
        }
        assert s["author_source"] == "suggestion_thread"

    def test_author_never_guessed_without_threads(self):
        s = by_id(fx.DOC_PLAIN_INSERTION, "suggest.ins1")
        assert s["author"] is None
        assert s["author_source"] == "unavailable"


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
