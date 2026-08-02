"""Field selection, filtering and pagination arithmetic.

No service objects and no JSON round-trip: :mod:`gdocs_preview.review_page`
is pure, so its boundary cases (an empty page, a token replayed under
different filters, a filter that interacts with pagination, an anchor that
was resolved between pages) are testable directly. The tool-level wiring is
covered in test_curated_tools.py.
"""

from __future__ import annotations

import json

import pytest

from gdocs_preview import review_page as rp


def record(
    sid: str,
    *,
    author: str = "dana",
    start: int = 0,
    end: int = 10,
    status: str = "OPEN",
    kind: str = "insertion",
    segment: str = "body",
    segment_id: str | None = None,
    tab_id: str | None = "t.0",
) -> dict:
    """One analysis record with every field the full mode reports."""
    return {
        "suggestion_id": sid,
        "type": kind,
        "pre_text": f"pre-{sid}",
        "post_text": f"post-{sid}",
        "context_before": "before",
        "context_after": "after",
        "segment": segment,
        "segment_id": segment_id,
        "tab_id": tab_id,
        "in_table": False,
        "start_index": start,
        "end_index": end,
        "author": {
            "display_name": author,
            "me": False,
            "anonymous": None,
            "user": "users/1",
        },
        "author_source": "suggestion_thread",
        "status": status,
        "create_time": "2026-07-30T10:00:00.000Z",
        "summary_text": f"Add: “{sid}”",
        "replies": [],
    }


def analysis(records: list[dict], document_id: str = "doc-1") -> dict:
    return {
        "document_id": document_id,
        "title": "T",
        "suggestion_count": len(records),
        "suggestions": records,
    }


def tabs_of(items: list[dict]) -> list[str]:
    return sorted({i.get("tab_id") for i in items if i.get("tab_id")})


def listing(records: list[dict], **kwargs) -> dict:
    """``build_listing`` with the document's tab inventory defaulted.

    Production reads the inventory off ``read.tab_metadata``; here the
    records' own tabs stand in for it, which is right for every test that is
    not ABOUT the inventory. The tests that are (a document with more tabs
    than its cards occupy) pass ``tab_ids`` explicitly -- that divergence is
    the whole point of :class:`TestTabInventoryIsTheDocumentsNotTheRecords`.
    """
    kwargs.setdefault("fields", rp.FIELDS_SUMMARY)
    kwargs.setdefault("tab_ids", tabs_of(records))
    return rp.build_listing(
        analysis(records),
        document_id="doc-1",
        read_source="preview_threads",
        ga_source="ga_documents_get",
        **kwargs,
    )


def review_view(rendered: dict, **kwargs) -> dict:
    """``build_review_view`` with the tab inventory defaulted, as above."""
    kwargs.setdefault("tab_ids", tabs_of(rendered.get("paragraphs") or []))
    return rp.build_review_view(rendered, **kwargs)


def ladder(n: int, **kwargs) -> list[dict]:
    """``n`` records at non-overlapping, ascending index ranges."""
    return [record(f"s.{i}", start=i * 10, end=i * 10 + 5, **kwargs) for i in range(n)]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class TestProjection:
    def test_summary_keeps_exactly_the_declared_fields(self):
        projected = rp.project(record("s.1"), rp.FIELDS_SUMMARY)
        assert set(projected) == set(rp.SUMMARY_FIELDS)

    def test_summary_and_omitted_partition_the_full_record(self):
        """Nothing may fall between the two lists: a field that is neither
        kept nor declared-omitted is a silent drop."""
        full = set(record("s.1"))
        assert set(rp.SUMMARY_FIELDS) | set(rp.SUMMARY_OMITTED_FIELDS) == full
        assert not set(rp.SUMMARY_FIELDS) & set(rp.SUMMARY_OMITTED_FIELDS)

    def test_summary_flattens_the_author_to_a_display_name(self):
        assert rp.project(record("s.1"), rp.FIELDS_SUMMARY)["author"] == "dana"

    def test_summary_author_is_none_when_the_thread_was_unavailable(self):
        anonymous = record("s.1")
        anonymous["author"] = None
        assert rp.project(anonymous, rp.FIELDS_SUMMARY)["author"] is None

    def test_full_is_the_record_untouched(self):
        original = record("s.1")
        assert rp.project(original, rp.FIELDS_FULL) is original

    def test_summary_is_measurably_smaller(self):
        records = ladder(30)
        big = json.dumps(listing(records, fields="full", page_size=30))
        small = json.dumps(listing(records, fields="summary", page_size=30))
        assert len(small) < len(big) * 0.5

    def test_unknown_field_mode_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid fields"):
            listing(ladder(1), fields="everything")


