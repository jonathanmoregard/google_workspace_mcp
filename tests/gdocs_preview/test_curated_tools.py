"""Behavioral tests for the curated docs_preview review tools.

Mocked Google service objects only -- no network. Covers:
  - list_document_suggestions: preview (tabs + threads) read plumbing,
    author/status/summary passthrough, the GA fallback, and the wiring of
    the fields/filter/pagination shaping (whose arithmetic is tested on its
    own in test_review_page.py)
  - get_doc_review_view: view-mode validation, rendered output, comments,
    field modes and the index window
  - check_docs_review_capabilities: probe error-shape classification +
    process cache semantics (probe=False must never touch the API)
"""

import json
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdocs_preview import curated_tools, preview_read, preview_status, review_page
from tests.gdocs_preview import fixtures as fx

EMAIL = "reviewer@example.com"

#: Three tabs; every pending suggestion sits in the middle one. Counting the
#: records makes this document look single-tab, which is how a wrong-tab
#: answer used to get past the multi-tab refusal (HIGH 1).
THREE_TABS_CARDS_IN_ONE = fx.build_tabs_payload(
    [
        ("t.0", fx.DOC_EMPTY),
        ("t.second", fx.DOC_PLAIN_INSERTION),
        ("t.third", fx.DOC_EMPTY),
    ]
)


def _unwrap(tool):
    """Unwrap the decorated tool function to the original implementation."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _docs_get_service(document):
    """Service whose documents().get() returns ``document`` for any params.

    A ``Mock`` accepts ``commentsViewMode``, so the preview read path is the
    one exercised -- exactly like the mockdocs fake service, and unlike the
    real client (see gdocs_preview/preview_read.py).
    """
    service = Mock()
    service.documents.return_value.get.return_value.execute = Mock(
        return_value=document
    )
    return service


def _ga_only_service(document, failure=None):
    """Service where only the GA read works: the thread-bearing read fails
    the way a non-enrolled caller's does."""
    service = Mock()
    get = service.documents.return_value.get

    def _get(**kwargs):
        if "commentsViewMode" in kwargs:
            raise failure or preview_read.PreviewReadError(
                "preview documents.get returned HTTP 400: not enrolled"
            )
        return Mock(execute=Mock(return_value=document))

    get.side_effect = _get
    return service


def _http_error(status, content: bytes) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "mock"
    return HttpError(resp=resp, content=content)


@pytest.fixture(autouse=True)
def _reset_preview_status():
    preview_status.reset()
    yield
    preview_status.reset()


