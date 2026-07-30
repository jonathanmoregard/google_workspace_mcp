"""Property tests for the API adapter, including cross-validation against the
repo's real ``gdocs_preview.analysis``.

The adapter's job is unit conversion: the model counts grapheme clusters, the
Docs API counts UTF-16 code units. Three families of property here:

1. **Index discipline** -- every ``startIndex``/``endIndex`` the adapter emits
   slices the text it emits, under UTF-16, exactly.
2. **View-mode fidelity** -- each ``suggestionsViewMode`` returns its SPEC §3
   projection.
3. **Cross-validation** -- ``analysis.extract_suggestions`` run on the
   adapter's output must compute the same per-suggestion pre/post text the
   model computes from the char array. This checks the mock and
   ``analysis.py`` against one algebra: a disagreement means one of them is
   wrong, and neither gets to be the oracle.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from googleapiclient.errors import HttpError
from hypothesis import HealthCheck, given, settings

from gdocs_preview import preview_status
from gdocs_preview.analysis import extract_suggestions, render_document, utf16_len
from mockdocs.adapter import (
    PREVIEW_REQUEST_TYPES,
    SUGGEST_UNSUPPORTED_OFFICIAL,
    document_payload,
    to_grapheme_index,
    utf16_offsets,
)
from mockdocs.fake_services import FakeBackend
from mockdocs.model import MockDoc
from tests.mockdocs.strategies import suggestion_docs

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _elements(payload: dict) -> list[dict]:
    return [
        element
        for structural in payload["body"]["content"]
        if "paragraph" in structural
        for element in structural["paragraph"]["elements"]
    ]


def _payload_text(payload: dict) -> str:
    return "".join(e["textRun"]["content"] for e in _elements(payload))


# ---------------------------------------------------------------------------
# 1. Index discipline
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=suggestion_docs())
def test_emitted_indexes_slice_emitted_text_under_utf16(doc):
    """The defining property of the API boundary: indexes are UTF-16 code
    units into the document text, so slicing UTF-16 by them reproduces each
    run's own content. A code-point-based implementation fails this on the
    first emoji."""
    for mode in (
        "SUGGESTIONS_INLINE",
        "PREVIEW_WITHOUT_SUGGESTIONS",
        "PREVIEW_SUGGESTIONS_ACCEPTED",
    ):
        payload = document_payload(doc, mode)
        text = _payload_text(payload)
        units = text.encode("utf-16-le")
        for element in _elements(payload):
            start, end = element["startIndex"], element["endIndex"]
            content = element["textRun"]["content"]
            # Body text begins at index 1; two bytes per UTF-16 code unit.
            sliced = units[(start - 1) * 2 : (end - 1) * 2].decode("utf-16-le")
            assert sliced == content
            assert end - start == utf16_len(content)


@SETTINGS
@given(doc=suggestion_docs())
def test_indexes_are_contiguous_and_paragraphs_align(doc):
    payload = document_payload(doc, "SUGGESTIONS_INLINE")
    cursor = 1
    for structural in payload["body"]["content"]:
        if "paragraph" not in structural:
            continue
        assert structural["startIndex"] == cursor
        for element in structural["paragraph"]["elements"]:
            assert element["startIndex"] == cursor
            cursor = element["endIndex"]
        assert structural["endIndex"] == cursor


@SETTINGS
@given(doc=suggestion_docs())
def test_utf16_grapheme_index_round_trip(doc):
    offsets = utf16_offsets(doc.chars)
    for grapheme_index, utf16_index in enumerate(offsets):
        assert to_grapheme_index(doc.chars, utf16_index) == grapheme_index


@SETTINGS
@given(doc=suggestion_docs())
def test_indexes_inside_a_character_are_rejected(doc):
    """A tool that computed indexes with Python ``len()`` on an emoji-bearing
    document lands between the surrogates; the API rejects that and so must
    the mock."""
    from mockdocs.model import MockDocsError

    offsets = set(utf16_offsets(doc.chars))
    for candidate in range(1, max(offsets) + 1):
        if candidate in offsets:
            continue
        with pytest.raises(MockDocsError):
            to_grapheme_index(doc.chars, candidate)
        break


# ---------------------------------------------------------------------------
# 2. View-mode fidelity (SPEC §3)
# ---------------------------------------------------------------------------


@SETTINGS
@given(doc=suggestion_docs())
def test_view_modes_return_their_projections(doc):
    assert (
        _payload_text(document_payload(doc, "SUGGESTIONS_INLINE")) == doc.display_text()
    )
    assert (
        _payload_text(document_payload(doc, "PREVIEW_WITHOUT_SUGGESTIONS"))
        == doc.original_text()
    )
    assert (
        _payload_text(document_payload(doc, "PREVIEW_SUGGESTIONS_ACCEPTED"))
        == doc.final_text()
    )


@SETTINGS
@given(doc=suggestion_docs())
def test_clean_views_carry_no_suggestion_marks(doc):
    for mode in ("PREVIEW_WITHOUT_SUGGESTIONS", "PREVIEW_SUGGESTIONS_ACCEPTED"):
        payload = document_payload(doc, mode)
        for element in _elements(payload):
            assert "suggestedInsertionIds" not in element["textRun"]
            assert "suggestedDeletionIds" not in element["textRun"]
        rendered = render_document(payload)
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
    """
    lo, hi = doc.ranges()[sid]
    window = doc.chars[lo:hi]
    pre = "".join(c.cp for c in window if not c.ins)
    post = "".join(
        c.cp for c in window if (sid in c.ins) or (not c.ins and sid not in c.dels)
    )
    return pre, post