class TestSummaryIsAddressable:
    """A summary card must be enough to write back to the RIGHT place.

    Docs indexes are unique only within a ``(tabId, segmentId)`` pair, and
    ``suggest_doc_edit`` defaults to the body of the default tab. A card that
    carried a bare ``start_index`` would therefore let an agent aim a
    footnote's index at the body -- silently, since nothing in the response
    would say the card was not in the body.
    """

    def test_summary_carries_the_disambiguators(self):
        projected = rp.project(record("s.1"), rp.FIELDS_SUMMARY)
        assert {"segment", "segment_id", "tab_id"} <= set(projected)

    def test_a_footnote_card_is_distinguishable_from_a_body_card(self):
        records = [
            record("s.body", start=6, end=10),
            record(
                "s.note",
                start=6,
                end=10,
                segment="footnote",
                segment_id="kix.fn1",
                tab_id="t.0",
            ),
        ]
        cards = {s["suggestion_id"]: s for s in listing(records)["suggestions"]}
        assert cards["s.body"]["segment"] == "body"
        assert cards["s.body"]["segment_id"] is None
        assert cards["s.note"]["segment"] == "footnote"
        assert cards["s.note"]["segment_id"] == "kix.fn1"
        # Identical indexes, different places: without the two fields above
        # the cards are indistinguishable.
        assert cards["s.body"]["start_index"] == cards["s.note"]["start_index"]

    def test_a_second_tabs_card_names_its_tab(self):
        records = [
            record("s.one", tab_id="t.0"),
            record("s.two", tab_id="t.second"),
        ]
        cards = {s["suggestion_id"]: s for s in listing(records)["suggestions"]}
        assert cards["s.one"]["tab_id"] == "t.0"
        assert cards["s.two"]["tab_id"] == "t.second"

    def test_the_write_tools_arguments_are_all_present(self):
        """The exact keyword arguments ``suggest_doc_edit`` takes to target a
        range: nothing about the write is a guess from a summary card."""
        card = rp.project(
            record("s.1", segment="header", segment_id="kix.h1", tab_id="t.0"),
            rp.FIELDS_SUMMARY,
        )
        assert {
            "start_index": card["start_index"],
            "end_index": card["end_index"],
            "segment_id": card["segment_id"],
            "tab_id": card["tab_id"],
        } == {
            "start_index": 0,
            "end_index": 10,
            "segment_id": "kix.h1",
            "tab_id": "t.0",
        }

    def test_the_disambiguators_are_not_listed_as_omitted(self):
        for name in ("segment", "segment_id", "tab_id"):
            assert name not in rp.SUMMARY_OMITTED_FIELDS
        notice = listing(ladder(1))["notice"]
        assert "segment" not in notice


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


class TestCounts:
    def test_totals_are_reported_even_when_nothing_is_narrowed(self):
        result = listing(ladder(3))
        assert result["suggestion_count"] == 3
        assert result["matched_count"] == 3
        assert result["returned_count"] == 3
        assert result["page"]["has_more"] is False
        assert result["page"]["next_page_token"] is None

    def test_suggestion_count_stays_the_document_total_under_filters(self):
        """The pre-existing meaning of suggestion_count must not change:
        a caller reading it still reads 'how many are in the document'."""
        records = ladder(4, author="dana") + ladder(2, author="sam")
        result = listing(records, author="sam")
        assert result["suggestion_count"] == 6
        assert result["matched_count"] == 2
        assert result["returned_count"] == 2

    def test_a_partial_page_says_so_in_words(self):
        result = listing(ladder(10), page_size=4)
        assert result["returned_count"] == 4
        assert result["page"]["has_more"] is True
        assert "PAGE, not the whole set" in result["notice_page"]
        assert "4 of 10" in result["notice_page"]

    def test_the_last_page_carries_no_token_and_no_page_notice(self):
        first = listing(ladder(6), page_size=4)
        second = listing(
            ladder(6), page_size=4, page_token=first["page"]["next_page_token"]
        )
        assert second["returned_count"] == 2
        assert second["page"]["has_more"] is False
        assert second["page"]["next_page_token"] is None
        assert "notice_page" not in second

    def test_empty_document_is_an_empty_page_not_an_error(self):
        result = listing([])
        assert result["suggestion_count"] == 0
        assert result["matched_count"] == 0
        assert result["returned_count"] == 0
        assert result["suggestions"] == []
        assert result["page"]["has_more"] is False


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def test_author_is_exact_and_case_insensitive(self):
        records = [
            record("s.1", author="dana"),
            record("s.2", author="Dana"),
            record("s.3", author="danielle"),
        ]
        result = listing(records, author="DANA")
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.1", "s.2"]

    def test_author_miss_reports_the_authors_that_exist(self):
        result = listing(ladder(2, author="dana"), author="marcus")
        assert result["matched_count"] == 0
        assert result["filters"]["authors_present"] == ["dana"]

    def test_status_filter(self):
        records = [
            record("s.1", status="OPEN"),
            record("s.2", status="RESOLVED"),
        ]
        assert [
            s["suggestion_id"] for s in listing(records, status="open")["suggestions"]
        ] == ["s.1"]

    def test_status_miss_reports_the_statuses_that_exist(self):
        result = listing(ladder(2), status="RESOLVED")
        assert result["filters"]["statuses_present"] == ["OPEN"]

    def test_index_range_selects_overlapping_records(self):
        records = [
            record("s.1", start=0, end=10),
            record("s.2", start=20, end=30),
            record("s.3", start=40, end=50),
        ]
        result = listing(records, start_index=15, end_index=35)
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.2"]

    def test_the_range_is_half_open_so_seams_do_not_double_count(self):
        """A card ending exactly where the window starts is NOT in it: Docs
        endIndex is exclusive, and the numbers the caller filters with come
        straight off that convention."""
        records = [
            record("s.left", start=0, end=20),
            record("s.inside", start=20, end=40),
            record("s.right", start=40, end=60),
        ]
        result = listing(records, start_index=20, end_index=40)
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.inside"]

    def test_a_card_straddling_the_boundary_is_included(self):
        records = [record("s.straddle", start=10, end=30)]
        assert listing(records, start_index=20, end_index=40)["matched_count"] == 1

    def test_a_single_open_bound_works(self):
        records = ladder(5)  # ranges [0,5) [10,15) [20,25) [30,35) [40,45)
        assert listing(records, start_index=25)["matched_count"] == 2
        assert listing(records, end_index=25)["matched_count"] == 3

    def test_a_backwards_range_is_refused(self):
        with pytest.raises(ValueError, match="half-open"):
            listing(ladder(3), start_index=40, end_index=20)

    def test_an_empty_range_is_refused(self):
        with pytest.raises(ValueError, match="end_index=start_index\\+1"):
            listing(ladder(3), start_index=20, end_index=20)

    def test_records_without_indexes_are_excluded_and_counted(self):
        broken = record("s.x")
        broken["start_index"] = None
        broken["end_index"] = None
        result = listing([record("s.1", start=0, end=10), broken], start_index=0)
        assert result["matched_count"] == 1
        assert result["filters"]["excluded_without_indexes"] == 1

    def test_filters_compose(self):
        records = [
            record("s.1", author="dana", start=0, end=10),
            record("s.2", author="dana", start=100, end=110),
            record("s.3", author="sam", start=0, end=10),
        ]
        result = listing(records, author="dana", start_index=0, end_index=50)
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.1"]

    def test_applied_filters_are_echoed(self):
        result = listing(ladder(3), author="dana", start_index=0, end_index=100)
        assert result["filters"]["author"] == "dana"
        assert result["filters"]["start_index"] == 0
        assert result["filters"]["end_index"] == 100
        assert "half-open" in result["filters"]["range_match"]

    def test_no_filters_means_an_empty_filter_block(self):
        assert listing(ladder(3))["filters"] == {}

    @pytest.mark.parametrize("parameter", ["author", "status", "segment_id", "tab_id"])
    def test_a_blank_filter_is_refused_rather_than_dropped(self, parameter):
        """Fail-open is the one failure mode a review filter cannot have:
        a dropped filter answers with every suggestion in the document, and
        the caller then resolves cards it never asked to see."""
        with pytest.raises(ValueError, match="is blank"):
            listing(ladder(3), **{parameter: "   "})

    def test_an_omitted_filter_still_means_everything(self):
        assert listing(ladder(3), author=None)["matched_count"] == 3