class TestListSuggestions:
    @pytest.mark.asyncio
    async def test_requests_the_thread_bearing_view(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.list_document_suggestions)

        await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")

        service.documents.return_value.get.assert_called_once_with(
            documentId="doc-fixture-1",
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )

    @pytest.mark.asyncio
    async def test_returns_analysis_json(self):
        service = _docs_get_service(
            fx.build_tabs_payload([("t.0", fx.DOC_REPLACEMENT)])
        )
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )

        assert result["suggestion_count"] == 1
        s = result["suggestions"][0]
        assert s["type"] == "replacement"
        assert s["pre_text"] == "morning"
        assert s["post_text"] == "evening"

    @pytest.mark.asyncio
    async def test_author_status_and_summary_come_from_the_thread(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )

        assert result["read_source"] == curated_tools.READ_SOURCE_PREVIEW
        assert result["tabs"] == [{"tab_id": "t.0", "title": "Tab 1", "index": 0}]
        (s,) = result["suggestions"]
        assert s["suggestion_id"] == "suggest.ins1"
        assert s["author"]["display_name"] == "Alice Reviewer"
        assert s["author"]["user"] == "users/123"
        assert s["author_source"] == "suggestion_thread"
        assert s["status"] == "OPEN"
        assert s["create_time"] == "2026-07-30T10:00:00.000Z"
        assert s["summary_text"] == "Add: “brave”"
        assert s["tab_id"] == "t.0"

    @pytest.mark.asyncio
    async def test_summary_default_keeps_author_status_and_summary_text(self):
        """The M4a fields survive the default projection -- flattened, not
        dropped: author becomes the display name, status and summary_text
        pass through unchanged."""
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        assert result["fields"] == "summary"
        (s,) = result["suggestions"]
        assert s["author"] == "Alice Reviewer"
        assert s["status"] == "OPEN"
        assert s["summary_text"] == "Add: “brave”"
        assert set(s) == set(review_page.SUMMARY_FIELDS)
        assert result["omitted_fields"] == list(review_page.SUMMARY_OMITTED_FIELDS)
        assert "pre_text" not in s

    @pytest.mark.asyncio
    async def test_summary_locates_a_header_card_in_its_own_segment(self):
        """The blocker this field set exists for.

        Docs indexes are local to a ``(tabId, segmentId)`` pair, so a header
        run and a body run can carry the SAME start_index. ``suggest_doc_edit``
        defaults to the body of the default tab, so a summary card without
        ``segment_id`` would let an agent write a header's index into the
        body of a customer document with nothing warning it.
        """
        document = fx.build_doc(
            [
                fx.paragraph(
                    fx.run("Body "),
                    fx.run("edit", ins=["suggest.body1"]),
                    fx.run(" here.\n"),
                )
            ],
            headers={
                "kix.h1": [
                    fx.paragraph(
                        fx.run("Head "),
                        fx.run("edit", ins=["suggest.hdr1"]),
                        fx.run("\n"),
                    )
                ]
            },
        )
        service = _docs_get_service(fx.build_tabs_payload([("t.0", document)]))
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        cards = {s["suggestion_id"]: s for s in result["suggestions"]}
        assert set(cards) == {"suggest.body1", "suggest.hdr1"}
        body, header = cards["suggest.body1"], cards["suggest.hdr1"]
        assert body["segment"] == "body" and body["segment_id"] is None
        assert header["segment"] == "header" and header["segment_id"] == "kix.h1"
        assert header["tab_id"] == body["tab_id"] == "t.0"
        # The ranges overlap numerically: [6,10) in the body, [5,9) in the
        # header. Only the segment tells them apart.
        assert body["start_index"] < header["end_index"]
        assert header["start_index"] < body["end_index"]

        # And an index range covering both numbers means the BODY, unless
        # the caller names the header segment.
        in_body = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=20,
            )
        )
        assert [s["suggestion_id"] for s in in_body["suggestions"]] == ["suggest.body1"]
        assert in_body["filters"]["excluded_other_segments"] == 1
        in_header = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=20,
                segment_id="kix.h1",
            )
        )
        assert [s["suggestion_id"] for s in in_header["suggestions"]] == [
            "suggest.hdr1"
        ]

    @pytest.mark.asyncio
    async def test_multi_tab_records_carry_their_tab_id(self):
        service = _docs_get_service(fx.TABS_PAYLOAD_MULTI)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )

        assert [(s["suggestion_id"], s["tab_id"]) for s in result["suggestions"]] == [
            ("suggest.ins1", "t.0"),
            ("suggest.tab2", "t.second"),
        ]

    @pytest.mark.asyncio
    async def test_a_range_is_refused_on_a_tab_the_cards_do_not_reach(self):
        """HIGH 1: the refusal must count the DOCUMENT's tabs.

        Three tabs, every pending card in the middle one. Counting the
        records saw a single tab, so the refusal did not fire and the
        omitted ``tab_id`` resolved silently to ``t.second`` -- a caller
        meaning the default tab got that tab's cards back as though they
        were at the range it named.
        """
        service = _docs_get_service(THREE_TABS_CARDS_IN_ONE)
        fn = _unwrap(curated_tools.list_document_suggestions)

        with pytest.raises(UserInputError, match="3 tabs"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=100,
            )

    @pytest.mark.asyncio
    async def test_naming_the_tab_answers_the_same_range(self):
        service = _docs_get_service(THREE_TABS_CARDS_IN_ONE)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=100,
                tab_id="t.second",
            )
        )
        assert [s["suggestion_id"] for s in result["suggestions"]] == ["suggest.ins1"]
        assert result["filters"]["range_scope"]["tab_id"] == "t.second"
        # The inventory really is the document's: all three tabs are echoed.
        assert [t["tab_id"] for t in result["tabs"]] == ["t.0", "t.second", "t.third"]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_ga_read_when_not_enrolled(self):
        """Enrollment is a property of the caller's project, not the
        document: a preview-read failure must degrade, never fail."""
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )

        assert result["read_source"] == curated_tools.READ_SOURCE_GA
        assert "not enrolled" in result["degraded_reason"]
        (s,) = result["suggestions"]
        assert s["suggestion_id"] == "suggest.ins1"
        assert s["author"] is None
        assert s["author_source"] == "unavailable"
        # Second call: the GA read, with no preview-only parameters.
        assert service.documents.return_value.get.call_args_list[-1].kwargs == {
            "documentId": "doc-fixture-1",
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
        }

    @pytest.mark.asyncio
    async def test_a_degraded_read_says_the_labels_are_missing_in_both_modes(self):
        """The nulls are a property of the READ, so both field modes have to
        say so. A reviewer who reads a page of nulls in ``full`` concludes
        the suggestions have no author with more confidence, having asked
        for everything."""
        fn = _unwrap(curated_tools.list_document_suggestions)

        for mode in ("summary", "full"):
            service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
            result = json.loads(
                await fn(
                    service,
                    user_google_email=EMAIL,
                    document_id="doc-fixture-1",
                    fields=mode,
                )
            )
            assert result["read_source"] == curated_tools.READ_SOURCE_GA
            assert "null on EVERY record" in result["degraded_notice"], mode
            assert "author" in result["null_fields"], mode
            assert "status" in result["null_fields"], mode

    @pytest.mark.asyncio
    async def test_a_tab_filter_on_a_degraded_read_is_refused(self):
        """``tab_id`` belongs in the same refusal as author/status, and was
        missing from it.

        The GA payload is one UNNAMED body: every record carries
        ``tab_id: None``, so a named tab matches nothing and the answer came
        back success-shaped -- ``matched_count: 0``, ``tabs_present: []`` --
        asserting "no suggestions in that tab" from a read that cannot see
        tabs at all. The id a caller passes here is typically one THIS SERVER
        printed on an earlier, healthy response.
        """
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        with pytest.raises(UserInputError) as excinfo:
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                tab_id="t.0",
            )

        message = str(excinfo.value)
        assert "tab_id cannot be filtered on this read" in message
        assert "single unnamed body" in message
        assert "every suggestion is still listed" in message

    @pytest.mark.asyncio
    async def test_an_author_filter_on_a_degraded_read_is_refused(self):
        """`matched_count: 0, authors_present: []` is a true statement about
        the read and a false one about the document: the reviewer reads it as
        'there are no suggestions by Dana'."""
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        with pytest.raises(UserInputError, match="cannot be filtered"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                author="Dana",
            )

    @pytest.mark.asyncio
    async def test_a_status_filter_on_a_degraded_read_is_refused(self):
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        with pytest.raises(UserInputError, match="cannot be filtered"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                status="OPEN",
            )

    @pytest.mark.asyncio
    async def test_a_degraded_read_still_lists_everything_unfiltered(self):
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )
        assert result["suggestion_count"] == 1
        assert result["matched_count"] == 1

    @pytest.mark.asyncio
    async def test_a_blank_author_is_refused_not_dropped(self):
        """A dropped filter answers with every suggestion in the document --
        a filter that fails open."""
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.list_document_suggestions)

        with pytest.raises(UserInputError, match="is blank"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                author="   ",
            )

    @pytest.mark.asyncio
    async def test_http_error_from_the_preview_read_also_degrades(self):
        service = _ga_only_service(
            fx.DOC_PLAIN_INSERTION,
            failure=_http_error(400, b'{"error": {"message": "unknown name"}}'),
        )
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )
        assert result["read_source"] == curated_tools.READ_SOURCE_GA