@SETTINGS
@given(doc=suggestion_docs())
def test_analysis_agrees_with_the_model(doc):
    """End-to-end: drive the REAL ``extract_suggestions`` over adapter output
    and require it to reproduce the model's per-suggestion pre/post text,
    span, type and author."""
    payload = document_payload(doc, "SUGGESTIONS_INLINE", me="alice")
    result = extract_suggestions(payload)

    assert result["document_id"] == doc.document_id
    assert result["suggestion_count"] == len(doc.registry)
    assert {r["suggestion_id"] for r in result["suggestions"]} == set(doc.registry)

    offsets = utf16_offsets(doc.chars)
    spans = doc.ranges()
    for record in result["suggestions"]:
        sid = record["suggestion_id"]
        pre, post = _model_pre_post(doc, sid)
        assert record["pre_text"] == pre, f"pre_text mismatch for {sid}"
        assert record["post_text"] == post, f"post_text mismatch for {sid}"

        lo, hi = spans[sid]
        assert record["start_index"] == offsets[lo]
        assert record["end_index"] == offsets[hi]
        # The reported index must be usable to address the model again.
        assert to_grapheme_index(doc.chars, record["start_index"]) == lo

        has_ins = any(sid in c.ins for c in doc.chars)
        has_del = any(sid in c.dels for c in doc.chars)
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


@SETTINGS
@given(doc=suggestion_docs())
def test_rendered_markers_match_the_render_states(doc):
    """``render_document``'s CriticMarkup output must agree with §4: every
    char whose render state involves an insertion mark appears inside
    ``{+...+}``, every purely struck char inside ``{-...-}``."""
    payload = document_payload(doc, "SUGGESTIONS_INLINE")
    rendered = render_document(payload)
    stripped = rendered["body_text"].replace("{+", "").replace("+}", "")
    stripped = stripped.replace("{-", "").replace("-}", "")
    assert stripped == doc.display_text()
    assert set(rendered["suggestion_ids"]) == set(doc.registry)


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


def test_suggest_mode_rejects_unsupported_request_types():
    backend, _ = _backend_with()
    docs = backend.docs_service()
    for kind in sorted(SUGGEST_UNSUPPORTED_OFFICIAL | PREVIEW_REQUEST_TYPES):
        with pytest.raises(HttpError) as exc:
            docs.documents().batchUpdate(
                documentId="d1",
                body={
                    "requests": [{kind: {}}],
                    "writeControl": {"writeMode": "SUGGEST"},
                },
            ).execute()
        assert exc.value.resp.status == 400


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