class TestRangeScope:
    """An index range names one ``(tab, segment)``, never raw numbers.

    Docs numbers the body, each header/footer/footnote and each tab from its
    own start, so comparing indexes across them makes an index-range filter
    match cards that are nowhere near the section under review -- and
    ``matched_count`` then reports a number that is simply wrong.
    """

    def body_and_header(self) -> list[dict]:
        return [
            record("s.body", start=100, end=140),
            record("s.far", start=900, end=940),
            record(
                "s.header",
                start=100,
                end=140,
                segment="header",
                segment_id="kix.h1",
            ),
            record(
                "s.note",
                start=110,
                end=120,
                segment="footnote",
                segment_id="kix.fn1",
            ),
        ]

    def test_a_range_means_the_body_unless_a_segment_is_named(self):
        result = listing(self.body_and_header(), start_index=90, end_index=200)
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.body"]
        assert result["matched_count"] == 1
        assert result["filters"]["excluded_other_segments"] == 2
        assert result["filters"]["range_scope"]["segment"] == "body"
        assert result["filters"]["range_scope"]["segment_id"] is None

    def test_naming_a_segment_reads_the_range_there(self):
        result = listing(
            self.body_and_header(),
            start_index=90,
            end_index=200,
            segment_id="kix.h1",
        )
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.header"]
        assert result["filters"]["range_scope"]["segment"] == "header"

    def test_a_segment_filter_works_without_a_range(self):
        result = listing(self.body_and_header(), segment_id="kix.fn1")
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.note"]
        assert result["filters"]["segment_id"] == "kix.fn1"

    def test_a_multi_tab_document_refuses_a_range_without_a_tab(self):
        records = [
            record("s.one", tab_id="t.0", start=100, end=140),
            record("s.two", tab_id="t.second", start=100, end=140),
        ]
        with pytest.raises(ValueError, match="needs a tab_id"):
            listing(records, start_index=90, end_index=200)

    def test_naming_the_tab_makes_the_range_answerable(self):
        records = [
            record("s.one", tab_id="t.0", start=100, end=140),
            record("s.two", tab_id="t.second", start=100, end=140),
        ]
        result = listing(records, start_index=90, end_index=200, tab_id="t.second")
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["s.two"]
        assert result["filters"]["range_scope"]["tab_id"] == "t.second"

    def test_a_single_tab_document_does_not_have_to_name_its_tab(self):
        result = listing(ladder(5), start_index=0, end_index=25)
        assert result["matched_count"] == 3
        assert result["filters"]["range_scope"]["tab_id"] == "t.0"

    def test_a_range_that_matches_nothing_lists_the_segments_that_exist(self):
        result = listing(self.body_and_header(), start_index=5000, end_index=6000)
        assert result["matched_count"] == 0
        present = result["filters"]["segments_present"]
        assert {"segment": "body", "segment_id": None, "tab_id": "t.0"} in present
        assert {
            "segment": "header",
            "segment_id": "kix.h1",
            "tab_id": "t.0",
        } in present

    def test_each_segment_present_names_its_tab(self):
        """LOW 7: a segment id is only half an address. Listed beside an
        unrelated ``tabs_present``, it left the caller to guess the
        cross-product -- and 'kix.h1' may exist in one tab and not another.

        Each entry is exactly the shape ``range_scope`` reports and the
        filters accept, so correcting the call is a copy, not a rebuild.
        """
        records = [
            record("s.a", tab_id="t.0", segment="header", segment_id="kix.h1"),
            record("s.b", tab_id="t.second"),
        ]
        result = listing(records, segment_id="kix.nope")
        assert result["matched_count"] == 0
        assert result["filters"]["segments_present"] == [
            {"segment": "header", "segment_id": "kix.h1", "tab_id": "t.0"},
            {"segment": "body", "segment_id": None, "tab_id": "t.second"},
        ]
        # And every entry is a scope the filters take verbatim.
        for scope in result["filters"]["segments_present"]:
            assert set(scope) == set(rp.SCOPE_FIELDS)
            filters = {k: v for k, v in scope.items() if k != "segment"}
            assert listing(records, **filters)["matched_count"] == 1

    def test_a_mistyped_tab_id_lists_the_tabs_that_exist(self):
        """author/status/segment all answer an empty match with the values
        that ARE present; the tab used to answer with nothing, and it is the
        one half of the address a caller cannot look up any other way."""
        records = [
            record("s.one", tab_id="t.0", start=100, end=140),
            record("s.two", tab_id="t.second", start=100, end=140),
        ]
        result = listing(records, tab_id="t.typo")
        assert result["matched_count"] == 0
        assert result["filters"]["tabs_present"] == ["t.0", "t.second"]

    def test_a_range_that_matches_nothing_also_lists_the_tabs(self):
        result = listing(self.body_and_header(), start_index=5000, end_index=6000)
        assert result["filters"]["tabs_present"] == ["t.0"]

    def test_a_token_scoped_to_one_segment_is_refused_under_another(self):
        records = [
            record(
                f"s.h{i}",
                start=i * 10,
                end=i * 10 + 5,
                segment="header",
                segment_id="kix.h1",
            )
            for i in range(6)
        ] + [record(f"s.b{i}", start=i * 10, end=i * 10 + 5) for i in range(6)]
        first = listing(records, segment_id="kix.h1", page_size=4)
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(records, page_size=4, page_token=first["page"]["next_page_token"])


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_default_page_size_depends_on_the_field_mode(self):
        assert (
            listing(ladder(3), fields="summary")["page"]["page_size"]
            == (rp.DEFAULT_PAGE_SIZE["summary"])
        )
        assert (
            listing(ladder(3), fields="full")["page"]["page_size"]
            == (rp.DEFAULT_PAGE_SIZE["full"])
        )

    def test_pages_partition_the_matched_set_exactly_once(self):
        records = ladder(25)
        seen: list[str] = []
        token = None
        for _ in range(10):
            page = listing(records, page_size=7, page_token=token)
            seen.extend(s["suggestion_id"] for s in page["suggestions"])
            token = page["page"]["next_page_token"]
            if token is None:
                break
        assert seen == [r["suggestion_id"] for r in records]

    def test_pagination_applies_after_filtering(self):
        records = ladder(6, author="dana") + ladder(6, author="sam")
        page = listing(records, author="sam", page_size=4)
        assert page["matched_count"] == 6
        assert page["returned_count"] == 4
        assert {s["author"] for s in page["suggestions"]} == {"sam"}
        second = listing(
            records,
            author="sam",
            page_size=4,
            page_token=page["page"]["next_page_token"],
        )
        assert second["returned_count"] == 2
        assert {s["author"] for s in second["suggestions"]} == {"sam"}

    def test_the_page_ceiling_is_per_field_mode(self):
        """One ceiling for both modes is wrong for one of them: 500 `full`
        records is ~390 KB, ~7x the size at which the observed client stopped
        delivering tool output."""
        assert rp.MAX_PAGE_SIZE["full"] < rp.MAX_PAGE_SIZE["summary"]
        for mode in rp.LIST_FIELD_MODES:
            ceiling = rp.MAX_PAGE_SIZE[mode]
            assert ceiling * rp.CHARS_PER_RECORD[mode] <= rp.SPILL_THRESHOLD_CHARS
            assert rp.DEFAULT_PAGE_SIZE[mode] <= ceiling
            page = listing(ladder(3), fields=mode, page_size=10_000)["page"]
            assert page["page_size"] == ceiling

    def test_a_clamped_page_size_says_so(self):
        """A silent min() reads exactly like a document that ran out of
        suggestions, and the difference decides whether the agent paginates."""
        page = listing(ladder(3), fields="full", page_size=500)["page"]
        assert page["page_size"] == rp.MAX_PAGE_SIZE["full"]
        assert page["page_size_requested"] == 500
        assert "was reduced to" in page["page_size_note"]
        assert "next_page_token" in page["page_size_note"]

    def test_a_page_size_within_the_ceiling_carries_no_note(self):
        page = listing(ladder(3), fields="full", page_size=2)["page"]
        assert page["page_size"] == 2
        assert "page_size_note" not in page
        assert "page_size_requested" not in page

    def test_page_size_zero_is_refused(self):
        with pytest.raises(ValueError, match="at least 1"):
            listing(ladder(3), page_size=0)

    def test_page_size_must_be_an_integer(self):
        with pytest.raises(ValueError, match="must be an integer"):
            listing(ladder(3), page_size="lots")

    def test_a_token_past_the_end_yields_an_empty_page_not_an_error(self):
        records = ladder(4)
        token = rp.encode_page_token(
            document_id="doc-1",
            fields=rp.FIELDS_SUMMARY,
            applied={},
            ordinal=99,
            anchor=None,
        )
        result = listing(records, page_token=token)
        assert result["returned_count"] == 0
        assert result["suggestions"] == []
        assert result["page"]["has_more"] is False


