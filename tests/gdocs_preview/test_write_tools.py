"""Behavioral tests for the native docs_preview write tools.

Mocked Google service objects only -- no network. Covers:
  - suggest_doc_edit: mode inference, exact batchUpdate payloads incl.
    writeControl and tabId/segmentId handling, created-id union
  - manage_document_suggestion: accept/reject payloads + id extraction
  - reply_to_doc_thread: thread-id union validation, payloads, post_id
  - create_anchored_doc_comment: range requirement, payloads, thread fields
  - shared helper: not-enrolled -> UserInputError, commentUpdateState
    enforcement, preview_status feeding (source="tool_call")
"""

import json
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdocs_preview import preview_status, write_tools

EMAIL = "reviewer@example.com"
DOC = "doc-fixture-1"
LINK = f"https://docs.google.com/document/d/{DOC}/edit"

NOT_ENROLLED_BODY = (
    b'{"error": {"message": "Invalid JSON payload received. '
    b'Unknown name \\"insertComment\\" at \'requests[0]\'"}}'
)
SEMANTIC_400_BODY = b'{"error": {"message": "Invalid suggestion id: nope"}}'


def _unwrap(tool):
    """Unwrap the decorated tool function to the original implementation."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _batch_service(response=None):
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        return_value={} if response is None else response
    )
    return service


def _failing_service(error):
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        side_effect=error
    )
    return service


def _http_error(status, content: bytes) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "mock"
    return HttpError(resp=resp, content=content)


def _batch_kwargs(service):
    _, kwargs = service.documents.return_value.batchUpdate.call_args
    return kwargs


@pytest.fixture(autouse=True)
def _reset_preview_status():
    preview_status.reset()
    yield
    preview_status.reset()


class TestSuggestDocEdit:
    @pytest.mark.asyncio
    async def test_insertion_payload_and_suggest_write_mode(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="hello",
            )
        )

        kwargs = _batch_kwargs(service)
        assert kwargs["documentId"] == DOC
        assert kwargs["body"]["writeControl"] == {"writeMode": "SUGGEST"}
        assert kwargs["body"]["requests"] == [
            {"insertText": {"location": {"index": 5}, "text": "hello"}}
        ]
        assert result["mode"] == "insertion"
        assert result["requests_applied"] == 1
        assert result["link"] == LINK

    @pytest.mark.asyncio
    async def test_deletion_payload(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                end_index=9,
            )
        )

        assert _batch_kwargs(service)["body"]["requests"] == [
            {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 9}}}
        ]
        assert result["mode"] == "deletion"
        assert result["requests_applied"] == 1

    @pytest.mark.asyncio
    async def test_replacement_is_delete_then_insert_at_start(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                end_index=9,
                text="new",
            )
        )

        assert _batch_kwargs(service)["body"]["requests"] == [
            {"deleteContentRange": {"range": {"startIndex": 5, "endIndex": 9}}},
            {"insertText": {"location": {"index": 5}, "text": "new"}},
        ]
        assert result["mode"] == "replacement"
        assert result["requests_applied"] == 2

    @pytest.mark.asyncio
    async def test_segment_and_tab_ids_included_only_when_set(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        await fn(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            start_index=5,
            end_index=9,
            text="new",
            tab_id="tab-1",
            segment_id="kix.seg1",
        )

        requests = _batch_kwargs(service)["body"]["requests"]
        assert requests[0]["deleteContentRange"]["range"] == {
            "startIndex": 5,
            "endIndex": 9,
            "segmentId": "kix.seg1",
            "tabId": "tab-1",
        }
        assert requests[1]["insertText"]["location"] == {
            "index": 5,
            "segmentId": "kix.seg1",
            "tabId": "tab-1",
        }

    @pytest.mark.asyncio
    async def test_neither_text_nor_end_index_rejected(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(UserInputError, match="Provide text"):
            await fn(service, user_google_email=EMAIL, document_id=DOC, start_index=5)
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_index_must_exceed_start_index(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(UserInputError, match="end_index"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                end_index=5,
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_index_below_one_rejected_no_aliasing(self):
        """Deliberate deviation from modify_doc_text: no index-0 remapping."""
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(UserInputError, match="list_document_suggestions"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=0,
                text="hello",
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_created_suggestion_ids_union_deduplicated(self):
        service = _batch_service(
            {
                "suggestionResponses": [
                    {"createdSuggestionIds": ["s1"]},
                    {"createdSuggestionIds": ["s1", "s2"]},
                ]
            }
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                end_index=9,
                text="new",
            )
        )
        assert result["created_suggestion_ids"] == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_missing_suggestion_responses_yields_empty_ids(self):
        """Ids are never fabricated when the API omits the field."""
        service = _batch_service({})
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="hello",
            )
        )
        assert result["created_suggestion_ids"] == []


class TestManageDocumentSuggestion:
    @pytest.mark.asyncio
    async def test_accept_payload_without_write_control(self):
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["s1"]}]}
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="s1",
            )
        )

        body = _batch_kwargs(service)["body"]
        assert body["requests"] == [{"acceptSuggestion": {"suggestionId": "s1"}}]
        assert "writeControl" not in body
        assert result["action"] == "accept"
        assert result["accepted_suggestion_ids"] == ["s1"]
        assert result["link"] == LINK

    @pytest.mark.asyncio
    async def test_reject_payload_and_action_normalization(self):
        service = _batch_service(
            {"suggestionResponses": [{"rejectedSuggestionIds": ["s2"]}]}
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action=" Reject ",
                suggestion_id="s2",
            )
        )

        assert _batch_kwargs(service)["body"]["requests"] == [
            {"rejectSuggestion": {"suggestionId": "s2"}}
        ]
        assert result["action"] == "reject"
        assert result["rejected_suggestion_ids"] == ["s2"]

    @pytest.mark.asyncio
    async def test_invalid_action_rejected(self):
        service = _batch_service()
        fn = _unwrap(write_tools.manage_document_suggestion)

        with pytest.raises(UserInputError, match="Must be 'accept' or 'reject'"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="delete",
                suggestion_id="s1",
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_comment_update_state_reported_when_sent(self):
        """A nonexistent id can be a 200 no-op carrying commentUpdateState."""
        service = _batch_service({"commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"})
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="no-such",
            )
        )
        assert result["comment_update_state"] == "ALL_FAILED_UNKNOWN_REASON"
        assert result["accepted_suggestion_ids"] == []

    @pytest.mark.asyncio
    async def test_comment_update_state_omitted_when_absent(self):
        service = _batch_service({})
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="s1",
            )
        )
        assert "comment_update_state" not in result


class TestReplyToDocThread:
    @pytest.mark.asyncio
    async def test_comment_thread_payload(self):
        service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        fn = _unwrap(write_tools.reply_to_doc_thread)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Agreed.",
                comment_id="c1",
            )
        )

        assert _batch_kwargs(service)["body"]["requests"] == [
            {
                "addCommentReply": {
                    "post": {"content": "Agreed."},
                    "commentId": "c1",
                }
            }
        ]
        assert result["thread_type"] == "comment"
        assert result["comment_id"] == "c1"
        assert "suggestion_id" not in result
        assert result["comment_update_state"] == "ALL_SAVED"

    @pytest.mark.asyncio
    async def test_suggestion_thread_payload(self):
        service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        fn = _unwrap(write_tools.reply_to_doc_thread)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Why this change?",
                suggestion_id="s1",
            )
        )

        assert _batch_kwargs(service)["body"]["requests"] == [
            {
                "addCommentReply": {
                    "post": {"content": "Why this change?"},
                    "suggestionId": "s1",
                }
            }
        ]
        assert result["thread_type"] == "suggestion"
        assert result["suggestion_id"] == "s1"
        assert "comment_id" not in result

    @pytest.mark.asyncio
    async def test_exactly_one_thread_id_required(self):
        service = _batch_service()
        fn = _unwrap(write_tools.reply_to_doc_thread)

        with pytest.raises(UserInputError, match="exactly one"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
            )
        with pytest.raises(UserInputError, match="exactly one"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
                comment_id="c1",
                suggestion_id="s1",
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_reply_content_rejected(self):
        service = _batch_service()
        fn = _unwrap(write_tools.reply_to_doc_thread)

        with pytest.raises(UserInputError, match="reply_content"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="   ",
                comment_id="c1",
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_failed_comment_update_state_raises(self):
        """HTTP 200 + ALL_FAILED_UNKNOWN_REASON must never look like success."""
        service = _batch_service({"commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"})
        fn = _unwrap(write_tools.reply_to_doc_thread)

        with pytest.raises(Exception, match="NOT saved"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
                comment_id="c1",
            )

    @pytest.mark.asyncio
    async def test_post_id_extracted_when_present_else_null(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [{"addCommentReply": {"post": {"postId": "p9"}}}],
            }
        )
        fn = _unwrap(write_tools.reply_to_doc_thread)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
                comment_id="c1",
            )
        )
        assert result["post_id"] == "p9"

        bare_service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        bare = json.loads(
            await fn(
                bare_service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
                comment_id="c1",
            )
        )
        assert bare["post_id"] is None


class TestCreateAnchoredDocComment:
    @pytest.mark.asyncio
    async def test_insert_comment_payload_with_required_range(self):
        service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Needs a citation.",
                start_index=5,
                end_index=12,
            )
        )

        body = _batch_kwargs(service)["body"]
        assert body["requests"] == [
            {
                "insertComment": {
                    "content": "Needs a citation.",
                    "range": {"startIndex": 5, "endIndex": 12},
                }
            }
        ]
        assert "writeControl" not in body
        assert result["comment_update_state"] == "ALL_SAVED"
        assert result["link"] == LINK

    @pytest.mark.asyncio
    async def test_assignee_and_segment_tab_included_when_set(self):
        service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        await fn(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            content="Please fix.",
            start_index=5,
            end_index=12,
            segment_id="kix.seg1",
            tab_id="tab-1",
            assignee_email="editor@example.com",
        )

        request = _batch_kwargs(service)["body"]["requests"][0]["insertComment"]
        assert request["assigneeEmailAddress"] == "editor@example.com"
        assert request["range"] == {
            "startIndex": 5,
            "endIndex": 12,
            "segmentId": "kix.seg1",
            "tabId": "tab-1",
        }

    @pytest.mark.asyncio
    async def test_empty_content_rejected(self):
        service = _batch_service()
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        with pytest.raises(UserInputError, match="content"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="  ",
                start_index=5,
                end_index=12,
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_range_rejected(self):
        service = _batch_service()
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        with pytest.raises(UserInputError, match="start_index"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Hi",
                start_index=0,
                end_index=12,
            )
        with pytest.raises(UserInputError, match="end_index"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Hi",
                start_index=5,
                end_index=5,
            )
        service.documents.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_fields_extracted_when_present_else_null(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "insertComment": {
                            "commentThread": {
                                "commentId": "c7",
                                "anchorId": "a1",
                                "plainTextQuote": "quoted words",
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
                content="Hi",
                start_index=5,
                end_index=12,
            )
        )
        assert result["comment_id"] == "c7"
        assert result["anchor_id"] == "a1"
        assert result["quoted_text"] == "quoted words"

        bare_service = _batch_service({"commentUpdateState": "ALL_SAVED"})
        bare = json.loads(
            await fn(
                bare_service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Hi",
                start_index=5,
                end_index=12,
            )
        )
        assert bare["comment_id"] is None
        assert bare["anchor_id"] is None
        assert bare["quoted_text"] is None

    @pytest.mark.asyncio
    async def test_all_failed_comment_update_state_raises(self):
        service = _batch_service({"commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"})
        fn = _unwrap(write_tools.create_anchored_doc_comment)

        with pytest.raises(Exception, match="NOT saved"):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Hi",
                start_index=5,
                end_index=12,
            )


class TestPreviewGating:
    """Not-enrolled classification and preview_status feeding through the
    shared helper (exercised via suggest_doc_edit; the helper is the single
    choke point for all four write tools)."""

    @pytest.mark.asyncio
    async def test_not_enrolled_400_raises_actionable_user_input_error(self):
        service = _failing_service(_http_error(400, NOT_ENROLLED_BODY))
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(UserInputError) as excinfo:
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="hello",
            )
        message = str(excinfo.value)
        assert "Developer Preview" in message
        assert "check_docs_review_capabilities" in message
        assert "suggest_doc_edit" in message

        status = preview_status.get_status()
        assert status["availability"] == "unavailable"
        assert status["evidence"]["reason"] == "not_enrolled"
        assert status["source"] == "tool_call"

    @pytest.mark.asyncio
    async def test_semantic_400_reraises_http_error_and_records_available(self):
        service = _failing_service(_http_error(400, SEMANTIC_400_BODY))
        fn = _unwrap(write_tools.manage_document_suggestion)

        with pytest.raises(HttpError):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="nope",
            )

        status = preview_status.get_status()
        assert status["availability"] == "available"
        assert status["evidence"]["reason"] == "preview_request_type_recognized"
        assert status["source"] == "tool_call"

    @pytest.mark.asyncio
    async def test_403_reraises_and_records_unknown(self):
        service = _failing_service(
            _http_error(403, b'{"error": {"message": "no permission"}}')
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(HttpError):
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="hello",
            )

        status = preview_status.get_status()
        assert status["availability"] == "unknown"
        assert status["evidence"]["reason"] == "permission_or_scope"

    @pytest.mark.asyncio
    async def test_success_records_available_from_tool_call(self):
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        await fn(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            start_index=5,
            text="hello",
        )

        status = preview_status.get_status()
        assert status["availability"] == "available"
        assert status["evidence"]["reason"] == "preview_request_succeeded"
        assert status["source"] == "tool_call"
