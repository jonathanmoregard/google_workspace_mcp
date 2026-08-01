"""Property tests for the API adapter, including cross-validation against the
repo's real ``gdocs_preview.analysis``.

The adapter's job is unit conversion and coordinate-space resolution: the
model counts grapheme clusters, the Docs API counts UTF-16 code units, and it
counts them **per ``(tab, segment)``**. Three families of property here:

1. **Index discipline** -- every ``startIndex``/``endIndex`` the adapter emits
   slices the text it emits, under UTF-16, exactly, *within the segment that
   emitted it*.
2. **View-mode fidelity** -- each ``suggestionsViewMode`` returns its SPEC §3
   projection, per segment.
3. **Cross-validation** -- ``analysis.extract_suggestions`` run on the
   adapter's output must compute the same per-suggestion pre/post text, span
   AND address the model computes from the char arrays. This checks the mock
   and ``analysis.py`` against one algebra: a disagreement means one of them
   is wrong, and neither gets to be the oracle.

Every property here is quantified over multi-tab, multi-segment documents
(:func:`tests.mockdocs.strategies.tabbed_docs`, which also generates the
degenerate single-tab body-only case). An index is only half of an address,
and a property stated over one flat coordinate space cannot see the half it
is missing.

The tab and segment *facts* those properties rest on -- every tab's body
starts at 1, a non-body segment starts at 0 and omits its ``startIndex``, a
batchUpdate goes exactly where its ``tabId``/``segmentId`` say -- are pinned
separately in ``tests/mockdocs/test_tabs_and_segments.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from googleapiclient.errors import HttpError
from hypothesis import HealthCheck, given, settings

from gdocs_preview import preview_status
from gdocs_preview.analysis import (
    extract_suggestions_from_tabs,
    render_document,
    utf16_len,
)
from gdocs_preview.preview_read import suggestion_threads_by_id, tab_documents
from mockdocs.adapter import (
    PREVIEW_REQUEST_TYPES,
    SUGGEST_UNSUPPORTED_MESSAGE,
    SUGGEST_UNSUPPORTED_OFFICIAL,
    document_payload,
    segment_offsets,
    tabs_document_payload,
    to_grapheme_index,
)
from mockdocs.fake_services import FakeBackend
from mockdocs.model import MockDoc
from tests.mockdocs.strategies import suggestion_docs, tabbed_docs

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _elements(content: list[dict]) -> list[dict]:
    return [
        element
        for structural in content
        if "paragraph" in structural
        for element in structural["paragraph"]["elements"]
    ]


def _content_text(content: list[dict]) -> str:
    return "".join(e["textRun"]["content"] for e in _elements(content))


def _payload_text(payload: dict) -> str:
    return _content_text(payload["body"]["content"])


def _payload_segments(payload: dict) -> list[tuple[str, str, int, list[dict]]]:
    """``(tab_id, segment_id, index_base, content)`` over a tabs-mode payload.

    Flattened through the production ``preview_read.tab_documents``, so the
    test walks the payload the way the tools do rather than the way the mock
    built it.
    """
    out: list[tuple[str, str, int, list[dict]]] = []
    for tab in tab_documents(payload):
        document = tab.document
        out.append(
            (tab.tab_id, None, 1, (document.get("body") or {}).get("content", []))
        )
        for field in ("headers", "footers", "footnotes"):
            for seg_id in sorted(document.get(field) or {}):
                segment = document[field][seg_id] or {}
                out.append((tab.tab_id, seg_id, 0, segment.get("content", [])))
    return out


# ---------------------------------------------------------------------------
# 1. Index discipline
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=tabbed_docs())
def test_emitted_indexes_slice_emitted_text_under_utf16(doc):
    """The defining property of the API boundary: indexes are UTF-16 code
    units into **their own segment's** text, so slicing UTF-16 by them
    reproduces each run's own content. A code-point-based implementation fails
    this on the first emoji; an implementation that numbered every segment
    from one shared origin fails it on the first header.

    ``startIndex`` is read with a default of 0, not required: proto3 omits it
    there, and demanding it would make this test pass only on bodies.
    """
    for mode in (
        "SUGGESTIONS_INLINE",
        "PREVIEW_WITHOUT_SUGGESTIONS",
        "PREVIEW_SUGGESTIONS_ACCEPTED",
    ):
        payload = tabs_document_payload(doc, mode)
        for _tab_id, _seg_id, base, content in _payload_segments(payload):
            units = _content_text(content).encode("utf-16-le")
            for element in _elements(content):
                start = element.get("startIndex", 0)
                end = element["endIndex"]
                text = element["textRun"]["content"]
                # Two bytes per UTF-16 code unit; the segment's own base.
                sliced = units[(start - base) * 2 : (end - base) * 2].decode(
                    "utf-16-le"
                )
                assert sliced == text
                assert end - start == utf16_len(text)


@SETTINGS
@given(doc=tabbed_docs())
def test_indexes_are_contiguous_and_paragraphs_align(doc):
    """Within one segment the indexes run without gaps from that segment's own
    base -- 1 for a body (past the leading section break), 0 for the rest."""
    payload = tabs_document_payload(doc, "SUGGESTIONS_INLINE")
    for _tab_id, _seg_id, base, content in _payload_segments(payload):
        cursor = base
        for structural in content:
            if "paragraph" not in structural:
                continue
            assert structural.get("startIndex", 0) == cursor
            for element in structural["paragraph"]["elements"]:
                assert element.get("startIndex", 0) == cursor
                cursor = element["endIndex"]
            assert structural["endIndex"] == cursor


@SETTINGS
@given(doc=tabbed_docs())
def test_utf16_grapheme_index_round_trip(doc):
    """Round-trips per segment, at that segment's base. The same UTF-16 number
    round-trips to a different grapheme index in each segment, which is the
    whole hazard."""
    for segment in doc.ordered_segments():
        offsets = segment_offsets(segment)
        assert offsets[0] == segment.index_base
        for grapheme_index, utf16_index in enumerate(offsets):
            assert (
                to_grapheme_index(segment.chars, utf16_index, segment.index_base)
                == grapheme_index
            )


@SETTINGS
@given(doc=tabbed_docs())
def test_indexes_inside_a_character_are_rejected(doc):
    """A tool that computed indexes with Python ``len()`` on an emoji-bearing
    document lands between the surrogates; the API rejects that and so must
    the mock."""
    from mockdocs.model import MockDocsError

    for segment in doc.ordered_segments():
        offsets = set(segment_offsets(segment))
        for candidate in range(segment.index_base, max(offsets) + 1):
            if candidate in offsets:
                continue
            with pytest.raises(MockDocsError):
                to_grapheme_index(segment.chars, candidate, segment.index_base)
            break


# ---------------------------------------------------------------------------
# 2. View-mode fidelity (SPEC §3)
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=tabbed_docs())
def test_view_modes_return_their_projections(doc):
    """Each view mode projects every segment, not only the body."""
    for mode, projection in (
        ("SUGGESTIONS_INLINE", "display"),
        ("PREVIEW_WITHOUT_SUGGESTIONS", "original"),
        ("PREVIEW_SUGGESTIONS_ACCEPTED", "final"),
    ):
        payload = tabs_document_payload(doc, mode)
        for tab_id, seg_id, _base, content in _payload_segments(payload):
            assert _content_text(content) == doc.segment_text(
                (tab_id, seg_id), projection
            )


@SETTINGS
@given(doc=tabbed_docs())
def test_clean_views_carry_no_suggestion_marks(doc):
    for mode in ("PREVIEW_WITHOUT_SUGGESTIONS", "PREVIEW_SUGGESTIONS_ACCEPTED"):
        payload = tabs_document_payload(doc, mode)
        for _tab_id, _seg_id, _base, content in _payload_segments(payload):
            for element in _elements(content):
                assert "suggestedInsertionIds" not in element["textRun"]
                assert "suggestedDeletionIds" not in element["textRun"]
        for tab in tab_documents(payload):
            rendered = render_document(tab.document, tab_id=tab.tab_id)
            assert "{+" not in rendered["body_text"]
            assert "{-" not in rendered["body_text"]
            assert rendered["suggestion_ids"] == []


# ---------------------------------------------------------------------------
# 3. Cross-validation: the mock's algebra vs the real analysis.py
# ---------------------------------------------------------------------------


def _model_pre_post(doc: MockDoc, sid: str) -> tuple[str, str]:
    """Per-suggestion pre/post text computed from the char array, using
    ``analysis.py``'s documented semantics:

    pre  = base text of the affected range (ALL insertions stripped, ALL
           deletions kept).
    post = the range with S -- and only S -- applied.

    Sliced out of the suggestion's OWN segment: ``ranges()`` is in that
    segment's index space, and slicing the body with a header's range would
    quietly produce the wrong window rather than an error.
    """
    lo, hi = doc.ranges()[sid]
    window = doc.segment_of(sid).chars[lo:hi]
    pre = "".join(c.cp for c in window if not c.ins)
    post = "".join(
        c.cp for c in window if (sid in c.ins) or (not c.ins and sid not in c.dels)
    )
    return pre, post


@SETTINGS
@given(doc=tabbed_docs())
def test_analysis_agrees_with_the_model(doc):
    """End-to-end: drive the REAL ``extract_suggestions_from_tabs`` over
    adapter output and require it to reproduce the model's per-suggestion
    pre/post text, span, type, author AND address.

    Authors ride the tabs+comments read (the only one that carries thread
    objects), so this drives the whole production read path: tabs payload ->
    ``preview_read`` normalizers -> ``analysis``.

    The address assertions are the ones the flat model could not make. A
    record's ``start_index`` is checked against its own segment's offsets and
    then fed back through :func:`to_grapheme_index` *at that segment's base*,
    so a record that named the right number against the wrong segment fails
    here instead of at some future review round.
    """
    payload = tabs_document_payload(doc, "SUGGESTIONS_INLINE", me="alice")
    tabs = tab_documents(payload)
    result = extract_suggestions_from_tabs(
        [(t.tab_id, t.document) for t in tabs],
        threads=suggestion_threads_by_id(payload),
    )

    assert result["document_id"] == doc.document_id
    assert result["suggestion_count"] == len(doc.registry)
    assert {r["suggestion_id"] for r in result["suggestions"]} == set(doc.registry)

    spans = doc.ranges()
    for record in result["suggestions"]:
        sid = record["suggestion_id"]
        home = doc.segment_of(sid)
        pre, post = _model_pre_post(doc, sid)
        assert record["pre_text"] == pre, f"pre_text mismatch for {sid}"
        assert record["post_text"] == post, f"post_text mismatch for {sid}"

        assert record["tab_id"] == home.tab_id
        assert record["segment_id"] == home.segment_id
        assert record["segment"] == home.kind

        offsets = segment_offsets(home)
        lo, hi = spans[sid]
        assert record["start_index"] == offsets[lo]
        assert record["end_index"] == offsets[hi]
        # The reported index must be usable to address the model again -- and
        # only in the segment it was reported for.
        assert (
            to_grapheme_index(home.chars, record["start_index"], home.index_base) == lo
        )

        has_ins = any(sid in c.ins for c in home.chars)
        has_del = any(sid in c.dels for c in home.chars)
        expected_type = (
            "replacement"
            if has_ins and has_del
            else "insertion"
            if has_ins
            else "deletion"
        )
        assert record["type"] == expected_type

        assert record["author"]["display_name"] == doc.registry[sid].author
        assert record["author_source"] == "suggestion_thread"
        assert record["author"]["me"] == (doc.registry[sid].author == "alice")
        assert record["status"] == "OPEN"
        assert record["summary_text"] == doc.label(sid)["text"]


@SETTINGS
@given(doc=tabbed_docs())
def test_rendered_markers_match_the_render_states(doc):
    """``render_document``'s CriticMarkup output must agree with §4: every
    char whose render state involves an insertion mark appears inside
    ``{+...+}``, every purely struck char inside ``{-...-}``.

    Asserted per tab and per segment against ``segment_text``. Deliberately
    NOT against a whole-document concatenation: the review layer is free to
    put separators between tabs (and does), and this test is about the marker
    round-trip, not about presentation.
    """
    payload = tabs_document_payload(doc, "SUGGESTIONS_INLINE")

    def unmark(text: str) -> str:
        for marker in ("{+", "+}", "{-", "-}"):
            text = text.replace(marker, "")
        return text

    seen: set[str] = set()
    for tab in tab_documents(payload):
        rendered = render_document(tab.document, tab_id=tab.tab_id)
        assert unmark(rendered["body_text"]) == doc.segment_text((tab.tab_id, None))
        for field in ("headers", "footers", "footnotes"):
            for entry in rendered[field]:
                # Every entry is a whole address, so the segment it claims to
                # be can be looked up in the tab it claims to be in.
                assert entry["tab_id"] == tab.tab_id
                assert unmark(entry["text"]) == doc.segment_text(
                    (entry["tab_id"], entry["segment_id"])
                )
        seen.update(rendered["suggestion_ids"])
    assert seen == set(doc.registry)


# ---------------------------------------------------------------------------
# 3b. Read-mode fidelity: where threads live (verified against prod 2026-07-30)
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=suggestion_docs())
def test_plain_get_carries_no_thread_objects(doc):
    """The real plain ``documents.get`` returns no comment or suggestion
    threads at all -- only the tabs+comments read does. A mock that leaked
    threads into the plain payload would hide the whole preview read path."""
    payload = document_payload(doc, "SUGGESTIONS_INLINE", me="alice")
    assert "suggestions" not in payload
    assert "comments" not in payload
    assert "suggestionThreads" not in payload
    assert "tabs" not in payload
    assert payload["commentsViewMode"] == "COMMENTS_VIEW_MODE_OMITTED"
    assert suggestion_threads_by_id(payload) == {}


@SETTINGS
@given(doc=tabbed_docs())
def test_tabs_read_moves_the_body_and_adds_threads(doc):
    """Tabs mode: no top-level ``body``, content under
    ``tabs[i].documentTab.body`` with the SAME indexes, threads at the top
    level. Verified against the real API.

    The GA read is the FIRST tab byte-for-byte and nothing else -- that is
    what "``includeTabsContent=false`` returns only the first tab" means, and
    a mock that silently merged the other tabs in would make the degraded read
    look lossless.
    """
    plain = document_payload(doc, "SUGGESTIONS_INLINE", me="alice")
    payload = tabs_document_payload(doc, "SUGGESTIONS_INLINE", me="alice")

    assert "body" not in payload
    assert [t["tabProperties"]["tabId"] for t in payload["tabs"]] == [
        t.tab_id for t in doc.tabs
    ]
    first = payload["tabs"][0]
    assert first["documentTab"]["body"] == plain["body"]
    assert first["tabProperties"]["tabId"] == "t.0"
    for field in ("headers", "footers", "footnotes"):
        assert first["documentTab"].get(field) == plain.get(field)
    # Threads are document-wide: top level, not per tab.
    assert payload["commentsViewMode"] == "COMMENTS_VIEW_MODE_INCLUDED"
    assert set(suggestion_threads_by_id(payload)) == set(doc.registry)
    for tab in payload["tabs"]:
        assert "suggestions" not in tab and "comments" not in tab
    for thread in payload.get("suggestions", []):
        # A suggestion head post has an author but no content (prod shape).
        assert thread["headPost"]["author"]["displayName"]
        assert "content" not in thread["headPost"]


@SETTINGS
@given(doc=suggestion_docs())
def test_tabs_read_without_comments_view_mode_omits_threads(doc):
    payload = tabs_document_payload(
        doc, "SUGGESTIONS_INLINE", me="alice", include_comments=False
    )
    assert "tabs" in payload
    assert "suggestions" not in payload
    assert payload["commentsViewMode"] == "COMMENTS_VIEW_MODE_OMITTED"


def test_comments_view_mode_requires_tabs_content():
    """Real API 2026-07-30: 400 "Comments view mode may only be specified if
    tabs content is also requested." """
    backend, _ = _backend_with()
    with pytest.raises(HttpError) as exc:
        backend.docs_service().documents().get(
            documentId="d1", commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED"
        ).execute()
    assert exc.value.resp.status == 400
    assert "tabs content" in str(exc.value)