# ---------------------------------------------------------------------------
# Page-token validity
# ---------------------------------------------------------------------------


class TestPageToken:
    def test_garbage_is_refused_with_an_actionable_message(self):
        with pytest.raises(rp.PageTokenError, match="never"):
            listing(ladder(3), page_token="not-a-token")

    def test_empty_token_is_refused(self):
        with pytest.raises(rp.PageTokenError, match="empty"):
            listing(ladder(3), page_token="   ")

    def test_a_token_from_another_document_is_refused(self):
        token = rp.encode_page_token(
            document_id="other-doc",
            fields=rp.FIELDS_SUMMARY,
            applied={},
            ordinal=2,
            anchor="s.1",
        )
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(ladder(4), page_token=token)

    def test_a_token_from_another_field_mode_is_refused(self):
        first = listing(ladder(10), fields="full", page_size=4)
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(
                ladder(10),
                fields="summary",
                page_size=4,
                page_token=first["page"]["next_page_token"],
            )

    def test_a_token_from_another_filter_set_is_refused(self):
        records = ladder(6, author="dana") + ladder(6, author="sam")
        first = listing(records, author="dana", page_size=4)
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(
                records,
                author="sam",
                page_size=4,
                page_token=first["page"]["next_page_token"],
            )

    def test_the_same_filters_replay_fine(self):
        records = ladder(6, author="dana")
        first = listing(records, author="dana", page_size=4)
        second = listing(
            records,
            author="dana",
            page_size=4,
            page_token=first["page"]["next_page_token"],
        )
        assert second["returned_count"] == 2

    def test_wrong_version_is_refused(self):
        import base64

        raw = json.dumps({"v": 99, "k": "x", "i": 0, "a": None}).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with pytest.raises(rp.PageTokenError, match="version"):
            listing(ladder(3), page_token=token)


