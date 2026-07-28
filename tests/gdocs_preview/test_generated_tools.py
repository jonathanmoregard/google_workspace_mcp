"""Behavioral tests for generated docs_preview tools.

One representative per generated-tool category, with mocked Google service
objects verifying exact API payload construction:
  - plain resource method (Drive comments.list / comments.delete)
  - GA batchUpdate Request-union member (insertText), incl. write_mode plumbing
  - Developer Preview overlay member (acceptSuggestion, insertComment)
"""

import inspect
import json
from unittest.mock import Mock

import pytest

from gdocs_preview.generated import (
    docs_batch_update,
    docs_methods,
    docs_preview,
    drive_comments,
)


def _unwrap(tool):
    """Unwrap the decorated tool function to the original implementation."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _docs_service(response):
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        return_value=response
    )
    return service


EMAIL = "reviewer@example.com"


class TestPlainMethodTools:
    @pytest.mark.asyncio
    async def test_comments_list_builds_request_kwargs(self):
        service = Mock()
        response = {"comments": [{"id": "c1", "author": {"displayName": "Alice"}}]}
        service.comments.return_value.list.return_value.execute = Mock(
            return_value=response
        )

        fn = _unwrap(drive_comments.drive_api_comments_list)
        result = await fn(
            service,
            user_google_email=EMAIL,
            file_id="file-1",
            page_size=5,
        )

        service.comments.return_value.list.assert_called_once_with(
            fileId="file-1", pageSize=5, fields="*"
        )
        assert json.loads(result) == response

    @pytest.mark.asyncio
    async def test_comments_delete_handles_empty_response(self):
        service = Mock()
        service.comments.return_value.delete.return_value.execute = Mock(
            return_value=""
        )

        fn = _unwrap(drive_comments.drive_api_comments_delete)
        result = await fn(
            service,
            user_google_email=EMAIL,
            file_id="file-1",
            comment_id="c1",
        )

        service.comments.return_value.delete.assert_called_once_with(
            fileId="file-1", commentId="c1", fields="*"
        )
        assert "empty response" in result

    @pytest.mark.asyncio
    async def test_documents_get_passes_view_mode(self):
        service = Mock()
        service.documents.return_value.get.return_value.execute = Mock(
            return_value={"documentId": "d1", "title": "T"}
        )

        fn = _unwrap(docs_methods.docs_api_documents_get)
        await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            suggestions_view_mode="SUGGESTIONS_INLINE",
        )

        service.documents.return_value.get.assert_called_once_with(
            documentId="d1", suggestionsViewMode="SUGGESTIONS_INLINE"
        )


class TestBatchUpdateMemberTools:
    @pytest.mark.asyncio
    async def test_insert_text_wraps_single_request(self):
        service = _docs_service({"documentId": "d1", "replies": [{}]})

        fn = _unwrap(docs_batch_update.docs_api_insert_text)
        result = await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            text="hello",
            location={"index": 1},
        )

        service.documents.return_value.batchUpdate.assert_called_once_with(
            documentId="d1",
            body={
                "requests": [
                    {"insertText": {"location": {"index": 1}, "text": "hello"}}
                ]
            },
        )
        assert json.loads(result)["documentId"] == "d1"

    @pytest.mark.asyncio
    async def test_write_mode_suggest_plumbs_into_write_control(self):
        service = _docs_service({"documentId": "d1"})

        fn = _unwrap(docs_batch_update.docs_api_insert_text)
        await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            text="hello",
            location={"index": 1},
            write_mode="SUGGEST",
        )

        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        assert body["writeControl"] == {"writeMode": "SUGGEST"}
        assert body["requests"] == [
            {"insertText": {"location": {"index": 1}, "text": "hello"}}
        ]

    @pytest.mark.asyncio
    async def test_omitted_write_mode_sends_no_write_control(self):
        service = _docs_service({"documentId": "d1"})

        fn = _unwrap(docs_batch_update.docs_api_insert_text)
        await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            text="hello",
        )

        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        assert "writeControl" not in body

    def test_suggest_unsupported_members_lack_write_mode_param(self):
        # From the official unsupported list (suggestions how-to).
        for tool, expected in (
            (docs_batch_update.docs_api_create_named_range, False),
            (docs_batch_update.docs_api_delete_header, False),
            (docs_batch_update.docs_api_insert_text, True),
            (docs_batch_update.docs_api_replace_all_text, True),
        ):
            params = inspect.signature(_unwrap(tool)).parameters
            assert ("write_mode" in params) is expected, tool


class TestPreviewOverlayTools:
    @pytest.mark.asyncio
    async def test_accept_suggestion_payload(self):
        service = _docs_service({"documentId": "d1", "commentUpdateState": "ALL_SAVED"})

        fn = _unwrap(docs_preview.docs_api_accept_suggestion)
        result = await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            suggestion_id="sugg.1",
        )

        service.documents.return_value.batchUpdate.assert_called_once_with(
            documentId="d1",
            body={"requests": [{"acceptSuggestion": {"suggestionId": "sugg.1"}}]},
        )
        assert json.loads(result)["commentUpdateState"] == "ALL_SAVED"

    @pytest.mark.asyncio
    async def test_insert_comment_payload_with_range_anchor(self):
        service = _docs_service({"documentId": "d1"})

        fn = _unwrap(docs_preview.docs_api_insert_comment)
        await fn(
            service,
            user_google_email=EMAIL,
            document_id="d1",
            content="Needs a citation.",
            range={"startIndex": 5, "endIndex": 12},
        )

        service.documents.return_value.batchUpdate.assert_called_once_with(
            documentId="d1",
            body={
                "requests": [
                    {
                        "insertComment": {
                            "content": "Needs a citation.",
                            "range": {"startIndex": 5, "endIndex": 12},
                        }
                    }
                ]
            },
        )

    def test_all_preview_tools_carry_preview_marker(self):
        manifest_preview = []
        for name in dir(docs_preview):
            if not name.startswith("docs_api_"):
                continue
            tool = getattr(docs_preview, name)
            manifest_preview.append(name)
            doc = _unwrap(tool).__doc__ or ""
            assert "[DEVELOPER PREVIEW]" in doc, name
            assert "Developer Preview" in doc, name
        assert sorted(manifest_preview) == [
            "docs_api_accept_suggestion",
            "docs_api_add_comment_reply",
            "docs_api_delete_comment",
            "docs_api_delete_comment_reply",
            "docs_api_delete_suggestion",
            "docs_api_insert_comment",
            "docs_api_reject_suggestion",
            "docs_api_update_comment_post",
        ]

    def test_preview_tools_have_no_write_mode(self):
        for name in (
            "docs_api_accept_suggestion",
            "docs_api_insert_comment",
            "docs_api_add_comment_reply",
        ):
            fn = _unwrap(getattr(docs_preview, name))
            assert "write_mode" not in inspect.signature(fn).parameters, name

    def test_ga_tools_carry_ga_marker(self):
        doc = _unwrap(docs_batch_update.docs_api_insert_text).__doc__ or ""
        assert doc.startswith("[GA]")
        doc = _unwrap(drive_comments.drive_api_comments_list).__doc__ or ""
        assert doc.startswith("[GA]")