def test_invalid_comments_view_mode_is_rejected():
    backend, _ = _backend_with()
    with pytest.raises(HttpError) as exc:
        backend.docs_service().documents().get(
            documentId="d1",
            commentsViewMode="COMMENTS_EXCLUDED",
            includeTabsContent=True,
        ).execute()
    assert exc.value.resp.status == 400
    assert "CommentsViewMode" in str(exc.value)


def test_tabs_read_surfaces_comment_threads_with_authors():
    backend, _ = _backend_with()
    backend.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {
                    "insertComment": {
                        "content": "check this",
                        "range": {"startIndex": 1, "endIndex": 6},
                    }
                }
            ]
        },
    ).execute()
    payload = (
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
    from gdocs_preview.preview_read import comment_threads

    (thread,) = comment_threads(payload)
    assert thread["comment_id"]
    assert thread["author"]["display_name"] == "alice"
    assert thread["quoted_text"] == "Hello"


def test_label_grammar_matches_prod_summary_text():
    """Prod is the oracle for §8's label grammar: typographic quotes, and
    ``Replace: "x" with "y"`` for a replacement (verified 2026-07-30)."""
    backend, doc = _backend_with(text="Hello brave world.\n")
    docs = backend.docs_service()
    docs.documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 12}}},
                {"insertText": {"location": {"index": 7}, "text": "bold"}},
            ],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    (sid,) = doc.registry
    assert doc.label(sid)["text"] == "Replace: “brave” with “bold”"

    backend2, doc2 = _backend_with(text="Hello brave world.\n")
    backend2.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 12}}}
            ],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    (sid2,) = doc2.registry
    assert doc2.label(sid2)["text"] == "Delete: “brave”"

    backend3, doc3 = _backend_with(text="Hello world.\n")
    backend3.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "Say "}}],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    (sid3,) = doc3.registry
    assert doc3.label(sid3)["text"] == "Add: “Say”"