class TestPageTokenCannotRescope:
    """A token must never come back scoped to a different (tab, segment).

    ``tab_id`` is omitted from ``filters`` when the caller did not pass one,
    so the requested query alone cannot tell "tab A, resolved implicitly"
    from "tab B, resolved implicitly". The fingerprint therefore carries the
    RESOLVED ``range_scope``. Without it the sequence below replays cleanly,
    the anchor is gone, the ordinal fallback fires, and the caller silently
    gets tab B's cards for a sweep it started in tab A -- a skipped
    suggestion, which is the worst outcome this product has.
    """

    @staticmethod
    def _in_tab(tab: str, n: int, prefix: str) -> list[dict]:
        return [
            record(f"{prefix}{i}", tab_id=tab, start=i * 10, end=i * 10 + 5)
            for i in range(n)
        ]

    def test_an_implicitly_resolved_tab_is_part_of_the_token(self):
        only_a = self._in_tab("t.a", 6, "a.")
        first = listing(only_a, start_index=0, end_index=1000, page_size=3)
        assert first["filters"]["range_scope"]["tab_id"] == "t.a"
        # tab_id was never REQUESTED, so it is not in the filter echo -- the
        # thing the old fingerprint was built from.
        assert "tab_id" not in first["filters"]

        # The agent resolves tab A's cards; tab B's are now the only ones,
        # and the identical call resolves to tab B.
        only_b = self._in_tab("t.b", 6, "b.")
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(
                only_b,
                start_index=0,
                end_index=1000,
                page_size=3,
                page_token=first["page"]["next_page_token"],
            )

    def test_the_refusal_says_how_to_make_the_query_stable(self):
        first = listing(
            self._in_tab("t.a", 6, "a."), start_index=0, end_index=1000, page_size=3
        )
        with pytest.raises(rp.PageTokenError, match="Pass tab_id explicitly"):
            listing(
                self._in_tab("t.b", 6, "b."),
                start_index=0,
                end_index=1000,
                page_size=3,
                page_token=first["page"]["next_page_token"],
            )

    def test_naming_the_tab_makes_the_token_replay(self):
        """The stable form of the same sweep: the scope is pinned by the
        caller, so it cannot be re-resolved underneath the token."""
        records = self._in_tab("t.a", 6, "a.")
        first = listing(
            records, start_index=0, end_index=1000, tab_id="t.a", page_size=3
        )
        second = listing(
            records,
            start_index=0,
            end_index=1000,
            tab_id="t.a",
            page_size=3,
            page_token=first["page"]["next_page_token"],
        )
        assert [s["suggestion_id"] for s in second["suggestions"]] == [
            "a.3",
            "a.4",
            "a.5",
        ]

    def test_a_segment_rescope_is_refused_too(self):
        """Same failure one level down: the range resolved to the body, then
        to a header, under an unchanged request."""
        body = [record(f"b.{i}", start=i * 10, end=i * 10 + 5) for i in range(6)]
        first = listing(body, start_index=0, end_index=1000, page_size=3)
        assert first["filters"]["range_scope"]["segment"] == "body"
        header = [
            record(
                f"h.{i}",
                start=i * 10,
                end=i * 10 + 5,
                segment="header",
                segment_id="kix.h1",
            )
            for i in range(6)
        ]
        with pytest.raises(rp.PageTokenError, match="different query"):
            listing(
                header,
                start_index=0,
                end_index=1000,
                segment_id="kix.h1",
                page_size=3,
                page_token=first["page"]["next_page_token"],
            )