class TestReadDocument:
    @pytest.mark.asyncio
    async def test_a_degraded_read_qualifies_its_empty_comments(self):
        """``comments: []`` is an absence claim about a customer's document.

        On the GA fallback it is a fact about the READ -- comment threads
        exist only on the preview payload -- and this tool said nothing:
        ``read_source`` plus a raw exception string in ``degraded_reason``,
        while ``list_document_suggestions`` has built a whole notice for the
        same read since the fallback existed. A reviewer who reads an empty
        ``comments`` as "nobody has commented" stops reviewing.
        """
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        assert result["read_source"] == curated_tools.READ_SOURCE_GA
        assert result["comments"] == []
        assert result["comments_unavailable"] == "read_degraded"
        notice = result["degraded_notice"]
        # The empty comments, and the one-tab prose, both named.
        assert "`comments` is EMPTY because" in notice
        assert "does not mean the document has no" in notice
        assert "every other tab is missing" in notice

    @pytest.mark.asyncio
    async def test_a_window_in_a_named_tab_is_refused_on_a_degraded_read(self):
        """The mirror of the listing's tab_id refusal.

        A window is resolved into ONE (tab, segment). The GA fallback has a
        single unnamed body and no tab ids, so a window named in a tab
        resolved to nothing and came back ``body_text: ""`` -- "that range is
        empty" said about a tab the read never saw.
        """
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.get_doc_review_view)

        with pytest.raises(UserInputError) as excinfo:
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=5,
                tab_id="t.0",
            )

        message = str(excinfo.value)
        assert "cannot be used on this read" in message
        assert "single unnamed body" in message

    @pytest.mark.asyncio
    async def test_a_bare_tab_id_on_a_degraded_read_is_still_only_ignored(self):
        """The control: without a window, tab_id names no coordinate space to
        resolve, so it is reported as ignored rather than refused -- the
        behaviour a healthy read already has."""
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                tab_id="t.0",
            )
        )
        assert "IGNORED" in result["scope_note"]

    @pytest.mark.asyncio
    async def test_a_preview_read_carries_no_degraded_notice(self):
        """The control: the notice is about the read, so a good read must not
        carry it -- an unconditional warning is one an agent learns to skip."""
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        assert result["read_source"] == curated_tools.READ_SOURCE_PREVIEW
        assert "degraded_notice" not in result
        assert "comments_unavailable" not in result

    @pytest.mark.asyncio
    async def test_default_view_mode_and_rendering(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        service.documents.return_value.get.assert_called_once_with(
            documentId="doc-fixture-1",
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )
        assert result["view_mode"] == "SUGGESTIONS_INLINE"
        assert result["fields"] == "text"
        assert result["body_text"] == "Hello{+ brave+} world.\n"
        assert result["read_source"] == curated_tools.READ_SOURCE_PREVIEW
        # The default drops the paragraph map, and says so.
        assert "paragraphs" not in result
        assert "paragraphs" in result["omitted_fields"]

    @pytest.mark.asyncio
    async def test_full_fields_restore_the_original_shape(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )

        assert result["body_text"] == "Hello{+ brave+} world.\n"
        assert [p["text"] for p in result["paragraphs"]] == ["Hello{+ brave+} world.\n"]
        assert result["suggestion_ids"] == ["suggest.ins1"]
        assert "omitted_fields" not in result

    @pytest.mark.asyncio
    async def test_the_window_refuses_the_same_document_the_listing_refuses(self):
        """The third resolver, counting the same inventory as the other two.

        ``build_review_view`` reads the paragraph map, which does happen to
        touch every tab -- so it was accidentally right here while the two
        suggestion-record callers were wrong. Agreement by coincidence is
        what let this class survive three rounds; this pins it to the
        document's ``tab_metadata`` instead.
        """
        service = _docs_get_service(THREE_TABS_CARDS_IN_ONE)
        fn = _unwrap(curated_tools.get_doc_review_view)

        with pytest.raises(UserInputError, match="3 tabs"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                start_index=1,
                end_index=100,
            )

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
                start_index=1,
                end_index=100,
                tab_id="t.second",
            )
        )
        assert result["window"]["scope"]["tab_id"] == "t.second"
        assert result["body_text"] == "Hello{+ brave+} world.\n"

    @pytest.mark.asyncio
    async def test_paragraphs_fields_drop_body_text_without_losing_characters(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="paragraphs",
            )
        )

        assert "body_text" not in result
        assert "body_text" in result["omitted_fields"]
        body = "".join(
            p["text"] for p in result["paragraphs"] if p["segment"] == "body"
        )
        assert body == "Hello{+ brave+} world.\n"

    @pytest.mark.asyncio
    async def test_index_window_narrows_paragraphs_and_body_text(self):
        document = fx.build_doc(
            [
                fx.paragraph(
                    fx.run("First paragraph"),
                    fx.run(" plus", ins=["s.1"]),
                    fx.run(".\n"),
                ),
                fx.paragraph(fx.run("Second paragraph.\n")),
                fx.paragraph(
                    fx.run("Third"), fx.run(" cut", dels=["s.2"]), fx.run(".\n")
                ),
            ]
        )
        service = _docs_get_service(fx.build_tabs_payload([("t.0", document)]))
        fn = _unwrap(curated_tools.get_doc_review_view)

        whole = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
            )
        )
        first = whole["paragraphs"][0]
        windowed = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="full",
                start_index=first["start_index"],
                end_index=first["end_index"],
            )
        )

        assert windowed["paragraph_count"] == len(whole["paragraphs"])
        assert windowed["returned_paragraph_count"] == 1
        assert windowed["body_text"] == first["text"]
        assert windowed["window"]["paragraphs_outside_window"] == (
            len(whole["paragraphs"]) - 1
        )
        assert windowed["suggestion_ids"] == first["suggestion_ids"]

    @pytest.mark.asyncio
    async def test_include_comments_false_reports_what_it_left_out(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                include_comments=False,
            )
        )

        assert result["comments"] == []
        assert result["comments_omitted"] == 1

    @pytest.mark.asyncio
    async def test_invalid_fields_rejected(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        with pytest.raises(UserInputError, match="fields"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                fields="everything",
            )

    @pytest.mark.asyncio
    async def test_comment_threads_carry_ids_and_authors(self):
        service = _docs_get_service(fx.TABS_PAYLOAD)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        (comment,) = result["comments"]
        assert comment["comment_id"] == "AAAAcomment1"
        assert comment["anchor_id"] == "kix.anchor1"
        assert comment["author"]["display_name"] == "Alice Reviewer"
        assert comment["quoted_text"] == "Hello"
        (reply,) = comment["replies"]
        assert reply["post_id"] == "AAAAcomment2"
        assert reply["author"]["display_name"] == "Bob Author"

    @pytest.mark.asyncio
    async def test_ga_fallback_reports_no_comments(self):
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.get_doc_review_view)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        assert result["read_source"] == curated_tools.READ_SOURCE_GA
        assert result["comments"] == []
        assert result["body_text"] == "Hello{+ brave+} world.\n"

    @pytest.mark.asyncio
    async def test_explicit_view_mode_passthrough(self):
        service = _docs_get_service(fx.build_tabs_payload([("t.0", fx.DOC_EMPTY)]))
        fn = _unwrap(curated_tools.get_doc_review_view)

        await fn(
            service,
            user_google_email=EMAIL,
            document_id="doc-fixture-1",
            view_mode="PREVIEW_SUGGESTIONS_ACCEPTED",
        )

        service.documents.return_value.get.assert_called_once_with(
            documentId="doc-fixture-1",
            suggestionsViewMode="PREVIEW_SUGGESTIONS_ACCEPTED",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )

    @pytest.mark.asyncio
    async def test_invalid_view_mode_rejected(self):
        service = _docs_get_service(fx.DOC_EMPTY)
        fn = _unwrap(curated_tools.get_doc_review_view)

        with pytest.raises(UserInputError, match="view_mode"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id="doc-fixture-1",
                view_mode="NOT_A_MODE",
            )
        service.documents.return_value.get.assert_not_called()


