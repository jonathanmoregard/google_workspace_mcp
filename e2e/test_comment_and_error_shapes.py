"""The API's own grammar: comment anchoring, error strings, status enums.

Three long-open questions, resolved against the live API on 2026-08-01 and
encoded here so they stay resolved. Full transcripts and the reasoning:
``docs/findings/errors-and-discovery.md``.

1. **Unanchored ``insertComment``.** ``create_anchored_doc_comment`` requires
   a range and the docstring sends callers to the Drive path for
   document-level comments. Both halves are now tested: the API refuses an
   unanchored ``insertComment``, and the Drive-created comment really does
   come back through the Docs preview read as a thread with no anchor.
2. **The non-enrolled error classifier.** ``preview_status``'s markers decide
   "this caller is NOT enrolled" and had only ever been checked against
   ``mockdocs``' simulation. A second, non-enrolled GCP project is not
   available -- but the markers are about PROTO PARSING, not about
   enrollment, so deliberately bogus request types and field names reach the
   same code path from an enrolled caller. These tests hold the classifier to
   the strings the live API actually returns.
3. **The status enums.** The type names are not in any reachable discovery
   document (also asserted here); the VALUE sets are driven through every
   reachable transition instead.

**These tests talk to the Google API directly**, through a harness-side
``AuthorizedSession``, rather than through the MCP server. That is deliberate
and is the difference from ``test_preview_surface.py``: the subject is the
API's own wire grammar -- the thing ``preview_status``'s markers and the
tools' docstrings are written against -- and routing it through the server
would only re-test the server's error wrapping. Scratch documents still come
from the conftest fixtures, so teardown is unchanged.
"""

from __future__ import annotations

import json

import pytest
import requests
from google.auth.transport.requests import AuthorizedSession

from e2e.conftest import create_doc_via_mcp, new_scratch_title
from e2e.run_report import REPORT
from e2e.util import poll_until
from gdocs_preview.preview_status import (
    _PARSE_FAILURE_MARKERS,
    _UNKNOWN_FIELD_MARKERS,
    classify_preview_error,
)

BASE_TEXT = "Say the brave word today. And another sentence here.\n"

DOCS_URL = "https://docs.googleapis.com/v1/documents/{doc_id}"
DISCOVERY_URL = "https://docs.googleapis.com/$discovery/rest"

#: Every element of the Developer Preview surface ``gdocs_preview`` puts on
#: the wire. All six are FIELD NAMES, which is the whole bridge from "these
#: markers match real proto-parse errors" to "these markers would fire for a
#: non-enrolled caller" -- see the module docstring of ``preview_status``.
PREVIEW_REQUEST_TYPES = (
    "insertComment",
    "acceptSuggestion",
    "rejectSuggestion",
    "addCommentReply",
)


# ---------------------------------------------------------------------------
# Harness-side API access (NOT the server under test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api(ga_auth):
    """Raw authorized access to docs.googleapis.com.

    The preview surface is only half-reachable through googleapiclient
    anyway (``commentsViewMode`` is absent from discovery), and these tests
    need to send bodies the client would never build.
    """
    session = AuthorizedSession(ga_auth.credentials)
    yield session
    session.close()


def batch_update(api, doc_id: str, body: dict) -> tuple[int, dict]:
    response = api.post(
        DOCS_URL.format(doc_id=doc_id) + ":batchUpdate", json=body, timeout=60
    )
    return response.status_code, response.json()


def error_message(payload: dict) -> str:
    return ((payload or {}).get("error") or {}).get("message", "")


def read_threads(api, doc_id: str) -> dict:
    """The thread-bearing preview read (``preview_read``'s own query)."""
    response = api.get(
        DOCS_URL.format(doc_id=doc_id),
        params={
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
            "commentsViewMode": "COMMENTS_VIEW_MODE_INCLUDED",
            "includeTabsContent": "true",
        },
        timeout=60,
    )
    assert response.status_code == 200, response.text[:500]
    return response.json()


@pytest.fixture(scope="module")
def probe_doc(mcp, ga_auth, doc_tracker) -> str:
    """One scratch doc shared by the tests that only provoke FAILURES.

    Every request those tests send is rejected, so none of them can observe
    another's effect; a doc apiece would just be extra round trips.
    Registered with the session tracker, and trashed when the module is done
    whether or not a test failed.
    """
    title = new_scratch_title("-shapes")
    doc_id = create_doc_via_mcp(mcp, ga_auth.email, title, content=BASE_TEXT)
    doc_tracker.register(doc_id, title)
    yield doc_id
    doc_tracker.cleanup(doc_id)