class TestPageTokenStability:
    """A cursor has to survive the writes the agent makes between pages."""

    def test_resolving_an_earlier_card_does_not_skip_the_next_one(self):
        records = ladder(9)
        first = listing(records, page_size=3)
        assert [s["suggestion_id"] for s in first["suggestions"]] == [
            "s.0",
            "s.1",
            "s.2",
        ]
        # The agent accepts two of page 1, which removes them from the set.
        after = [r for r in records if r["suggestion_id"] not in ("s.0", "s.1")]
        second = listing(
            after, page_size=3, page_token=first["page"]["next_page_token"]
        )
        # An offset cursor would have resumed at position 3 -- i.e. "s.5",
        # silently skipping s.3 and s.4. The anchor finds s.2 where it now is.
        assert [s["suggestion_id"] for s in second["suggestions"]] == [
            "s.3",
            "s.4",
            "s.5",
        ]
        assert second["page"]["anchor_found"] is True

    def test_a_resolved_anchor_falls_back_and_says_so(self):
        records = ladder(9)
        first = listing(records, page_size=3)
        after = [r for r in records if r["suggestion_id"] != "s.2"]
        second = listing(
            after, page_size=3, page_token=first["page"]["next_page_token"]
        )
        assert second["page"]["anchor_found"] is False
        assert "no longer in the document" in second["page"]["anchor_note"]
        assert "skip or repeat" in second["page"]["anchor_note"]


# ---------------------------------------------------------------------------
# get_doc_review_view shaping
# ---------------------------------------------------------------------------


def rendered_document() -> dict:
    return {
        "document_id": "doc-1",
        "title": "T",
        "body_text": "First.\nSecond.\nThird.\n",
        "paragraphs": [
            {
                "segment": "body",
                "segment_id": None,
                "tab_id": "t.0",
                "start_index": 1,
                "end_index": 8,
                "text": "First.\n",
                "named_style": "NORMAL_TEXT",
                "is_list_item": False,
                "in_table": False,
                "suggestion_ids": ["s.1"],
            },
            {
                "segment": "body",
                "segment_id": None,
                "tab_id": "t.0",
                "start_index": 8,
                "end_index": 16,
                "text": "Second.\n",
                "named_style": "NORMAL_TEXT",
                "is_list_item": False,
                "in_table": False,
                "suggestion_ids": [],
            },
            {
                "segment": "body",
                "segment_id": None,
                "tab_id": "t.0",
                "start_index": 16,
                "end_index": 23,
                "text": "Third.\n",
                "named_style": "NORMAL_TEXT",
                "is_list_item": False,
                "in_table": False,
                "suggestion_ids": ["s.2"],
            },
        ],
        "headers": [],
        "footers": [],
        "footnotes": [],
        "suggestion_ids": ["s.1", "s.2"],
    }


