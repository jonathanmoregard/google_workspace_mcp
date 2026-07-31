"""Every agent-facing index payload is a complete address.

:mod:`gdocs_preview.address` exists to make "an index without its
``(tab, segment)``" unrepresentable, and its docstring says a future edit
that drops the pairing "has to delete a field from ``ADDRESS_FIELDS``". That
was only true of the payloads routed through it. Two of them --
``analysis.render_document``'s paragraph map and the suggestion records
``review_page.project`` returns verbatim under ``fields="full"`` -- built the
same five keys as dict literals, and ``create_anchored_doc_comment``'s
``requested_range`` built four of them and omitted ``segment``. Correct on the
day they were written; unguarded against the day they are edited.

This module is the guard. It enumerates every block the service emits that
carries a Docs index into an agent's context and asserts the address is
whole, so deleting a field from :data:`ADDRESS_FIELDS` is the only way to
shrink one -- which is where the reasons are written down.

The three-times-found bug it is defending against: an agent reads a bare
``start_index`` out of a response and hands it back to ``suggest_doc_edit``
or ``create_anchored_doc_comment``, both of which default to the BODY of the
DEFAULT tab. A header's index 5 aimed at the body is a silent write into the
wrong place in a customer document -- index 0 fails loud on the floor check,
every other index does not.
"""

from __future__ import annotations

import json

import pytest

from gdocs_preview import analysis, review_page, write_tools
from gdocs_preview.address import ADDRESS_FIELDS, SCOPE_FIELDS, address_of
from gdocs_preview.suggestion_ledger import _KEPT_FIELDS
from tests.gdocs_preview import fixtures as fx
from tests.gdocs_preview.test_write_tools import (
    DOC,
    EMAIL,
    _batch_service,
    _unwrap,
)

ADDRESS = set(ADDRESS_FIELDS)


def _addressed(payload: dict) -> set[str]:
    """The address fields actually present -- missing keys are the failure."""
    return ADDRESS & set(payload)


class TestAddressFieldsAreTheContract:
    def test_the_five_fields_are_what_the_write_tools_take(self):
        """If this drifts, every "pass these back unchanged" docstring is a
        lie, and the round-trip an agent is told to make stops working."""
        assert set(ADDRESS_FIELDS) == {
            "segment",
            "segment_id",
            "tab_id",
            "start_index",
            "end_index",
        }
        assert set(SCOPE_FIELDS) == ADDRESS - {"start_index", "end_index"}

    def test_address_of_never_omits_a_field(self):
        """``None`` means "this response did not say"; a missing key means
        the consumer cannot tell that from "the body"."""
        assert set(address_of({})) == ADDRESS
        assert address_of({}) == dict.fromkeys(ADDRESS_FIELDS)