# ---------------------------------------------------------------------------
# Q2: the proto-parse error grammar the classifier reads
# ---------------------------------------------------------------------------

#: label -> (request body, the field name the API should name back)
UNKNOWN_NAME_CASES = {
    "invented request type": (
        {"requests": [{"thisRequestTypeDoesNotExist": {}}]},
        "thisRequestTypeDoesNotExist",
    ),
    "preview request type in the wrong case": (
        {"requests": [{"insertcomment": {"content": "x"}}]},
        "insertcomment",
    ),
    "unknown sub-field of a GA request": (
        {
            "requests": [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": "x",
                        "bogusFieldXyz": 1,
                    }
                }
            ]
        },
        "bogusFieldXyz",
    ),
    "unknown top-level field of the batchUpdate body": (
        {"requests": [], "bogusTopLevelFieldXyz": 1},
        "bogusTopLevelFieldXyz",
    ),
    "unknown field inside writeControl": (
        {"requests": [], "writeControl": {"bogusXyz": 1}},
        "bogusXyz",
    ),
}


@pytest.mark.e2e_ga
@pytest.mark.parametrize("label", sorted(UNKNOWN_NAME_CASES))
def test_an_unknown_field_reads_as_not_enrolled(api, probe_doc, label):
    """The classifier's not-enrolled verdict, against the real grammar.

    Deliberately caller-independent: nothing here needs enrollment, because
    an unknown field name is rejected by the JSON->proto transcoder before
    any preview gate is consulted. That is precisely why this stands in for
    the non-enrolled project nobody has: it exercises the same code path.
    """
    body, expected_name = UNKNOWN_NAME_CASES[label]
    status, payload = batch_update(api, probe_doc, body)
    message = error_message(payload)
    REPORT.record_error_shape(f"unknown-name: {label}", status, message)

    assert status == 400, payload
    assert message.startswith("Invalid JSON payload received."), message
    assert f'Unknown name "{expected_name}"' in message, message
    assert "Cannot find field." in message, message
    assert classify_preview_error(status, message) == ("unavailable", "not_enrolled"), (
        message
    )


@pytest.mark.e2e_ga
def test_an_unknown_query_parameter_still_carries_two_markers(api, probe_doc):
    """The one unknown-name variant that drops "Cannot find field.".

    It is why the marker list has three entries and not one: the query
    parameter binder words its rejection differently, and only the first two
    markers survive.
    """
    response = api.get(
        DOCS_URL.format(doc_id=probe_doc),
        params={"bogusQueryParamXyz": "1"},
        timeout=60,
    )
    message = error_message(response.json())
    REPORT.record_error_shape("unknown-name: query parameter", 400, message)

    assert response.status_code == 400, message
    assert "Cannot find field." not in message, message
    assert "Cannot bind query parameter." in message, message
    hit = [m for m in _UNKNOWN_FIELD_MARKERS if m in message.lower()]
    assert sorted(hit) == ["invalid json payload", "unknown name"], hit


@pytest.mark.e2e_ga
def test_a_rejected_value_is_not_read_as_proof_of_enrollment(api, probe_doc):
    """The fail-open direction, closed.

    ``Invalid value at ...`` is the OTHER proto-parse failure family: the
    field name resolved, the value would not parse into its type. It carries
    none of the unknown-name markers, so it used to fall through to
    ``available`` -- a request the API never parsed, reported as evidence
    that the preview surface is reachable. It is not evidence the other way
    either, so the verdict is ``unknown``.
    """
    status, payload = batch_update(
        api,
        probe_doc,
        {"requests": [{"insertText": {"location": {"index": "abc"}, "text": "x"}}]},
    )
    message = error_message(payload)
    REPORT.record_error_shape("invalid-value: non-numeric index", status, message)

    assert status == 400, payload
    assert message.startswith("Invalid value at "), message
    assert not [m for m in _UNKNOWN_FIELD_MARKERS if m in message.lower()], message
    assert [m for m in _PARSE_FAILURE_MARKERS if m in message.lower()], message
    assert classify_preview_error(status, message) == ("unknown", "request_not_parsed")