class TestReviewView:
    def test_text_mode_drops_the_paragraph_map_and_declares_it(self):
        view = review_view(rendered_document(), fields="text")
        assert view["body_text"] == "First.\nSecond.\nThird.\n"
        assert "paragraphs" not in view
        assert view["omitted_fields"] == ["paragraphs"]
        assert view["paragraph_count"] == 3
        assert view["returned_paragraph_count"] == 3

    def test_paragraph_mode_drops_body_text_and_declares_it(self):
        view = review_view(rendered_document(), fields="paragraphs")
        assert "body_text" not in view
        assert "body_text" in view["omitted_fields"]
        assert len(view["paragraphs"]) == 3

    def test_full_mode_keeps_both_and_declares_nothing(self):
        view = review_view(rendered_document(), fields="full")
        assert view["body_text"] == "First.\nSecond.\nThird.\n"
        assert len(view["paragraphs"]) == 3
        assert "omitted_fields" not in view

    def test_unwindowed_body_text_is_character_identical_to_the_renderer(self):
        source = rendered_document()
        for mode in ("text", "full"):
            assert review_view(source, fields=mode)["body_text"] == source["body_text"]

    def test_window_narrows_paragraphs_body_text_and_suggestion_ids(self):
        view = review_view(
            rendered_document(), fields="full", start_index=8, end_index=16
        )
        assert view["returned_paragraph_count"] == 1
        assert view["body_text"] == "Second.\n"
        assert view["suggestion_ids"] == []
        assert view["window"]["paragraphs_outside_window"] == 2

    def test_a_window_taken_off_the_paragraph_map_selects_that_paragraph(self):
        """The realistic loop: read the map, take a paragraph's own
        start/end, ask for it back. Half-open means it returns exactly one."""
        source = rendered_document()
        for paragraph in source["paragraphs"]:
            view = review_view(
                source,
                fields="paragraphs",
                start_index=paragraph["start_index"],
                end_index=paragraph["end_index"],
            )
            assert [p["text"] for p in view["paragraphs"]] == [paragraph["text"]]

    def test_window_spanning_two_paragraphs_returns_both(self):
        view = review_view(
            rendered_document(), fields="paragraphs", start_index=8, end_index=23
        )
        assert [p["text"] for p in view["paragraphs"]] == ["Second.\n", "Third.\n"]

    def test_a_backwards_window_is_refused(self):
        with pytest.raises(ValueError, match="half-open"):
            review_view(rendered_document(), fields="full", start_index=16, end_index=8)

    def test_window_that_matches_nothing_is_an_empty_map_not_an_error(self):
        view = review_view(
            rendered_document(), fields="full", start_index=900, end_index=999
        )
        assert view["returned_paragraph_count"] == 0
        assert view["paragraphs"] == []
        assert view["body_text"] == ""
        assert view["paragraph_count"] == 3

    def test_unknown_field_mode_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid fields"):
            review_view(rendered_document(), fields="prose")


def rendered_with_header() -> dict:
    """A document whose header paragraph is numbered from 0, like prod's."""
    source = rendered_document()
    source["paragraphs"] = [
        {
            "segment": "header",
            "segment_id": "kix.h1",
            "tab_id": "t.0",
            "start_index": 0,
            "end_index": 12,
            "text": "Header.\n",
            "named_style": "NORMAL_TEXT",
            "is_list_item": False,
            "in_table": False,
            "suggestion_ids": ["s.h"],
        },
        *source["paragraphs"],
    ]
    source["headers"] = [
        {
            "text": "Header.\n",
            "segment": "header",
            "segment_id": "kix.h1",
            "tab_id": "t.0",
            "start_index": 0,
            "end_index": 12,
        }
    ]
    return source


class TestReviewViewWindowScope:
    def test_a_body_window_does_not_pull_in_the_header(self):
        view = review_view(
            rendered_with_header(), fields="full", start_index=1, end_index=8
        )
        assert [p["text"] for p in view["paragraphs"]] == ["First.\n"]
        assert view["window"]["scope"]["segment"] == "body"
        assert view["body_text"] == "First.\n"

    def test_naming_the_segment_reads_the_window_in_the_header(self):
        view = review_view(
            rendered_with_header(),
            fields="full",
            start_index=0,
            end_index=12,
            segment_id="kix.h1",
        )
        assert [p["text"] for p in view["paragraphs"]] == ["Header.\n"]
        assert view["window"]["scope"]["segment_id"] == "kix.h1"
        # The window is in the header, so nothing of the body is claimed.
        assert view["body_text"] == ""

    def test_a_multi_tab_document_refuses_a_window_without_a_tab(self):
        source = rendered_document()
        source["paragraphs"][1]["tab_id"] = "t.second"
        with pytest.raises(ValueError, match="needs a tab_id"):
            review_view(source, fields="full", start_index=1, end_index=30)


class TestReviewViewWindowIsWholeResponse:
    """A window has to narrow every text surface, not just ``body_text``."""

    def test_a_body_window_does_not_carry_the_whole_header(self):
        view = review_view(
            rendered_with_header(), fields="full", start_index=1, end_index=8
        )
        assert view["body_text"] == "First.\n"
        # The header is a different coordinate space and outside this window;
        # returning it whole made the response describe two windows at once.
        assert view["headers"] == []

    def test_a_header_window_returns_the_header_text_it_selected(self):
        view = review_view(
            rendered_with_header(),
            fields="full",
            start_index=0,
            end_index=12,
            segment_id="kix.h1",
        )
        assert view["headers"] == [
            {
                "text": "Header.\n",
                "segment": "header",
                "segment_id": "kix.h1",
                "tab_id": "t.0",
                "start_index": 0,
                "end_index": 12,
            }
        ]
        # body_text is empty because no BODY paragraph is in this window --
        # legible now that headers is populated beside it.
        assert view["body_text"] == ""

    def test_an_unwindowed_read_still_returns_every_segment(self):
        view = review_view(rendered_with_header(), fields="full")
        assert view["headers"] == [
            {
                "text": "Header.\n",
                "segment": "header",
                "segment_id": "kix.h1",
                "tab_id": "t.0",
                "start_index": 0,
                "end_index": 12,
            }
        ]
        assert view["body_text"] == "First.\nSecond.\nThird.\n"