class TestClassifyPreviewError:
    def test_400_unknown_field_means_not_enrolled(self):
        availability, reason = preview_status.classify_preview_error(
            400,
            'Invalid JSON payload received. Unknown name "acceptSuggestion" '
            "at 'requests[0]': Cannot find field.",
        )
        assert availability == "unavailable"
        assert reason == "not_enrolled"

    def test_400_recognized_field_means_available(self):
        availability, reason = preview_status.classify_preview_error(
            400, "Invalid suggestion id: no-such-suggestion"
        )
        assert availability == "available"
        assert reason == "preview_request_type_recognized"

    def test_403_means_permission_or_scope(self):
        availability, reason = preview_status.classify_preview_error(
            403, "The caller does not have permission"
        )
        assert availability == "unknown"
        assert reason == "permission_or_scope"

    def test_404_means_document_not_found(self):
        availability, reason = preview_status.classify_preview_error(
            404, "Requested entity was not found."
        )
        assert availability == "unknown"
        assert reason == "document_not_found"

    def test_other_status_unknown(self):
        availability, reason = preview_status.classify_preview_error(
            500, "Internal error"
        )
        assert availability == "unknown"
        assert reason == "unexpected_http_500"


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_default_is_side_effect_free(self):
        service = Mock()
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        result = json.loads(await fn(service, user_google_email=EMAIL))

        service.documents.assert_not_called()
        assert result["probe_performed"] is False
        assert result["preview"]["availability"] == "unknown"

    @pytest.mark.asyncio
    async def test_inventory_lists_hand_written_tools(self):
        service = Mock()
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        result = json.loads(await fn(service, user_google_email=EMAIL))

        tools = result["tools"]
        assert tools["names"] == list(curated_tools.REVIEW_TOOL_NAMES)
        assert tools["total"] == len(tools["names"])
        assert {
            "list_document_suggestions",
            "get_doc_review_view",
            "check_docs_review_capabilities",
            "suggest_doc_edit",
            "manage_document_suggestion",
            "reply_to_doc_thread",
            "create_anchored_doc_comment",
        } == set(tools["names"])
        assert result["scopes"]

    @pytest.mark.asyncio
    async def test_probe_requires_document_id(self):
        service = Mock()
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        with pytest.raises(UserInputError, match="document_id"):
            await fn(service, user_google_email=EMAIL, probe=True)

    @pytest.mark.asyncio
    async def test_probe_sends_nonexistent_accept_suggestion(self):
        service = Mock()
        service.documents.return_value.batchUpdate.return_value.execute = Mock(
            return_value={
                "documentId": "d1",
                "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON",
            }
        )
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="d1", probe=True)
        )

        _, kwargs = service.documents.return_value.batchUpdate.call_args
        assert kwargs["documentId"] == "d1"
        requests = kwargs["body"]["requests"]
        assert len(requests) == 1
        assert list(requests[0]) == ["acceptSuggestion"]
        suggestion_id = requests[0]["acceptSuggestion"]["suggestionId"]
        assert "probe" in suggestion_id  # clearly non-real ID
        assert result["probe_performed"] is True
        assert result["preview"]["availability"] == "available"

    @pytest.mark.asyncio
    async def test_probe_classifies_not_enrolled_and_caches(self):
        service = Mock()
        service.documents.return_value.batchUpdate.return_value.execute = Mock(
            side_effect=_http_error(
                400,
                b'{"error": {"message": "Invalid JSON payload received. '
                b'Unknown name \\"acceptSuggestion\\" at \'requests[0]\'"}}',
            )
        )
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="d1", probe=True)
        )
        assert result["preview"]["availability"] == "unavailable"
        assert result["preview"]["evidence"]["reason"] == "not_enrolled"
        assert result["preview"]["evidence"]["http_status"] == 400

        # Second call WITHOUT probe reuses the cached classification and
        # never touches the API.
        quiet_service = Mock()
        cached = json.loads(await fn(quiet_service, user_google_email=EMAIL))
        quiet_service.documents.assert_not_called()
        assert cached["preview"]["availability"] == "unavailable"
        assert cached["preview"]["evidence"]["reason"] == "not_enrolled"

    @pytest.mark.asyncio
    async def test_one_callers_probe_is_not_another_callers_verdict(self):
        """The cache was one process-global dict, and multi-user is the
        server's DEFAULT transport mode (``main.py``: anything without
        ``--single-user``).

        Two things crossed between tenants on the probe-free branch, which
        makes no API call and answers purely from the cache: the VERDICT, and
        the EVIDENCE STRING -- and ``HttpError.__str__`` embeds the request
        URI, so that string carries the other tenant's document id.
        """
        doc_a = "1AaBbCc-TENANT-A-PRIVATE-DOC"
        resp = MagicMock()
        resp.status = 400
        resp.reason = "Bad Request"
        service_a = Mock()
        service_a.documents.return_value.batchUpdate.return_value.execute = Mock(
            # Shaped as googleapiclient raises it: `HttpError(resp, content,
            # uri=self.uri)`, so str() carries the document id.
            side_effect=HttpError(
                resp=resp,
                content=b'{"error": {"message": "Invalid value at requests[0]"}}',
                uri=f"https://docs.googleapis.com/v1/documents/{doc_a}:batchUpdate",
            )
        )
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        a = json.loads(
            await fn(
                service_a,
                user_google_email="tenant-a@example.com",
                document_id=doc_a,
                probe=True,
            )
        )
        # A's own report is unchanged: their evidence, their document.
        assert a["preview"]["availability"] == "available"
        assert doc_a in a["preview"]["evidence"]["message"]

        service_b = Mock()
        b = json.loads(await fn(service_b, user_google_email="tenant-b@example.com"))

        service_b.documents.assert_not_called()
        assert doc_a not in json.dumps(b), "another tenant's document id leaked"
        assert b["preview"]["availability"] == "unknown"
        assert b["preview"]["evidence"] is None
        assert b["preview"]["source"] is None

    @pytest.mark.asyncio
    async def test_a_callers_own_verdict_survives_another_callers_probe(self):
        """The control: keying must not cost a caller their own cache."""
        service = Mock()
        service.documents.return_value.batchUpdate.return_value.execute = Mock(
            side_effect=_http_error(
                400,
                b'{"error": {"message": "Invalid JSON payload received. '
                b'Unknown name \\"acceptSuggestion\\" at \'requests[0]\'"}}',
            )
        )
        fn = _unwrap(curated_tools.check_docs_review_capabilities)
        await fn(service, user_google_email=EMAIL, document_id="d1", probe=True)

        # Somebody else probes in between, with a different outcome.
        other = Mock()
        other.documents.return_value.batchUpdate.return_value.execute = Mock(
            return_value={"documentId": "d2"}
        )
        await fn(
            other, user_google_email="other@example.com", document_id="d2", probe=True
        )

        quiet = Mock()
        mine = json.loads(await fn(quiet, user_google_email=EMAIL))
        quiet.documents.assert_not_called()
        assert mine["preview"]["availability"] == "unavailable"
        assert mine["preview"]["evidence"]["reason"] == "not_enrolled"

    @pytest.mark.asyncio
    async def test_probe_403_is_permission_not_verdict(self):
        service = Mock()
        service.documents.return_value.batchUpdate.return_value.execute = Mock(
            side_effect=_http_error(
                403, b'{"error": {"message": "The caller does not have permission"}}'
            )
        )
        fn = _unwrap(curated_tools.check_docs_review_capabilities)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="d1", probe=True)
        )
        assert result["preview"]["availability"] == "unknown"
        assert result["preview"]["evidence"]["reason"] == "permission_or_scope"
