"""Behavioral tests for the curated docs_preview review tools.

Mocked Google service objects only -- no network. Covers:
  - list_document_suggestions: preview (tabs + threads) read plumbing,
    author/status/summary passthrough, and the GA fallback
  - get_doc_review_view: view-mode validation, rendered output, comments
  - check_docs_review_capabilities: probe error-shape classification +
    process cache semantics (probe=False must never touch the API)
"""

import json
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdocs_preview import curated_tools, preview_read, preview_status
from tests.gdocs_preview import fixtures as fx

EMAIL = "reviewer@example.com"


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
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
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
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
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
    async def test_multi_tab_records_carry_their_tab_id(self):
        service = _docs_get_service(fx.TABS_PAYLOAD_MULTI)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
        )

        assert [(s["suggestion_id"], s["tab_id"]) for s in result["suggestions"]] == [
            ("suggest.ins1", "t.0"),
            ("suggest.tab2", "t.second"),
        ]

    @pytest.mark.asyncio
    async def test_falls_back_to_the_ga_read_when_not_enrolled(self):
        """Enrollment is a property of the caller's project, not the
        document: a preview-read failure must degrade, never fail."""
        service = _ga_only_service(fx.DOC_PLAIN_INSERTION)
        fn = _unwrap(curated_tools.list_document_suggestions)

        result = json.loads(
            await fn(service, user_google_email=EMAIL, document_id="doc-fixture-1")
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
        assert result["body_text"] == "Hello{+ brave+} world.\n"
        assert result["read_source"] == curated_tools.READ_SOURCE_PREVIEW

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