class TestAnalysisPayloadsCarryTheWholeAddress:
    """MEDIUM 3: both of these are returned verbatim to an agent."""

    DOCUMENT = fx.build_doc(
        [fx.paragraph(fx.run("Body "), fx.run("edit", ins=["s.body"]), fx.run(".\n"))],
        headers={
            "kix.h1": [fx.paragraph(fx.run("Head "), fx.run("x", ins=["s.head"]))]
        },
        footnotes={"kix.fn1": [fx.paragraph(fx.run("Note", dels=["s.note"]))]},
    )

    def test_every_suggestion_record_is_addressable(self):
        result = analysis.extract_suggestions(self.DOCUMENT, tab_id="t.0")
        assert result["suggestions"], "fixture produced no records to check"
        for record in result["suggestions"]:
            assert _addressed(record) == ADDRESS, record["suggestion_id"]

    def test_every_paragraph_is_addressable(self):
        rendered = analysis.render_document(self.DOCUMENT, tab_id="t.0")
        assert rendered["paragraphs"], "fixture produced no paragraphs to check"
        for paragraph in rendered["paragraphs"]:
            assert _addressed(paragraph) == ADDRESS, paragraph.get("text")

    def test_a_paragraphs_address_is_populated_not_merely_present(self):
        """``with_address`` guarantees the KEYS; these pin the VALUES.

        Dropping a key from the address source would otherwise arrive as a
        well-shaped block of nulls, which reads as "this response did not
        say" -- true of the code, and false of the document.
        """
        by_segment = {
            p["segment"]: p
            for p in analysis.render_document(self.DOCUMENT, tab_id="t.0")["paragraphs"]
        }
        assert by_segment["header"]["segment_id"] == "kix.h1"
        assert by_segment["footnote"]["segment_id"] == "kix.fn1"
        assert by_segment["body"]["segment_id"] is None
        assert {p["tab_id"] for p in by_segment.values()} == {"t.0"}
        # A header paragraph starts at 0; a body paragraph at 1. Same
        # document, same map, different coordinate spaces.
        assert by_segment["header"]["start_index"] == 0
        assert by_segment["body"]["start_index"] == 1

    def test_a_header_record_is_distinguishable_from_a_body_one(self):
        """The point of the address, not just its shape: two records at the
        same index in different segments must not read alike."""
        by_id = {
            r["suggestion_id"]: r
            for r in analysis.extract_suggestions(self.DOCUMENT, tab_id="t.0")[
                "suggestions"
            ]
        }
        assert by_id["s.head"]["segment"] == "header"
        assert by_id["s.head"]["segment_id"] == "kix.h1"
        assert by_id["s.body"]["segment"] == "body"
        assert by_id["s.body"]["segment_id"] is None
        assert by_id["s.note"]["segment"] == "footnote"
        # Every one of them names the tab it was analysed in.
        assert {r["tab_id"] for r in by_id.values()} == {"t.0"}

    def test_both_field_modes_project_the_whole_address(self):
        (record,) = analysis.extract_suggestions(fx.DOC_PLAIN_INSERTION, tab_id="t.0")[
            "suggestions"
        ]
        for mode in review_page.LIST_FIELD_MODES:
            assert _addressed(review_page.project(record, mode)) == ADDRESS, mode

    def test_the_ledger_keeps_the_whole_address(self):
        """``resolved_suggestion`` is built straight from this cache."""
        assert ADDRESS <= set(_KEPT_FIELDS)


class TestWritePayloadsCarryTheWholeAddress:
    def test_the_post_write_echo_is_addressable(self):
        (record,) = analysis.extract_suggestions(fx.DOC_PLAIN_INSERTION, tab_id="t.0")[
            "suggestions"
        ]
        assert _addressed(write_tools._echo_suggestion(record)) == ADDRESS

    @pytest.mark.asyncio
    async def test_the_requested_range_echo_is_addressable(self):
        """MEDIUM 4: this one really was missing ``segment``."""
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "insertComment": {
                            "commentThread": {
                                "commentId": "c1",
                                "plainTextQuote": "Note",
                                "headPost": {"postId": "p1", "content": "why?"},
                            }
                        }
                    }
                ],
            }
        )
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="why?",
                start_index=0,
                end_index=4,
                segment_id="kix.fn1",
                tab_id="t.0",
            )
        )

        anchored = result["verification"]["requested_range"]
        assert _addressed(anchored) == ADDRESS
        assert anchored["segment_id"] == "kix.fn1"
        assert anchored["tab_id"] == "t.0"
        # No read is made, so the segment KIND is unknown rather than
        # guessed to be the body -- which is what an omitted key read as.
        assert anchored["segment"] is None

    @pytest.mark.asyncio
    async def test_a_body_anchor_says_body(self):
        service = _batch_service(
            {"commentUpdateState": "ALL_SAVED", "replies": []},
        )
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="why?",
                start_index=1,
                end_index=4,
            )
        )
        assert result["verification"]["requested_range"]["segment"] == "body"


class TestScopeEchoesAreCompleteToo:
    """A resolved ``(tab, segment)`` is an address without the offsets."""

    RECORDS = [
        {
            "suggestion_id": "s.1",
            "segment": "body",
            "segment_id": None,
            "tab_id": "t.0",
            "start_index": 5,
            "end_index": 9,
        }
    ]

    def test_the_listings_range_scope_is_a_whole_scope(self):
        _, applied = review_page.filter_records(
            self.RECORDS, tab_ids=("t.0",), start_index=0, end_index=100
        )
        assert set(applied["range_scope"]) == set(SCOPE_FIELDS)

    def test_the_review_views_window_scope_is_a_whole_scope(self):
        rendered = analysis.render_document(fx.DOC_PLAIN_INSERTION, tab_id="t.0")
        view = review_page.build_review_view(
            rendered, fields="full", tab_ids=("t.0",), start_index=1, end_index=100
        )
        assert set(view["window"]["scope"]) == set(SCOPE_FIELDS)