@pytest.mark.e2e_ga
def test_a_semantic_400_uses_a_grammar_of_its_own(api, probe_doc):
    """The third grammar, and the reason the other two can be trusted.

    A request the API DID parse and then refused names the camelCase request
    and follows with prose. If the live API ever collapses these grammars
    into one, this fails -- which is the signal to revisit the markers.
    """
    status, payload = batch_update(
        api,
        probe_doc,
        {"requests": [{"insertText": {"location": {"index": 900_000}, "text": "x"}}]},
    )
    message = error_message(payload)
    REPORT.record_error_shape("semantic: index past the segment", status, message)

    assert status == 400, payload
    assert message.startswith("Invalid requests[0].insertText:"), message
    markers = _UNKNOWN_FIELD_MARKERS + _PARSE_FAILURE_MARKERS
    assert not [m for m in markers if m in message.lower()], message
    assert classify_preview_error(status, message) == (
        "available",
        "preview_request_type_recognized",
    )


# ---------------------------------------------------------------------------
# Q2/Q3: the public discovery document
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def public_discovery(ga_auth) -> dict:
    """The public Docs v1 discovery document.

    Depends on ``ga_auth`` even though the fetch is anonymous: ``pytest e2e``
    with no credentials must make NO network calls at all -- CI runs a bare
    ``uv run pytest`` from the repo root, which collects this directory, and
    ``tests/e2e_harness/test_no_creds_skip.py`` asserts that every e2e test
    skips there. A test that quietly reached out to Google anyway would pass
    in that run and break the contract the whole suite is gated on.
    """
    response = requests.get(DISCOVERY_URL, params={"version": "v1"}, timeout=60)
    assert response.status_code == 200, response.text[:500]
    return response.json()


@pytest.mark.e2e_ga
def test_every_preview_element_is_absent_from_the_public_discovery_document(
    public_discovery,
):
    """The bridge from "real parse errors" to "a non-enrolled caller".

    None of this package's preview elements is a mere enum VALUE on an
    otherwise-public field -- every one is a field NAME the public surface
    does not have. Under field-visibility gating that puts all of them in the
    unknown-name grammar, which the markers cover. If Google ever publishes
    one of these into the GA surface, this fails and the inference in
    ``preview_status``'s docstring has to be re-made.
    """
    schemas = public_discovery["schemas"]
    request_members = set(schemas["Request"]["properties"])
    assert not request_members & set(PREVIEW_REQUEST_TYPES), request_members

    assert set(schemas["WriteControl"]["properties"]) == {
        "requiredRevisionId",
        "targetRevisionId",
    }, "the public WriteControl gained a field; is writeMode public now?"

    get_params = set(
        public_discovery["resources"]["documents"]["methods"]["get"]["parameters"]
    )
    assert get_params == {
        "documentId",
        "includeTabsContent",
        "suggestionsViewMode",
    }, get_params

    for name in ("CommentThread", "SuggestionThread", "Post", "PostAuthor"):
        assert name not in schemas, name


@pytest.mark.e2e_ga
def test_no_preview_variant_of_the_discovery_document_is_reachable(public_discovery):
    """Q3, the negative half, stated as a test rather than as a claim.

    Every labelled variant Google's discovery service accepts answers with
    the SAME public document -- the label is ignored, not honoured -- and
    every plausible preview version is a 404. The preview types therefore
    have no discovery representation at all, which is why
    ``docs/preview-api-reference.md`` transcribes them by hand.
    """
    baseline = set(public_discovery["schemas"])

    for label in ("DEVELOPER_PREVIEW", "PREVIEW", "TRUSTED_TESTER"):
        response = requests.get(
            DISCOVERY_URL, params={"version": "v1", "labels": label}, timeout=60
        )
        assert response.status_code == 200, (label, response.text[:300])
        assert set(response.json()["schemas"]) == baseline, (
            f"labels={label} returned a DIFFERENT document -- a preview "
            "discovery variant may now exist; re-open UNCERTAIN item 2"
        )

    for version in ("v1preview", "v1beta", "v1alpha"):
        response = requests.get(DISCOVERY_URL, params={"version": version}, timeout=60)
        assert response.status_code == 404, (version, response.text[:300])


# ---------------------------------------------------------------------------
# Q1: anchoring is mandatory on insertComment
# ---------------------------------------------------------------------------