class TestReviewViewScopeArguments:
    def test_a_blank_segment_id_is_refused_not_treated_as_a_segment(self):
        """It used to be passed through as the id '', which no paragraph
        carries, so the window came back empty -- indistinguishable from a
        range that genuinely selected nothing."""
        with pytest.raises(ValueError, match="segment_id"):
            review_view(
                rendered_with_header(),
                fields="full",
                start_index=0,
                end_index=12,
                segment_id="   ",
            )

    def test_a_blank_tab_id_is_refused(self):
        with pytest.raises(ValueError, match="tab_id"):
            review_view(
                rendered_document(),
                fields="full",
                start_index=1,
                end_index=8,
                tab_id=" ",
            )

    def test_a_scope_without_a_window_says_it_did_nothing(self):
        view = review_view(rendered_with_header(), fields="full", segment_id="kix.h1")
        assert "IGNORED" in view["scope_note"]
        assert "kix.h1" in view["scope_note"]
        # And it really did nothing: the whole document is still here.
        assert view["returned_paragraph_count"] == view["paragraph_count"]
        assert view["body_text"] == "First.\nSecond.\nThird.\n"

    def test_a_tab_id_without_a_window_says_it_did_nothing(self):
        view = review_view(rendered_document(), fields="text", tab_id="t.0")
        assert "tab_id='t.0'" in view["scope_note"]

    def test_no_scope_note_when_none_was_passed(self):
        assert "scope_note" not in review_view(rendered_document(), fields="full")
        assert "scope_note" not in review_view(
            rendered_document(), fields="full", start_index=1, end_index=8
        )


# ---------------------------------------------------------------------------
# Pending suggestions the analysis layer does not model
# ---------------------------------------------------------------------------


def thread(
    sid: str, *, status: str | None = "OPEN", summary: str = "Format: alignment"
):
    """One normalized suggestion thread, as preview_read produces them."""
    return {
        "suggestion_id": sid,
        "status": status,
        "summary_text": summary,
        "author": {"display_name": "Alice Reviewer", "me": False, "user": "users/1"},
        "replies": [],
    }


class TestUnreportedSuggestions:
    """The set subtraction that makes an invisible card countable.

    Measured against prod 2026-08-02 (docs/findings/coverage.md): an
    ``updateParagraphStyle`` alignment suggestion leaves NO content mark, so
    ``analysis.extract_suggestions`` returns nothing for it while the API
    files an OPEN thread. ``suggestion_count`` was therefore short by one,
    with nothing in the response saying so.
    """

    def test_a_thread_with_no_record_is_reported(self):
        cards = rp.unreported_suggestions(
            {
                "s.text": thread("s.text", summary="Add: “x”"),
                "s.para": thread("s.para"),
            },
            ["s.text"],
        )
        assert [c["suggestion_id"] for c in cards] == ["s.para"]
        # Google's own label is what names the kind; a bare id would not.
        assert cards[0]["summary_text"] == "Format: alignment"
        assert cards[0]["author"] == "Alice Reviewer"
        assert cards[0]["status"] == "OPEN"

    def test_a_modelled_thread_is_not_reported_twice(self):
        assert rp.unreported_suggestions({"s.a": thread("s.a")}, ["s.a"]) == []

    def test_a_resolved_thread_is_not_reported(self):
        """A rejected card keeps its thread, so the raw subtraction would
        count every suggestion the document has ever had."""
        cards = rp.unreported_suggestions(
            {"s.gone": thread("s.gone", status="REJECTED")}, []
        )
        assert cards == []

    def test_no_threads_reports_nothing(self):
        assert rp.unreported_suggestions({}, ["s.a"]) == []


class TestAttachUnreported:
    def test_a_clean_document_says_zero_explicitly(self):
        result = rp.attach_unreported({}, threads={}, reported_ids=[], complete=True)
        assert result["unreported_suggestion_count"] == 0
        # No noise when there is nothing to say.
        assert "unreported_suggestions" not in result
        assert "notice_unreported" not in result

    def test_an_unmodelled_card_is_counted_listed_and_narrated(self):
        result = rp.attach_unreported(
            {"suggestion_count": 1},
            threads={"s.text": thread("s.text"), "s.para": thread("s.para")},
            reported_ids=["s.text"],
            complete=True,
        )
        assert result["unreported_suggestion_count"] == 1
        assert result["unreported_suggestions"][0]["suggestion_id"] == "s.para"
        notice = result["notice_unreported"]
        # The three things the notice has to carry: that the response's other
        # counts exclude them, what kind they are, and that they remain
        # actionable.
        assert "NOT counted by any other suggestion count in this response" in notice
        assert "Format: alignment" in notice
        assert "manage_document_suggestion" in notice
        # ...and NOT the name of any one tool's count field. Three tools ship
        # this string; ``get_doc_review_view`` and the write tools have no
        # ``suggestion_count``, so naming it pointed two of them at a field
        # their response does not contain.
        assert "`suggestion_count`" not in notice

    def test_a_degraded_read_says_it_cannot_tell_rather_than_zero(self):
        """Zero here would be an absence claim from a read carrying no thread
        array at all -- the same rule author, status and tab_id follow."""
        result = rp.attach_unreported({}, threads={}, reported_ids=[], complete=False)
        assert result["unreported_suggestion_count"] is None
        assert result["unreported_suggestions_unavailable"] == "read_degraded"
        assert "cannot say" in result["notice_unreported"]
        assert "unreported_suggestions" not in result