# ---------------------------------------------------------------------------
# 4. batchUpdate semantics
# ---------------------------------------------------------------------------


def _backend_with(text: str = "Hello 🎉 world.\n") -> tuple[FakeBackend, MockDoc]:
    backend = FakeBackend(me="alice")
    doc = backend.add_document(text=text, document_id="d1", title="T")
    return backend, doc


def test_suggest_mode_creates_suggestions_edit_mode_mutates_base():
    backend, doc = _backend_with()
    docs = backend.docs_service()

    docs.documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "Oh "}}],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    assert doc.original_text() == "Hello 🎉 world.\n"
    assert doc.display_text() == "Oh Hello 🎉 world.\n"
    assert len(doc.registry) == 1

    docs.documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "X"}}],
            "writeControl": {"writeMode": "EDIT"},
        },
    ).execute()
    assert doc.original_text() == "XHello 🎉 world.\n"


def test_suggest_mode_rejects_the_officially_unsupported_request_types():
    """Prod's message, verbatim, for each of Google's 8.

    Measured against the live API 2026-08-01
    (``e2e/test_suggest_semantics.py``): the refusal names the request slot
    and says *why*, which is what distinguishes it from a request that was
    simply malformed.
    """
    backend, _ = _backend_with()
    docs = backend.docs_service()
    for kind in sorted(SUGGEST_UNSUPPORTED_OFFICIAL):
        with pytest.raises(HttpError) as exc:
            docs.documents().batchUpdate(
                documentId="d1",
                body={
                    "requests": [{kind: {}}],
                    "writeControl": {"writeMode": "SUGGEST"},
                },
            ).execute()
        assert exc.value.resp.status == 400
        assert f"requests[0].{kind}" in str(exc.value)
        assert SUGGEST_UNSUPPORTED_MESSAGE in str(exc.value)