#: label -> (the InsertCommentRequest, the message the API answers with)
UNANCHORED_CASES = {
    "range omitted entirely": (
        {"content": "UNANCHORED"},
        "Insert comment requests must specify a range to anchor to.",
    ),
    "range present but empty": (
        {"content": "UNANCHORED", "range": {}},
        "Invalid range: must contain a start and end index",
    ),
    "range naming a tab but no indexes": (
        {"content": "UNANCHORED", "range": {"tabId": "t.0"}},
        "Invalid range: must contain a start and end index",
    ),
}


@pytest.mark.e2e_preview
@pytest.mark.parametrize("label", sorted(UNANCHORED_CASES))
def test_insert_comment_refuses_to_create_an_unanchored_comment(
    preview_ready, api, probe_doc, label
):
    """UNCERTAIN item 1, answered: the API requires the range.

    So ``create_anchored_doc_comment``'s mandatory ``start_index`` /
    ``end_index`` are not a self-imposed restriction to be relaxed -- they
    are the API's, and relaxing them would only move a 400 from the tool's
    own validation to a round trip. The docstring's redirect to
    ``manage_document_comment`` action ``create`` is the only unanchored
    path, and the next test shows it produces a first-class Docs thread.
    """
    insert_comment, expected = UNANCHORED_CASES[label]
    status, payload = batch_update(
        api, probe_doc, {"requests": [{"insertComment": insert_comment}]}
    )
    message = error_message(payload)
    REPORT.record_error_shape(f"unanchored insertComment: {label}", status, message)

    assert status == 400, payload
    assert message == f"Invalid requests[0].insertComment: {expected}", message
    # A SEMANTIC refusal: the preview request type parsed fine.
    assert classify_preview_error(status, message) == (
        "available",
        "preview_request_type_recognized",
    )


@pytest.mark.e2e_preview
def test_the_drive_path_makes_a_document_level_thread_the_docs_read_can_see(
    preview_ready, api, harness_drive, make_scratch_doc
):
    """The other half of the redirect: the Drive comment is a real thread.

    An unanchored Drive comment comes back through the Docs preview read as a
    ``CommentThread`` with an id, a head post, an author and a status -- and
    with ``anchorId`` and ``plainTextQuote`` ABSENT (proto3 omits them), which
    is exactly how "document-level" is represented. So the guidance costs a
    caller nothing on the read side: ``get_doc_review_view`` shows the
    comment either way.
    """
    doc_id = make_scratch_doc("-unanchored", content=BASE_TEXT)

    status, payload = batch_update(
        api,
        doc_id,
        {
            "requests": [
                {
                    "insertComment": {
                        "content": "ANCHORED",
                        "range": {"startIndex": 1, "endIndex": 4},
                    }
                }
            ]
        },
    )
    assert status == 200, payload
    anchored_id = payload["replies"][0]["insertComment"]["commentThread"]["commentId"]

    created = (
        harness_drive.comments()
        .create(fileId=doc_id, body={"content": "DOCUMENT-LEVEL"}, fields="*")
        .execute()
    )
    assert "anchor" not in created, created
    drive_id = created["id"]

    def both_visible():
        threads = {c["commentId"]: c for c in read_threads(api, doc_id)["comments"]}
        return threads if {anchored_id, drive_id} <= set(threads) else None

    threads = poll_until(
        both_visible, timeout=30, description="both comment threads in the Docs read"
    )

    anchored, unanchored = threads[anchored_id], threads[drive_id]
    assert anchored["anchorId"] and anchored["plainTextQuote"] == "Say"
    assert "anchorId" not in unanchored, unanchored
    assert "plainTextQuote" not in unanchored, unanchored

    # Everything else a review record needs is there for both.
    assert unanchored["status"] == "OPEN"
    assert unanchored["headPost"]["content"] == "DOCUMENT-LEVEL"
    assert unanchored["headPost"]["author"]["displayName"]


# ---------------------------------------------------------------------------
# Q3: the status enums, driven through every reachable transition
# ---------------------------------------------------------------------------


