"""The tab and segment facts, pinned against prod.

``mockdocs`` used to model a single flat character array: one tab, one
segment, one coordinate space. Google Docs has none of those things. It
numbers **every ``(tabId, segmentId)`` pair from that pair's own start**, so
``start_index: 5`` in a header and ``start_index: 5`` in the body are
different characters, and an index emitted or compared without the tab and
segment it belongs to is not an address at all.

Three consecutive review rounds of ``gdocs_preview`` found that same bug
class, and every one of them was invisible to the mock-backed unit tests --
not because the tests were weak but because a single flat array **cannot
represent** a wrong-segment or wrong-tab write. Only the prod e2e suite
caught them. This file is the repair: each test below makes one of those
mistakes representable, and therefore catchable here, in milliseconds,
without an enrolled account.

Everything asserted here was checked against the live enrolled API on
2026-07-31 and is treated as ground truth:

1. Each tab is numbered from its own start. A document with two tabs, each
   with one suggestion near the top, reports ``start_index: 1`` for BOTH.
2. The body's first insertable position is index 1 (index 0 is the leading
   section break) -- in EVERY tab, not just the first.
3. A header/footer/footnote segment is numbered from its OWN start, and
   index 0 IS a valid position there (verified by inserting at
   ``{"index": 0, "segmentId": <headerId>}``).
4. The API serializes proto3, so ``startIndex: 0`` is never written out. A
   header's only paragraph came back as ``{"endIndex": 13, "paragraph": …}``
   with no ``startIndex``, and its inner element likewise.
5. Tab ids look like ``t.0`` and ``t.sxw3lc9vb0lk``; ``addDocumentTab``
   creates one and is unsupported in SUGGEST write mode.
6. Content moves out of the top-level ``body`` into
   ``tabs[i].documentTab.body`` in tabs mode, byte-identical with the same
   indexes; non-body segments live at ``.headers``/``.footers``/
   ``.footnotes``, keyed by segment id.
7. Suggestion ids and comment threads are DOCUMENT-wide, so ``suggestions``
   and ``comments`` stay at the top level of the payload.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from gdocs_preview.analysis import (
    _indexes,
    extract_suggestions_from_tabs,
    render_document,
)
from gdocs_preview.preview_read import suggestion_threads_by_id, tab_documents
from mockdocs.adapter import (
    SUGGEST_UNSUPPORTED_OFFICIAL,
    document_payload,
    segment_offsets,
    tabs_document_payload,
)
from mockdocs.fake_services import FakeBackend
from mockdocs.model import MockDoc

BODY_TEXT = "Alpha beta gamma.\n"
SECOND_TEXT = "Delta epsilon zeta.\n"
HEADER_TEXT = "Confidential\n"


def two_tab_backend() -> tuple[FakeBackend, MockDoc]:
    """A document with two tabs, a header on the first, and nothing pending.

    Ids are given explicitly so the assertions read; the model mints opaque
    ones (``t.<12 base-36 chars>``, ``kix.<…>``) when a seed does not, which
    is what prod does and what stops anything downstream from assuming tab
    ids are ordered.
    """
    backend = FakeBackend(me="alice")
    backend.seed(
        {
            "me": "alice",
            "documents": [
                {
                    "document_id": "d1",
                    "title": "Two Tabs",
                    "text": BODY_TEXT,
                    "headers": {"kix.h1": HEADER_TEXT},
                    "tabs": [
                        {
                            "tab_id": "t.second",
                            "title": "Appendix",
                            "text": SECOND_TEXT,
                        }
                    ],
                }
            ],
        }
    )
    return backend, backend.get_document("d1")


def batch(backend: FakeBackend, *requests, suggest: bool = True):
    body = {"requests": list(requests)}
    if suggest:
        body["writeControl"] = {"writeMode": "SUGGEST"}
    return (
        backend.docs_service()
        .documents()
        .batchUpdate(documentId="d1", body=body)
        .execute()
    )


def tabs_read(backend: FakeBackend) -> dict:
    return (
        backend.docs_service()
        .documents()
        .get(
            documentId="d1",
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )
        .execute()
    )


# ---------------------------------------------------------------------------
# Fact 1 + 2: every tab's body starts at 1, and the numbers repeat
# ---------------------------------------------------------------------------


def test_every_tabs_body_starts_at_index_one():
    """Index 0 is the leading section break in EVERY tab.

    The section break is the element that carries ``endIndex: 1`` and no
    ``startIndex``; the first paragraph begins at 1. A model that gave the
    second tab a running offset from the first would fail the second half of
    this immediately.
    """
    _backend, doc = two_tab_backend()
    payload = tabs_document_payload(doc)
    assert [t["tabProperties"]["tabId"] for t in payload["tabs"]] == ["t.0", "t.second"]
    for tab in payload["tabs"]:
        content = tab["documentTab"]["body"]["content"]
        assert content[0] == {"endIndex": 1, "sectionBreak": {"sectionStyle": {}}}
        assert "startIndex" not in content[0]
        assert content[1]["startIndex"] == 1
        assert content[1]["paragraph"]["elements"][0]["startIndex"] == 1


def test_a_suggestion_near_the_top_of_each_tab_reports_start_index_one_in_both():
    """Prod fact 1, verbatim: two tabs, one suggestion near the top of each,
    ``start_index: 1`` for BOTH.

    This is the exact shape that made three rounds of index bugs invisible.
    The two records are indistinguishable by index -- only ``tab_id`` tells
    them apart, which is why an index without its tab is not an address.
    """
    backend, doc = two_tab_backend()
    batch(
        backend,
        {"insertText": {"location": {"index": 1}, "text": "One "}},
        {"insertText": {"location": {"index": 1, "tabId": "t.second"}, "text": "Two "}},
    )
    payload = tabs_read(backend)
    result = extract_suggestions_from_tabs(
        [(t.tab_id, t.document) for t in tab_documents(payload)],
        threads=suggestion_threads_by_id(payload),
    )
    records = {r["tab_id"]: r for r in result["suggestions"]}

    assert set(records) == {"t.0", "t.second"}
    assert records["t.0"]["start_index"] == 1
    assert records["t.second"]["start_index"] == 1
    assert records["t.0"]["suggestion_id"] != records["t.second"]["suggestion_id"]
    assert records["t.0"]["end_index"] == 5
    assert records["t.second"]["end_index"] == 5
    assert records["t.0"]["post_text"] == "One "
    assert records["t.second"]["post_text"] == "Two "
    # Same index, same author, adjacent-looking ranges -- two separate cards.
    assert len(doc.registry) == 2


# ---------------------------------------------------------------------------
# Fact 3 + 4: a non-body segment starts at 0, and the payload omits it
# ---------------------------------------------------------------------------


def test_a_non_body_segment_starts_at_zero_and_omits_start_index():
    """Prod fact 4: ``{"endIndex": 13, "paragraph": …}`` with NO
    ``startIndex``, and the element inside it likewise.

    ``analysis._indexes`` exists solely to read that absence as 0 -- reading
    it as "unindexed" made every suggestion at the start of a header
    unaddressable. The mock has to produce the absence or that code path is
    never executed by anything but the e2e suite.
    """
    _backend, doc = two_tab_backend()
    header = doc.resolve_segment(segment_id="kix.h1")
    assert header.index_base == 0
    assert segment_offsets(header)[0] == 0

    payload = tabs_document_payload(doc)
    (paragraph,) = payload["tabs"][0]["documentTab"]["headers"]["kix.h1"]["content"]
    assert paragraph == {
        "endIndex": len(HEADER_TEXT),
        "paragraph": {
            "elements": [
                {
                    "endIndex": len(HEADER_TEXT),
                    "textRun": {"content": HEADER_TEXT, "textStyle": {}},
                }
            ],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }
    # No sectionBreak either: only a body has one.
    assert "sectionBreak" not in str(paragraph)
    # And the consumer reads the absence the way the API means it.
    assert _indexes(paragraph) == (0, len(HEADER_TEXT))
    assert _indexes(paragraph["paragraph"]["elements"][0]) == (0, len(HEADER_TEXT))


def test_index_zero_is_writable_in_a_header_and_not_in_a_body():
    """Prod fact 3, and its converse.

    ``{"index": 0, "segmentId": <headerId>}`` is a valid insert location; the
    same request against a body is out of bounds, because index 0 there is the
    section break. A single-base model cannot express both.
    """
    backend, doc = two_tab_backend()
    batch(
        backend,
        {"insertText": {"location": {"index": 0, "segmentId": "kix.h1"}, "text": "!"}},
    )
    assert doc.segment_text((doc.default_tab_id, "kix.h1")) == "!" + HEADER_TEXT

    with pytest.raises(HttpError) as exc:
        batch(backend, {"insertText": {"location": {"index": 0}, "text": "!"}})
    assert exc.value.resp.status == 400
    assert "out of bounds" in str(exc.value)


def test_a_header_suggestion_is_addressable_at_index_zero_end_to_end():
    """The bug the absence of ``startIndex`` used to cause, as a whole trip:
    suggest at the very start of a header, read back, and get an index the
    caller can hand straight back to a write."""
    backend, doc = two_tab_backend()
    batch(
        backend,
        {
            "insertText": {
                "location": {"index": 0, "segmentId": "kix.h1"},
                "text": "DRAFT ",
            }
        },
    )
    payload = tabs_read(backend)
    result = extract_suggestions_from_tabs(
        [(t.tab_id, t.document) for t in tab_documents(payload)],
        threads=suggestion_threads_by_id(payload),
    )
    (record,) = result["suggestions"]
    assert record["segment"] == "header"
    assert record["segment_id"] == "kix.h1"
    assert record["tab_id"] == "t.0"
    assert record["start_index"] == 0  # not None
    assert record["end_index"] == 6

    # The reported address round-trips into a further write.
    batch(
        backend,
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": record["start_index"],
                    "endIndex": record["end_index"],
                    "segmentId": record["segment_id"],
                    "tabId": record["tab_id"],
                }
            }
        },
    )
    assert doc.segment_text((doc.default_tab_id, "kix.h1"), "original") == HEADER_TEXT


# ---------------------------------------------------------------------------
# The same index, two places
# ---------------------------------------------------------------------------


def test_the_same_index_in_two_segments_addresses_different_characters():
    """Index 1 of the body, index 1 of the header and index 1 of the second
    tab's body are three different characters. The index alone cannot say
    which."""
    _backend, doc = two_tab_backend()
    body = doc.segment()
    header = doc.resolve_segment(segment_id="kix.h1")
    second = doc.resolve_segment(tab_id="t.second")

    at_one = {}
    for segment in (body, header, second):
        offsets = segment_offsets(segment)
        at_one[segment.describe()] = segment.chars[offsets.index(1)].cp

    # Body index 1 is its FIRST character (0 is the section break); header
    # index 1 is its second, because the header is numbered from 0.
    assert at_one[body.describe()] == "A"
    assert at_one[header.describe()] == "o"
    assert at_one[second.describe()] == "D"
    assert len(set(at_one.values())) == 3


# ---------------------------------------------------------------------------
# batchUpdate routing: the footgun, made reproducible
# ---------------------------------------------------------------------------


def _texts(doc: MockDoc) -> dict[str, str]:
    return {s.describe(): doc.segment_text(s.key) for s in doc.ordered_segments()}


def test_a_request_naming_a_segment_and_tab_mutates_that_one_and_no_other():
    backend, doc = two_tab_backend()
    before = _texts(doc)
    batch(
        backend,
        {
            "insertText": {
                "location": {"index": 1, "segmentId": "kix.h1", "tabId": "t.0"},
                "text": "X",
            }
        },
    )
    after = _texts(doc)
    header = doc.resolve_segment(segment_id="kix.h1").describe()
    assert after[header] == "CX" + HEADER_TEXT[1:]
    assert {k: v for k, v in after.items() if k != header} == {
        k: v for k, v in before.items() if k != header
    }


def test_a_request_naming_only_a_tab_mutates_that_tabs_body():
    backend, doc = two_tab_backend()
    before = _texts(doc)
    batch(
        backend,
        {"insertText": {"location": {"index": 1, "tabId": "t.second"}, "text": "X"}},
    )
    after = _texts(doc)
    second = doc.resolve_segment(tab_id="t.second").describe()
    assert after[second] == "X" + SECOND_TEXT
    assert {k: v for k, v in after.items() if k != second} == {
        k: v for k, v in before.items() if k != second
    }


def test_a_request_omitting_both_lands_in_the_default_tabs_body():
    """**The footgun.** A caller that forgot to carry the tab and segment
    alongside its index does not get an error -- it gets a successful write at
    a numerically valid index in a document it never read.

    Reproducing it is the point. The mock must not "help" by refusing the
    ambiguous request, because prod does not, and a mock that refused would
    make the production code look correct while it silently corrupted a
    header.
    """
    backend, doc = two_tab_backend()
    # An index the caller read out of the HEADER, written back without saying
    # so. It is in range for the body, so it commits.
    response = batch(
        backend, {"insertText": {"location": {"index": 1}, "text": "OOPS"}}
    )
    assert response["suggestionResponses"][0]["createdSuggestionIds"]

    assert doc.segment_text((doc.default_tab_id, None)) == "OOPS" + BODY_TEXT
    assert doc.segment_text((doc.default_tab_id, "kix.h1")) == HEADER_TEXT
    assert doc.segment_text(("t.second", None)) == SECOND_TEXT


def test_an_unknown_tab_or_segment_is_a_400():
    backend, _doc = two_tab_backend()
    for location in (
        {"index": 1, "tabId": "t.nope"},
        {"index": 1, "segmentId": "kix.nope"},
        {"index": 1, "segmentId": "kix.h1", "tabId": "t.second"},  # right id, wrong tab
    ):
        with pytest.raises(HttpError) as exc:
            batch(backend, {"insertText": {"location": location, "text": "x"}})
        assert exc.value.resp.status == 400


def test_end_of_segment_location_means_the_end_of_that_segment():
    backend, doc = two_tab_backend()
    batch(
        backend,
        {"insertText": {"endOfSegmentLocation": {"segmentId": "kix.h1"}, "text": "!"}},
    )
    assert doc.segment_text((doc.default_tab_id, "kix.h1")) == HEADER_TEXT + "!"
    assert doc.segment_text((doc.default_tab_id, None)) == BODY_TEXT


def test_a_range_in_a_header_may_omit_its_zero_start_index():
    """proto3 omits a zero, so a range echoed back from a header payload can
    arrive as ``{"endIndex": n, "segmentId": …}``. Reading that as a missing
    field would 400 on a request the API accepts.

    The same request against a body is rejected -- not by a special case, but
    because index 0 there is the section break and out of bounds.
    """
    backend, doc = two_tab_backend()
    batch(
        backend,
        {"deleteContentRange": {"range": {"endIndex": 4, "segmentId": "kix.h1"}}},
    )
    assert doc.segment_text((doc.default_tab_id, "kix.h1"), "final") == HEADER_TEXT[4:]

    with pytest.raises(HttpError) as exc:
        batch(backend, {"deleteContentRange": {"range": {"endIndex": 4}}})
    assert exc.value.resp.status == 400
    assert "out of bounds" in str(exc.value)


def test_a_comment_anchored_in_a_header_quotes_the_header():
    """``insertComment``'s range is a Range like any other, so its quote comes
    out of the segment it names. Quoting the body instead would put a
    plausible-looking sentence on a thread that is anchored elsewhere."""
    backend, _doc = two_tab_backend()
    batch(
        backend,
        {
            "insertComment": {
                "content": "why?",
                "range": {"endIndex": 12, "segmentId": "kix.h1"},
            }
        },
        suggest=False,
    )
    (thread,) = backend.comments["d1"]
    assert thread["plainTextQuote"] == "Confidential"


# ---------------------------------------------------------------------------
# Resolution and threads stay document-wide
# ---------------------------------------------------------------------------


def test_suggestion_ids_and_threads_are_document_wide():
    """Fact 7: ``acceptSuggestion`` names an id and nothing else, so it has to
    find that id wherever in the document it lives -- including a header of a
    tab the caller never mentioned."""
    backend, doc = two_tab_backend()
    batch(
        backend,
        {"insertText": {"location": {"index": 0, "segmentId": "kix.h1"}, "text": "A"}},
        {"insertText": {"location": {"index": 1, "tabId": "t.second"}, "text": "B"}},
    )
    payload = tabs_read(backend)
    assert set(suggestion_threads_by_id(payload)) == set(doc.registry)
    assert "suggestions" in payload
    assert all("suggestions" not in tab for tab in payload["tabs"])

    for sid in sorted(doc.registry):
        assert (
            backend.docs_service()
            .documents()
            .batchUpdate(
                documentId="d1",
                body={"requests": [{"acceptSuggestion": {"suggestionId": sid}}]},
            )
            .execute()
        )
    assert not doc.registry
    assert doc.segment_text((doc.default_tab_id, "kix.h1")) == "A" + HEADER_TEXT
    assert doc.segment_text(("t.second", None)) == "B" + SECOND_TEXT


def test_replace_all_text_sweeps_every_segment_of_every_tab():
    """``replaceAllText`` names no index and no segment, so its scope is the
    document -- every tab, every header."""
    backend = FakeBackend(me="alice")
    backend.seed(
        {
            "documents": [
                {
                    "document_id": "d1",
                    "text": "old body\n",
                    "headers": {"kix.h1": "old header\n"},
                    "tabs": [{"tab_id": "t.second", "text": "old appendix\n"}],
                }
            ]
        }
    )
    doc = backend.get_document("d1")
    batch(
        backend,
        {"replaceAllText": {"containsText": {"text": "old"}, "replaceText": "new"}},
    )
    assert doc.segment_text((doc.default_tab_id, None), "final") == "new body\n"
    assert doc.segment_text((doc.default_tab_id, "kix.h1"), "final") == "new header\n"
    assert doc.segment_text(("t.second", None), "final") == "new appendix\n"


# ---------------------------------------------------------------------------
# Merge (§6) stops at the segment boundary
# ---------------------------------------------------------------------------


def test_merge_does_not_join_two_tabs_at_the_same_index():
    """§6's ``gap`` is a distance in characters and there is no distance
    between two coordinate spaces.

    Both edits are by the same author and both sit at index 1 of a body, so a
    merge implemented over a document-wide range map would see gap 0 and
    absorb one into the other -- taking away one of the reviewer's two
    independent decisions (L8) on the strength of a coincidence.
    """
    _backend, doc = two_tab_backend()
    first = doc.insert(1, "X", "alice")
    second = doc.insert(1, "Y", "alice", ("t.second", None))
    assert first != second
    assert sorted(doc.registry) == sorted([first, second])
    assert doc.merge_log == []
    doc.check_invariants()


def test_merge_does_not_join_a_body_and_its_header():
    _backend, doc = two_tab_backend()
    body = doc.insert(0, "X", "alice", (doc.default_tab_id, None))
    header = doc.insert(0, "Y", "alice", (doc.default_tab_id, "kix.h1"))
    assert {body, header} == set(doc.registry)
    assert doc.merge_log == []


def test_merge_still_happens_inside_one_non_body_segment():
    """The converse: within a segment §6 behaves exactly as it always did, so
    the segment split narrowed merging rather than disabling it."""
    _backend, doc = two_tab_backend()
    key = (doc.default_tab_id, "kix.h1")
    doc.delete(0, 4, "alice", key)
    survivor = doc.insert(0, "Draft", "alice", key)
    assert len(doc.registry) == 1
    assert doc.merge_log
    assert doc.label(survivor)["kind"] == "Replace"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_the_ga_read_returns_only_the_default_tab():
    """``includeTabsContent=false`` is Google's backwards-compatibility read:
    the first tab, flattened to the top level, and nothing else. The degraded
    read path depends on it being lossy in exactly this way."""
    _backend, doc = two_tab_backend()
    plain = document_payload(doc)
    tabs = tabs_document_payload(doc)

    assert "tabs" not in plain
    assert plain["body"] == tabs["tabs"][0]["documentTab"]["body"]
    assert plain["headers"] == tabs["tabs"][0]["documentTab"]["headers"]
    assert SECOND_TEXT not in str(plain)
    # One implicit tab with no id -- exactly what the tools see on a GA read.
    (only,) = tab_documents(plain)
    assert only.tab_id is None
    rendered = render_document(only.document)
    assert rendered["body_text"] == BODY_TEXT
    assert rendered["headers"] == {"kix.h1": HEADER_TEXT}


def test_add_document_tab_is_unsupported_in_suggest_mode():
    """Fact 5. The mock has tabs now, so it is worth restating that nothing
    under test can create one: ``addDocumentTab`` is on the official
    suggest-unsupported list, which makes tabs a seeding concern."""
    assert "addDocumentTab" in SUGGEST_UNSUPPORTED_OFFICIAL
    backend, _doc = two_tab_backend()
    with pytest.raises(HttpError) as exc:
        batch(backend, {"addDocumentTab": {}})
    assert exc.value.resp.status == 400


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_the_pre_tabs_seed_shape_still_means_what_it_meant():
    """Every existing scenario uses this shape. It must stay single-tab
    (``t.0``), body-only, and byte-identical."""
    backend = FakeBackend(me="alice")
    backend.seed(
        {
            "me": "alice",
            "documents": [
                {
                    "document_id": "d1",
                    "title": "Plain",
                    "text": "Hello world.\n",
                    "suggestions": [
                        {"op": "delete", "start": 0, "end": 5, "author": "bob"}
                    ],
                    "comments": [{"content": "hm", "quote": "Hello"}],
                }
            ],
        }
    )
    doc = backend.get_document("d1")
    assert [t.tab_id for t in doc.tabs] == ["t.0"]
    assert list(doc.segments) == [("t.0", None)]
    assert doc.chars is doc.segments[("t.0", None)].chars
    (sid,) = doc.registry
    assert doc.label(sid)["struck"] == "Hello"
    assert doc.segment_of(sid).key == ("t.0", None)


def test_a_seed_can_place_ops_in_extra_tabs_and_non_body_segments():
    backend = FakeBackend(me="alice")
    backend.seed(
        {
            "documents": [
                {
                    "document_id": "d1",
                    "text": "Body.\n",
                    "headers": {"kix.h1": "Header.\n"},
                    "footers": ["Footer.\n"],
                    "suggestions": [
                        {
                            "op": "insert",
                            "index": 0,
                            "text": "A",
                            "segment_id": "kix.h1",
                            "author": "bob",
                        }
                    ],
                    "tabs": [
                        {
                            "tab_id": "t.second",
                            "title": "Appendix",
                            "text": "Second.\n",
                            "footnotes": {"kix.fn1": "A note.\n"},
                            "suggestions": [
                                {
                                    "op": "delete",
                                    "start": 0,
                                    "end": 6,
                                    "author": "bob",
                                },
                                {
                                    "op": "insert",
                                    "index": 0,
                                    "text": "N",
                                    "segment_id": "kix.fn1",
                                    "author": "bob",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    )
    doc = backend.get_document("d1")
    assert [t.tab_id for t in doc.tabs] == ["t.0", "t.second"]
    kinds = {s.key: s.kind for s in doc.ordered_segments()}
    assert kinds[("t.0", "kix.h1")] == "header"
    assert kinds[("t.second", "kix.fn1")] == "footnote"
    # A footer declared as a bare list gets a minted, opaque id.
    (footer,) = doc.tab_segments("t.0", "footer")
    assert footer.segment_id.startswith("kix.") and footer.segment_id != "kix.h1"

    homes = {sid: doc.segment_of(sid).key for sid in doc.registry}
    assert set(homes.values()) == {
        ("t.0", "kix.h1"),
        ("t.second", None),
        ("t.second", "kix.fn1"),
    }
    # Three same-author suggestions, each at index 0 of its own segment: three
    # cards, because §6 does not merge across segments.
    assert len(doc.registry) == 3
    doc.check_invariants()


def test_minted_tab_and_segment_ids_look_like_prods_and_are_deterministic():
    """Fact 5: ``t.0`` then opaque. The mock mints opaque ids rather than
    ``t.1`` so nothing downstream can sort them, do arithmetic on them, or
    assume ``t.0`` is a prefix of the rest -- and get away with it here."""
    first = MockDoc(text="a\n")
    second = MockDoc(text="a\n")
    a = first.add_tab()
    b = second.add_tab()
    assert a.tab_id == b.tab_id  # deterministic: snapshots stay comparable
    assert a.tab_id.startswith("t.") and a.tab_id != "t.1"
    assert a.tab_id[2:].isalnum() and len(a.tab_id[2:]) == 12
    assert first.add_segment("header").segment_id.startswith("kix.")


# ---------------------------------------------------------------------------
# State snapshots
# ---------------------------------------------------------------------------


def test_a_snapshot_round_trips_tabs_and_segments():
    from mockdocs.state import dump_backend, load_backend

    backend, doc = two_tab_backend()
    batch(
        backend,
        {"insertText": {"location": {"index": 0, "segmentId": "kix.h1"}, "text": "A"}},
    )
    restored = load_backend(dump_backend(backend)).get_document("d1")
    assert [t.tab_id for t in restored.tabs] == [t.tab_id for t in doc.tabs]
    assert {s.key: s.kind for s in restored.ordered_segments()} == {
        s.key: s.kind for s in doc.ordered_segments()
    }
    assert restored.display_text() == doc.display_text()
    assert restored.segment_of(next(iter(doc.registry))).key == ("t.0", "kix.h1")
    restored.check_invariants()


def test_a_pre_tabs_snapshot_still_loads():
    """Adding tabs is not a schema-version bump: a snapshot written before
    they existed has no ``segments`` key and describes a single-tab body-only
    document, which is exactly what it restores as."""
    from mockdocs.state import SCHEMA_VERSION, load_backend

    restored = load_backend(
        {
            "schema_version": SCHEMA_VERSION,
            "me": "alice",
            "documents": [
                {
                    "document_id": "old",
                    "title": "Old",
                    "chars": [
                        {"cp": "h", "ins": [], "dels": [], "colour": None},
                        {"cp": "i", "ins": [], "dels": [], "colour": None},
                    ],
                    "registry": {},
                    "clock": 3,
                    "counters": {},
                }
            ],
            "comments": {},
        }
    )
    doc = restored.get_document("old")
    assert [t.tab_id for t in doc.tabs] == ["t.0"]
    assert list(doc.segments) == [("t.0", None)]
    assert doc.display_text() == "hi"