def test_the_preview_thread_operations_run_inside_a_suggest_batch():
    """Fact 5, resolved. The overlay excluded all 8 preview thread ops from
    SUGGEST batches on the reasoning that they act on threads rather than
    content. Prod does not: verified 2026-08-01, every one of them runs, takes
    effect, and reports ``commentUpdateState: ALL_SAVED``. A mock that refuses
    what prod accepts makes a real capability unreachable from every scenario
    built on it -- notably the mixed content-edit + comment batch below.
    """
    assert not (PREVIEW_REQUEST_TYPES & SUGGEST_UNSUPPORTED_OFFICIAL)
    backend, doc = _backend_with()
    docs = backend.docs_service()

    response = (
        docs.documents()
        .batchUpdate(
            documentId="d1",
            body={
                "requests": [
                    {"insertText": {"location": {"index": 1}, "text": "Oh "}},
                    {
                        "insertComment": {
                            "content": "in a suggest batch",
                            "range": {"startIndex": 1, "endIndex": 4},
                        }
                    },
                ],
                "writeControl": {"writeMode": "SUGGEST"},
            },
        )
        .execute()
    )
    assert response["suggestionResponses"][0]["createdSuggestionIds"]
    # 1:1 with the requests: the comment occupies its slot with no ids. (The
    # mock spells the empty slot out as five empty lists where prod, proto3,
    # sends a bare ``{}``; a pre-existing difference, and every reader of the
    # field goes through ``.get(...) or []``.)
    assert not any(response["suggestionResponses"][1].values())
    thread = response["replies"][1]["insertComment"]["commentThread"]
    assert thread["headPost"]["content"] == "in a suggest batch"
    assert response["commentUpdateState"] == "ALL_SAVED"

    # ... and a resolution op, in a SUGGEST batch, still resolves.
    (sid,) = doc.registry
    accepted = (
        docs.documents()
        .batchUpdate(
            documentId="d1",
            body={
                "requests": [{"acceptSuggestion": {"suggestionId": sid}}],
                "writeControl": {"writeMode": "SUGGEST"},
            },
        )
        .execute()
    )
    assert accepted["suggestionResponses"][0]["acceptedSuggestionIds"] == [sid]
    assert doc.original_text() == "Oh Hello 🎉 world.\n"