@pytest.mark.e2e_preview
def test_comment_thread_status_transitions(preview_ready, api, make_scratch_doc):
    """``CommentThread.status``: OPEN -> RESOLVED -> OPEN.

    Both non-default values of the enum ``docs/preview-api-reference.md``
    inlines by hand, observed live. ``STATUS_UNSPECIFIED`` is the proto3 zero
    value and is therefore omitted from JSON rather than emitted -- it is
    unobservable by construction, not merely unobserved.
    """
    doc_id = make_scratch_doc("-comment-status", content=BASE_TEXT)
    status, payload = batch_update(
        api,
        doc_id,
        {
            "requests": [
                {
                    "insertComment": {
                        "content": "C1",
                        "range": {"startIndex": 1, "endIndex": 4},
                    }
                }
            ]
        },
    )
    assert status == 200, payload
    thread = payload["replies"][0]["insertComment"]["commentThread"]
    comment_id = thread["commentId"]
    assert thread["status"] == "OPEN"

    def only_thread():
        (found,) = read_threads(api, doc_id)["comments"]
        return found

    assert only_thread()["status"] == "OPEN"

    for action, expected in (("RESOLVE", "RESOLVED"), ("REOPEN", "OPEN")):
        status, payload = batch_update(
            api,
            doc_id,
            {
                "requests": [
                    {
                        "addCommentReply": {
                            "commentId": comment_id,
                            "post": {
                                "content": action.lower(),
                                "commentAction": action,
                            },
                        }
                    }
                ]
            },
        )
        assert status == 200, payload
        assert payload["commentUpdateState"] == "ALL_SAVED", payload
        found = poll_until(
            lambda want=expected: (
                thread if (thread := only_thread())["status"] == want else None
            ),
            timeout=30,
            description=f"comment thread status {expected}",
        )
        assert found["replies"][-1]["commentAction"] == action, found


@pytest.mark.e2e_preview
def test_suggestion_thread_status_transitions(preview_ready, api, make_scratch_doc):
    """``SuggestionThread.status``: OPEN -> ACCEPTED and OPEN -> REJECTED.

    And the fact underneath it, which is easy to get backwards: a resolved
    suggestion thread does NOT leave ``suggestions[]``. It stays, restatused,
    with a reply carrying the ``suggestionAction``. The pending set the write
    tools verify against therefore cannot be "the length of ``suggestions``"
    -- it is derived from the body's suggestion marks, which is what
    ``gdocs_preview.analysis`` reads.

    Both transitions share one document on purpose. ``docs.googleapis.com``
    allows 60 write requests per minute per user and the e2e suite already
    runs into that ceiling; a test that answers two questions per scratch doc
    is a test the suite can afford to keep.
    """
    doc_id = make_scratch_doc("-suggestion-status", content=BASE_TEXT)
    created: dict[str, str] = {}
    for label, index in (("ACCEPT", 1), ("REJECT", 30)):
        status, payload = batch_update(
            api,
            doc_id,
            {
                "requests": [
                    {"insertText": {"location": {"index": index}, "text": f"{label} "}}
                ],
                "writeControl": {"writeMode": "SUGGEST"},
            },
        )
        assert status == 200, payload
        (created[label],) = payload["suggestionResponses"][0]["createdSuggestionIds"]
    assert len(set(created.values())) == 2, created

    def threads_by_id():
        return {
            s["suggestionId"]: s
            for s in (read_threads(api, doc_id).get("suggestions") or [])
        }

    listed = poll_until(
        lambda: (
            found if set(created.values()) <= set(found := threads_by_id()) else None
        ),
        timeout=30,
        description="both suggestion threads listed",
    )
    assert {listed[i]["status"] for i in created.values()} == {"OPEN"}, listed

    for request_type, label in (
        ("acceptSuggestion", "ACCEPT"),
        ("rejectSuggestion", "REJECT"),
    ):
        status, payload = batch_update(
            api,
            doc_id,
            {"requests": [{request_type: {"suggestionId": created[label]}}]},
        )
        assert status == 200, payload

    expected = {created["ACCEPT"]: "ACCEPTED", created["REJECT"]: "REJECTED"}
    resolved = poll_until(
        lambda: (
            found
            if all(
                (found := threads_by_id()).get(sid, {}).get("status") == want
                for sid, want in expected.items()
            )
            else None
        ),
        timeout=30,
        description="both suggestion threads restatused",
    )
    for label, action in (("ACCEPT", "ACCEPT"), ("REJECT", "REJECT")):
        thread = resolved[created[label]]
        assert thread["replies"][-1]["suggestionAction"] == action, thread