def test_a_suggest_batch_resolves_later_indexes_against_the_inline_space():
    """The mock matches prod's index resolution, measured 2026-08-01.

    A SUGGEST batch is PROGRESSIVE, like an EDIT batch -- request 1 is
    addressed against what request 0 left -- but it progresses in the
    SUGGESTIONS_INLINE space, where a suggested deletion has removed nothing.
    So an insertion shifts what follows and a deletion does not.
    """
    backend, doc = _backend_with(text="0123456789\n")
    docs = backend.docs_service()
    docs.documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": "AAAA"}},
                {"insertText": {"location": {"index": 5}, "text": "B"}},
            ],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    # Pre-batch resolution would have put B at the seed's own index 5, giving
    # "AAAA0123B456789".
    assert doc.display_text() == "AAAAB0123456789\n"
    assert doc.original_text() == "0123456789\n"

    backend2, doc2 = _backend_with(text="0123456789\n")
    backend2.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5}}},
                {"insertText": {"location": {"index": 6}, "text": "Z"}},
            ],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    # The suggested deletion left "0123" in the inline space, so index 6 is
    # still '5'. Under EDIT semantics the same batch gives "45678Z9".
    assert doc2.display_text() == "01234Z56789\n"
    assert doc2.final_text() == "4Z56789\n"


def test_indexes_are_utf16_at_the_batch_boundary():
    """The emoji is 2 UTF-16 units: deleting [7, 9) must remove exactly it."""
    backend, doc = _backend_with()
    assert utf16_len("Hello ") == 6
    backend.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {"deleteContentRange": {"range": {"startIndex": 7, "endIndex": 9}}}
            ],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    (sid,) = doc.registry
    assert doc.label(sid)["struck"] == "🎉"
    assert doc.final_text() == "Hello  world.\n"


def test_batch_index_inside_emoji_is_rejected():
    backend, _ = _backend_with()
    with pytest.raises(HttpError) as exc:
        backend.docs_service().documents().batchUpdate(
            documentId="d1",
            body={
                "requests": [
                    {"deleteContentRange": {"range": {"startIndex": 8, "endIndex": 9}}}
                ]
            },
        ).execute()
    assert exc.value.resp.status == 400
    assert "character boundary" in str(exc.value)


def test_replacement_batch_reports_only_live_suggestion_ids():
    """A SUGGEST replacement is two requests that §6 merges into one
    suggestion; the response must not name the absorbed id (see
    ``BatchUpdateApplier._resolve_merges``)."""
    backend, doc = _backend_with()
    response = (
        backend.docs_service()
        .documents()
        .batchUpdate(
            documentId="d1",
            body={
                "requests": [
                    {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 6}}},
                    {"insertText": {"location": {"index": 1}, "text": "Howdy"}},
                ],
                "writeControl": {"writeMode": "SUGGEST"},
            },
        )
        .execute()
    )
    reported = {
        sid
        for sr in response["suggestionResponses"]
        for sid in sr["createdSuggestionIds"]
    }
    assert reported == set(doc.registry)
    assert len(reported) == 1