@pytest.mark.e2e_preview
def test_the_api_names_its_own_preview_proto_types(preview_ready, api, probe_doc):
    """Q3's consolation prize: the type names, from the API's own mouth.

    No discovery document carries the preview schemas (asserted above), but
    the JSON transcoder names the fully-qualified proto type whenever a value
    will not parse into a field. That is the only channel that answers
    UNCERTAIN item 2 at all -- and it reaches every type that appears in a
    REQUEST, which the two thread ``status`` enums do not: ``status`` is
    output-only on both threads, so no request can carry one and no error can
    name one. The names below are evidence; the two status enum type names
    remain genuinely unknown.

    Note the convention this establishes and its exception: an enum owned by
    one message is nested under it (``Post.CommentActionType``,
    ``WriteControl.WriteMode``) while a standalone request-parameter enum is
    top-level (``CommentsViewMode``). Both forms exist, so the status enums'
    names cannot be inferred from the pattern either.

    Four of the eight names this channel yields are asserted; the other four
    (``AcceptSuggestionRequest``, ``RejectSuggestionRequest``,
    ``AddCommentReplyRequest``, ``Post``) are transcribed in
    ``docs/findings/errors-and-discovery.md``. The suite is bounded by
    ``docs.googleapis.com``'s 60-writes-per-minute-per-user quota, and a
    rejected batchUpdate spends the same budget as a real one.
    """
    cases = {
        "google.apps.docs.v1.InsertCommentRequest": {
            "requests": [{"insertComment": "s"}]
        },
        "google.apps.docs.v1.Post.CommentActionType": {
            "requests": [
                {
                    "addCommentReply": {
                        "commentId": "c",
                        "post": {"content": "x", "commentAction": "BOGUS_XYZ"},
                    }
                }
            ]
        },
        "google.apps.docs.v1.Post.SuggestionActionType": {
            "requests": [
                {
                    "addCommentReply": {
                        "commentId": "c",
                        "post": {"content": "x", "suggestionAction": "BOGUS_XYZ"},
                    }
                }
            ]
        },
        "google.apps.docs.v1.WriteControl.WriteMode": {
            "requests": [],
            "writeControl": {"writeMode": "BOGUS_XYZ"},
        },
    }
    observed = {}
    for expected, body in cases.items():
        status, payload = batch_update(api, probe_doc, body)
        message = error_message(payload)
        observed[expected] = (status, message)
        assert status == 400, (expected, payload)
        assert f"(type.googleapis.com/{expected})" in message, (expected, message)

    REPORT.record_error_shape(
        "preview proto type names",
        400,
        json.dumps(sorted(observed), ensure_ascii=False),
    )

    # The read path names its enum the same way, and shows the other half of
    # the convention: standalone parameter enums are NOT nested.
    response = api.get(
        DOCS_URL.format(doc_id=probe_doc),
        params={"commentsViewMode": "BOGUS_XYZ", "includeTabsContent": "true"},
        timeout=60,
    )
    assert response.status_code == 400, response.text[:300]
    assert (
        "(type.googleapis.com/google.apps.docs.v1.CommentsViewMode)"
        in error_message(response.json())
    ), response.text[:300]


@pytest.mark.e2e_preview
def test_no_request_carries_a_thread_status_so_no_error_can_name_its_type(
    preview_ready, api, probe_doc
):
    """Why UNCERTAIN item 2 stays open, stated as evidence rather than prose.

    ``status`` is output-only on both thread kinds: every request field named
    ``status`` is rejected as an UNKNOWN NAME, not as an invalid value. The
    type-naming channel of the previous test is therefore closed for exactly
    the two enums it would be needed for. If a future preview revision makes
    ``status`` writable anywhere, this fails -- and the answer becomes
    reachable.

    ``InsertCommentRequest`` stands for all three request positions a status
    could plausibly occupy; ``AddCommentReplyRequest`` and its nested ``Post``
    answer identically (transcripts in
    ``docs/findings/errors-and-discovery.md``). One call, not three, because
    the suite is bounded by a 60-writes-per-minute quota.
    """
    status, payload = batch_update(
        api,
        probe_doc,
        {"requests": [{"insertComment": {"content": "x", "status": "OPEN"}}]},
    )
    message = error_message(payload)
    assert status == 400, payload
    assert 'Unknown name "status"' in message, message
    assert "(type.googleapis.com/" not in message, message