def test_accept_and_reject_round_trip_through_the_api():
    backend, doc = _backend_with()
    docs = backend.docs_service()
    docs.documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [{"insertText": {"location": {"index": 1}, "text": "New "}}],
            "writeControl": {"writeMode": "SUGGEST"},
        },
    ).execute()
    (sid,) = doc.registry

    response = (
        docs.documents()
        .batchUpdate(
            documentId="d1",
            body={"requests": [{"acceptSuggestion": {"suggestionId": sid}}]},
        )
        .execute()
    )
    assert response["suggestionResponses"][0]["acceptedSuggestionIds"] == [sid]
    assert doc.display_text() == "New Hello 🎉 world.\n"
    assert not doc.registry

    with pytest.raises(HttpError) as exc:
        docs.documents().batchUpdate(
            documentId="d1",
            body={"requests": [{"acceptSuggestion": {"suggestionId": sid}}]},
        ).execute()
    assert exc.value.resp.status == 400


def test_comment_surfaces_are_shared():
    """A preview ``insertComment`` is visible to the GA Drive comment tools:
    there is only one comment on the document."""
    backend, _ = _backend_with()
    backend.docs_service().documents().batchUpdate(
        documentId="d1",
        body={
            "requests": [
                {
                    "insertComment": {
                        "content": "check this",
                        "range": {"startIndex": 1, "endIndex": 6},
                    }
                }
            ]
        },
    ).execute()
    listed = backend.drive_service().comments().list(fileId="d1").execute()
    (comment,) = listed["comments"]
    assert comment["content"] == "check this"
    assert comment["quotedFileContent"]["value"] == "Hello"
    assert comment["resolved"] is False

    backend.drive_service().replies().create(
        fileId="d1",
        commentId=comment["id"],
        body={"content": "done", "action": "resolve"},
    ).execute()
    (comment,) = (
        backend.drive_service().comments().list(fileId="d1").execute()["comments"]
    )
    assert comment["resolved"] is True


def test_comment_update_state_signals_partial_failure():
    backend, _ = _backend_with()
    backend.fail_comment_updates = True
    response = (
        backend.docs_service()
        .documents()
        .batchUpdate(
            documentId="d1",
            body={
                "requests": [
                    {
                        "insertComment": {
                            "content": "x",
                            "range": {"startIndex": 1, "endIndex": 3},
                        }
                    }
                ]
            },
        )
        .execute()
    )
    assert response["commentUpdateState"] == "ALL_FAILED_UNKNOWN_REASON"


def test_no_thread_ops_reports_no_updates_requested():
    backend, _ = _backend_with()
    response = (
        backend.docs_service()
        .documents()
        .batchUpdate(
            documentId="d1",
            body={
                "requests": [{"insertText": {"location": {"index": 1}, "text": "hi"}}]
            },
        )
        .execute()
    )
    assert response["commentUpdateState"] == "NO_UPDATES_REQUESTED"


# ---------------------------------------------------------------------------
# 5. Not-enrolled simulation vs the real classifier
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_preview_status():
    preview_status.reset()
    yield
    preview_status.reset()


def _unwrap(tool):
    """Unwrap a registered tool down to the undecorated implementation
    (same helper the gdocs_preview unit tests use)."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _capabilities(backend: FakeBackend) -> dict:
    from gdocs_preview import curated_tools

    fn = _unwrap(curated_tools.check_docs_review_capabilities)
    raw = asyncio.run(
        fn(
            backend.docs_service(),
            user_google_email="a@example.com",
            document_id="d1",
            probe=True,
        )
    )
    return json.loads(raw)


def test_not_enrolled_backend_classifies_as_unavailable():
    backend, _ = _backend_with()
    backend.not_enrolled = True
    report = _capabilities(backend)
    assert report["preview"]["availability"] == "unavailable"
    assert report["preview"]["evidence"]["reason"] == "not_enrolled"


def test_enrolled_backend_classifies_as_available():
    """The probe sends acceptSuggestion for a deliberately nonexistent id: an
    enrolled backend recognises the request type and fails only on the id, so
    the classifier must read that as ``available``."""
    backend, _ = _backend_with()
    report = _capabilities(backend)
    assert report["preview"]["availability"] == "available"
    assert report["preview"]["evidence"]["reason"] == "preview_request_type_recognized"


# ---------------------------------------------------------------------------
# 6. Style-split runs: the chunking prod does and the mock could not
# ---------------------------------------------------------------------------


def test_a_style_boundary_splits_a_suggestion_across_two_runs():
    """One suggested deletion, two deletion-marked ``textRun``s.

    Verified against the live API 2026-07-31 on ``Hello brave new world.``
    with "brave" bold: suggesting the deletion of "brave new" returned ONE
    created id and TWO runs --

        {"content": "brave", "textStyle": {"bold": true},
         "suggestedDeletionIds": ["suggest.grndhnkiya1d"]}
        {"content": " new",  "textStyle": {},
         "suggestedDeletionIds": ["suggest.grndhnkiya1d"]}

    The mock coalesced by mark set alone, so it could not build that payload
    and the whole "one run per suggestion" assumption class was invisible to
    every unit test and every llmux scenario.
    """
    doc = MockDoc(text="Hello brave new world.\n")
    doc.style_range(6, 11, "bold")  # "brave"
    sid = doc.delete(6, 15, "alice")  # "brave new", across the seam

    content = document_payload(doc)["body"]["content"]
    runs = [e["textRun"] for e in _elements(content)]
    struck = [r for r in runs if sid in (r.get("suggestedDeletionIds") or [])]

    assert [r["content"] for r in struck] == ["brave", " new"]
    assert struck[0]["textStyle"] == {"bold": True}
    assert struck[1]["textStyle"] == {}
    # Every character is still emitted exactly once, split or not.
    assert _content_text(content) == "Hello brave new world.\n"


def test_the_split_reaches_the_reviewer_view_as_two_marked_spans():
    """...which is why the base string is not in the rendered string.

    ``render_document`` wraps each run, so the reviewer sees
    ``{-brave-}{- new-}``: searching that for "brave new" finds nothing, and a
    verification that read "not found" as "the accept removed it" was
    fail-open on the destructive path (see
    ``gdocs_preview.analysis.check_resolution``).
    """
    doc = MockDoc(text="Hello brave new world.\n")
    doc.style_range(6, 11, "bold")
    doc.delete(6, 15, "alice")

    rendered = render_document(document_payload(doc))

    assert rendered["body_text"] == "Hello {-brave-}{- new-} world.\n"
    assert "brave new" not in rendered["body_text"]
    # The analysis layer is unaffected: it reads runs, not markers.
    (record,) = extract_suggestions_from_tabs([(None, document_payload(doc))])[
        "suggestions"
    ]
    assert record["pre_text"] == "brave new"
    assert record["post_text"] == ""


@SETTINGS
@given(doc=tabbed_docs())
def test_styling_never_changes_the_text_or_the_algebra(doc):
    """``style`` is chunking and nothing else: same characters, same records.

    Styling the first half of every segment must leave all three projections
    and every suggestion record byte-identical -- otherwise the seeding
    operation would be changing the thing it exists to observe.
    """
    before = tabs_document_payload(doc, "SUGGESTIONS_INLINE")
    before_records = extract_suggestions_from_tabs(
        [(t.tab_id, t.document) for t in tab_documents(before)]
    )["suggestions"]

    for segment in doc.ordered_segments():
        doc.style_range(0, len(segment.chars) // 2, "bold", segment=segment.key)

    after = tabs_document_payload(doc, "SUGGESTIONS_INLINE")
    after_records = extract_suggestions_from_tabs(
        [(t.tab_id, t.document) for t in tab_documents(after)]
    )["suggestions"]

    assert after_records == before_records
    for tab in tab_documents(after):
        assert _content_text(tab.document["body"]["content"]) == doc.segment_text(
            (tab.tab_id, None)
        )
