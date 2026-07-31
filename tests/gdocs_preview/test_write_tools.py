"""Behavioral tests for the native docs_preview write tools.

Mocked Google service objects only -- no network. Covers:
  - suggest_doc_edit: mode inference, exact batchUpdate payloads incl.
    writeControl and tabId/segmentId handling, created-id union
  - manage_document_suggestion: accept/reject payloads + id extraction
  - reply_to_doc_thread: thread-id union validation, payloads, post_id
  - create_anchored_doc_comment: range requirement, payloads, thread fields
  - shared helper: not-enrolled -> UserInputError, commentUpdateState
    enforcement, preview_status feeding (source="tool_call")
  - the post-write verification echo of every tool, incl. the free
    (batchUpdate-response) and the one-extra-read variants, and the rule
    that a failed verification never fails a landed write
  - collateral-GC detection and the three "that id is gone" error branches
    (proven cause / may have been removed / never seen)
"""

import json
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdocs_preview import (
    address,
    preview_read,
    preview_status,
    review_page,
    suggestion_ledger,
    write_tools,
)
from mockdocs.concurrency import Interference, Trigger, apply_interference
from mockdocs.fake_services import FakeBackend
from tests.gdocs_preview import fixtures as fx

EMAIL = "reviewer@example.com"
DOC = "doc-fixture-1"
LINK = f"https://docs.google.com/document/d/{DOC}/edit"

NOT_ENROLLED_BODY = (
    b'{"error": {"message": "Invalid JSON payload received. '
    b'Unknown name \\"insertComment\\" at \'requests[0]\'"}}'
)
#: A semantic 400 that is NOT about a missing suggestion, so the tools must
#: let it through to handle_http_errors untouched.
SEMANTIC_400_BODY = b'{"error": {"message": "Invalid requests[0]: bad range"}}'


def missing_suggestion_400(suggestion_id: str = "sug.bob.1") -> bytes:
    """The mock backend's missing-suggestion body (mockdocs/adapter.py)."""
    return (
        '{"error": {"message": "Invalid requests[0].acceptSuggestion: the '
        f"suggestion ID {suggestion_id} is invalid or the suggestion no "
        'longer exists."}}'
    ).encode()


def missing_suggestion_404(suggestion_id: str = "sug.bob.1") -> bytes:
    """The REAL API's shape (e2e/last_run.md, 2026-07-30): a 404, not a 400."""
    return (
        '{"error": {"message": "Suggestion with ID '
        f'{suggestion_id} does not exist."}}'
    ).encode()


def thread_for(
    suggestion_id: str, *, me: bool = True, display_name: str = "Reviewer"
) -> dict:
    """The SuggestionThread a preview read carries for one pending card.

    A preview read of a document with a pending suggestion ALWAYS carries its
    thread, and the thread's ``author.me`` is the only evidence of authorship
    the API offers -- which is what keeps a second reviewer's card from being
    echoed as one this call created.
    """
    return {
        "suggestionId": suggestion_id,
        "headPost": {
            "postId": f"{suggestion_id}.head",
            "author": {"displayName": display_name, "me": me, "user": "users/1"},
            "createTime": "2026-07-30T10:00:00.000Z",
            "updateTime": "2026-07-30T10:00:00.000Z",
        },
        "status": "OPEN",
        "summaryText": f"Add: “{suggestion_id}”",
    }


#: Verification read payload with no pending suggestions -- the default for
#: tests that are not about verification.
EMPTY_READ = fx.build_tabs_payload([("t.0", fx.DOC_EMPTY)])
#: One pending replacement ("morning" -> "evening"), id ``suggest.rep1``,
#: with the thread the preview read really returns beside it.
REPLACEMENT_READ = fx.build_tabs_payload(
    [("t.0", fx.DOC_REPLACEMENT)], suggestions=[thread_for("suggest.rep1")]
)


def _unwrap(tool):
    """Unwrap the decorated tool function to the original implementation."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _batch_service(response=None, document=None):
    """Service whose batchUpdate returns ``response``.

    ``documents().get()`` answers the post-write verification read with
    ``document`` (default: a document with no pending suggestions), so tests
    that do not care about verification still get a payload the analysis
    layer can walk instead of a bare ``Mock``.
    """
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        return_value={} if response is None else response
    )
    service.documents.return_value.get.return_value.execute = Mock(
        return_value=EMPTY_READ if document is None else document
    )
    return service


def _failing_service(error):
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        side_effect=error
    )
    service.documents.return_value.get.return_value.execute = Mock(
        return_value=EMPTY_READ
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


def _observe(*suggestions):
    """Seed the ledger the way a list_document_suggestions call would."""
    suggestion_ledger.observe(EMAIL, DOC, list(suggestions))


@pytest.fixture(autouse=True)
def _reset_preview_status():
    preview_status.reset()
    suggestion_ledger.reset()
    yield
    preview_status.reset()
    suggestion_ledger.reset()


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

    @pytest.mark.asyncio
    async def test_author_of_the_new_reply_is_reported(self):
        """Real response shape (verified 2026-07-30): the AddCommentReply
        response carries the whole Post, author included -- so the tool can
        report who authored the reply without a follow-up read."""
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "addCommentReply": {
                            "post": {
                                "postId": "AAACEhLh9j8",
                                "content": "c-reply",
                                "contentHtml": "c-reply",
                                "author": {
                                    "displayName": "Jonathan Moregård",
                                    "me": True,
                                    "user": "users/108544169371250993163",
                                },
                                "createTime": "2026-07-30T18:54:32.741Z",
                                "updateTime": "2026-07-30T18:54:32.741Z",
                                "commentAction": "NO_COMMENT_ACTION_CHANGE",
                            }
                        }
                    }
                ],
            }
        )
        fn = _unwrap(write_tools.reply_to_doc_thread)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="c-reply",
                comment_id="c1",
            )
        )
        assert result["author"] == {
            "display_name": "Jonathan Moregård",
            "me": True,
            "anonymous": None,
            "user": "users/108544169371250993163",
        }
        assert result["create_time"] == "2026-07-30T18:54:32.741Z"

    @pytest.mark.asyncio
    async def test_author_is_null_when_the_response_omits_the_post(self):
        service = _batch_service({"commentUpdateState": "ALL_SAVED"})
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
        assert result["author"] is None


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
        assert bare["author"] is None
        assert bare["post_id"] is None

    @pytest.mark.asyncio
    async def test_head_post_author_is_reported(self):
        """Real response shape (verified 2026-07-30): InsertCommentResponse
        carries the whole CommentThread, headPost.author included."""
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "insertComment": {
                            "commentThread": {
                                "commentId": "AAACEfTspmk",
                                "anchorId": "kix.5jicnobkgd9j",
                                "headPost": {
                                    "postId": "AAACEfTspmk",
                                    "content": "probe comment",
                                    "contentHtml": "probe comment",
                                    "author": {
                                        "displayName": "Jonathan Moregård",
                                        "me": True,
                                        "user": "users/108544169371250993163",
                                    },
                                    "createTime": "2026-07-30T18:51:48.198Z",
                                    "updateTime": "2026-07-30T18:51:48.198Z",
                                    "commentAction": "NO_COMMENT_ACTION_CHANGE",
                                },
                                "status": "OPEN",
                                "plainTextQuote": "Say",
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
                content="probe comment",
                start_index=1,
                end_index=4,
            )
        )
        assert result["comment_id"] == "AAACEfTspmk"
        assert result["post_id"] == "AAACEfTspmk"
        assert result["status"] == "OPEN"
        assert result["author"] == {
            "display_name": "Jonathan Moregård",
            "me": True,
            "anonymous": None,
            "user": "users/108544169371250993163",
        }
        assert result["create_time"] == "2026-07-30T18:51:48.198Z"

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
        """A semantic 400 that is not about a missing suggestion stays an
        HttpError for handle_http_errors to wrap."""
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
        """verify=False so the write's own evidence is the last recorded:
        the verification read is itself a preview read and would otherwise
        overwrite the reason with ``preview_read_succeeded``."""
        service = _batch_service()
        fn = _unwrap(write_tools.suggest_doc_edit)

        await fn(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            start_index=5,
            text="hello",
            verify=False,
        )

        status = preview_status.get_status()
        assert status["availability"] == "available"
        assert status["evidence"]["reason"] == "preview_request_succeeded"
        assert status["source"] == "tool_call"


# ---------------------------------------------------------------------------
# Fix 1 -- every write echoes a verifiable post-state
# ---------------------------------------------------------------------------

#: The replacement fixture after its suggestion was accepted.
ACCEPTED_READ = fx.build_tabs_payload(
    [("t.0", fx.build_doc([fx.paragraph(fx.run("Good evening\n"))]))]
)
#: The analysis record of ``suggest.rep1`` as a listing would report it --
#: address included, because that is what a listing reports.
REP1_RECORD = {
    "suggestion_id": "suggest.rep1",
    "type": "replacement",
    "pre_text": "morning",
    "post_text": "evening",
    "context_before": "Good ",
    "context_after": "\n",
    "segment": "body",
    "segment_id": None,
    "tab_id": "t.0",
    "start_index": 6,
    "end_index": 20,
    "summary_text": "Replace: “morning” with “evening”",
    "status": "OPEN",
}


def _get_calls(service):
    return service.documents.return_value.get.call_args_list


class TestSuggestDocEditVerification:
    """suggest_doc_edit must answer "did my edit do what I meant" inline."""

    @pytest.mark.asyncio
    async def test_created_suggestion_is_echoed_with_pre_post_and_context(self):
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["suggest.rep1"]}]},
            document=REPLACEMENT_READ,
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                end_index=13,
                text="evening",
            )
        )

        verification = result["verification"]
        assert verification["source"] == "post_write_read"
        assert verification["read_source"] == "preview_threads"
        assert verification["pending_suggestion_count"] == 1
        (echo,) = verification["created_suggestions"]
        assert echo["suggestion_id"] == "suggest.rep1"
        assert echo["type"] == "replacement"
        assert echo["pre_text"] == "morning"
        assert echo["post_text"] == "evening"
        assert echo["context_before"] == "Good "
        assert echo["start_index"] == 6
        assert echo["end_index"] == 20
        # Exactly ONE extra read, as the docstring promises.
        assert len(_get_calls(service)) == 1

    @pytest.mark.asyncio
    async def test_diff_finds_the_suggestion_when_the_api_omits_created_ids(self):
        """The real API may return no createdSuggestionIds at all; the
        before/after diff must still name what was created."""
        _observe()  # a listing that found nothing pending
        service = _batch_service({}, document=REPLACEMENT_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="evening",
            )
        )

        assert result["created_suggestion_ids"] == []
        (echo,) = result["verification"]["created_suggestions"]
        assert echo["suggestion_id"] == "suggest.rep1"

    @pytest.mark.asyncio
    async def test_created_ids_the_read_cannot_confirm_are_dropped(self):
        """An id the response named but the document does not carry (a §6
        merge retired it) is never echoed as if it were live."""
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["merged.away"]}]},
            document=EMPTY_READ,
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="x",
            )
        )

        assert result["created_suggestion_ids"] == ["merged.away"]
        assert result["verification"]["created_suggestions"] == []

    @pytest.mark.asyncio
    async def test_verify_false_makes_no_extra_read(self):
        service = _batch_service({}, document=REPLACEMENT_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="x",
                verify=False,
            )
        )

        assert result["verification"] == {"source": "skipped", "reason": "verify=false"}
        assert _get_calls(service) == []

    @pytest.mark.asyncio
    async def test_a_failed_verification_read_never_fails_the_write(self):
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["s1"]}]}
        )
        service.documents.return_value.get.return_value.execute = Mock(
            side_effect=RuntimeError("read blew up")
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="x",
            )
        )

        assert result["created_suggestion_ids"] == ["s1"]
        assert result["verification"]["source"] == "unavailable"
        assert "read blew up" in result["verification"]["reason"]

    @pytest.mark.asyncio
    async def test_a_suggestion_absorbed_by_the_new_edit_is_reported(self):
        """§6 merge: an adjacent same-author suggestion is gone afterwards."""
        _observe(REP1_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["suggest.ins1"]}]},
            document=fx.build_tabs_payload([("t.0", fx.DOC_PLAIN_INSERTION)]),
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="brave",
            )
        )

        verification = result["verification"]
        assert verification["also_removed_suggestion_ids"] == ["suggest.rep1"]
        assert "merges" in verification["notes"][0]


CONC_DOC = "conc-doc"
CONC_SEED = {
    "me": "mockuser",
    "documents": [
        {
            "document_id": CONC_DOC,
            "title": "Concurrency",
            "text": "The brave new plan ships in March.\n",
            "suggestions": [],
        }
    ],
}


def _other_editor_suggests(backend, *, editor: str, text: str, at: int) -> str:
    """The harness's ``overlapping_suggestion``, fired as ``editor``."""
    effect, violations, _ = apply_interference(
        backend,
        Interference(
            name=f"{editor}-edits",
            kind="overlapping_suggestion",
            trigger=Trigger(),
            editor=editor,
            document_id=CONC_DOC,
            params={"at": at, "text": text},
        ),
    )
    assert violations == [], violations
    return effect["suggestion_id"]


class TestAConcurrentReviewersCardIsNotClaimedAsOurs:
    """MEDIUM 5: ``created_suggestions`` claims authorship, so it needs it.

    Source (2) of the echo is a DIFF -- "ids in the post-write read that were
    not in the last listing" -- against a snapshot that is as old as the last
    ``list_document_suggestions``. A second reviewer working in the document
    at the same time has their card land in exactly that set, and it was
    echoed under ``created_suggestions``: this call claiming authorship of
    somebody else's suggestion, which the agent may then reply to or resolve
    as its own.

    Driven through ``mockdocs.concurrency`` rather than a hand-built payload,
    so the other editor's card is produced by the same SPEC §5 operations a
    real second editor's would be -- including its thread, which is the only
    evidence of authorship the API offers.
    """

    @pytest.mark.asyncio
    async def test_another_editors_card_is_reported_but_not_attributed(self):
        backend = FakeBackend(me="mockuser")
        backend.seed(CONC_SEED)
        # The agent's last listing: nothing pending anywhere.
        suggestion_ledger.observe(EMAIL, CONC_DOC, [])

        # Bob edits while the agent is deciding.
        bob = _other_editor_suggests(backend, editor="bob", text="URGENT ", at=0)

        fn = _unwrap(write_tools.suggest_doc_edit)
        result = json.loads(
            await fn(
                backend.docs_service(),
                user_google_email=EMAIL,
                document_id=CONC_DOC,
                start_index=11,
                text="bold ",
            )
        )

        verification = result["verification"]
        ours = {s["suggestion_id"] for s in verification["created_suggestions"]}
        theirs = {s["suggestion_id"] for s in verification["appeared_since_last_read"]}

        assert bob in theirs
        assert bob not in ours, "another editor's card was claimed by this write"
        assert ours == set(result["created_suggestion_ids"])
        assert ours, "the write's own suggestion must still be echoed"
        (note,) = verification["notes"]
        assert bob in note
        assert "NOT reported as created by this call" in note
        assert "different author" in note

    @pytest.mark.asyncio
    async def test_our_own_card_is_still_attributed_without_a_reported_id(self):
        """Source (2) still does its job: an id the API did not report, but
        whose thread names us, is ours."""
        backend = FakeBackend(me="mockuser")
        backend.seed(CONC_SEED)
        suggestion_ledger.observe(EMAIL, CONC_DOC, [])

        # A second session of the SAME account -- the phone, the other tab.
        mine = _other_editor_suggests(backend, editor="mockuser", text="URGENT ", at=0)

        fn = _unwrap(write_tools.suggest_doc_edit)
        result = json.loads(
            await fn(
                backend.docs_service(),
                user_google_email=EMAIL,
                document_id=CONC_DOC,
                start_index=11,
                text="bold ",
            )
        )

        verification = result["verification"]
        ours = {s["suggestion_id"] for s in verification["created_suggestions"]}
        assert mine in ours
        assert "appeared_since_last_read" not in verification

    def test_unknown_authorship_is_not_ownership(self):
        """A read that degraded to the GA documents.get carries no threads,
        so every author is null. Null is not "me"."""
        assert write_tools._is_ours({"author": {"me": True}}) is True
        assert write_tools._is_ours({"author": {"me": False}}) is False
        assert write_tools._is_ours({"author": {"me": None}}) is False
        assert write_tools._is_ours({"author": None}) is False
        assert write_tools._is_ours({}) is False


class TestManageSuggestionVerification:
    @pytest.mark.asyncio
    async def test_accept_echoes_what_it_resolved_and_the_resulting_text(self):
        _observe(REP1_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.rep1",
            )
        )

        verification = result["verification"]
        assert verification["source"] == "post_write_read"
        assert verification["still_pending"] is False
        assert verification["resolved_suggestion"]["summary_text"] == (
            "Replace: “morning” with “evening”"
        )
        # accept means the range should now read post_text.
        assert verification["expected_text"] == "evening"
        assert "Good evening" in verification["resulting_text"]
        assert verification["matches_expectation"] is True
        assert verification["pending_suggestion_ids"] == []
        assert "also_removed_suggestion_ids" not in verification

    @pytest.mark.asyncio
    async def test_reject_expects_the_pre_text(self):
        _observe(REP1_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"rejectedSuggestionIds": ["suggest.rep1"]}]},
            document=fx.build_tabs_payload(
                [("t.0", fx.build_doc([fx.paragraph(fx.run("Good morning\n"))]))]
            ),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="reject",
                suggestion_id="suggest.rep1",
            )
        )

        verification = result["verification"]
        assert verification["expected_text"] == "morning"
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_collateral_gc_is_named_in_the_response(self):
        """The higher-leverage half of Fix 2: report the collateral BEFORE
        the agent trips over it as an unexplained 400."""
        _observe(REP1_RECORD, {"suggestion_id": "suggest.del1", "pre_text": " cruel"})
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.rep1",
            )
        )

        verification = result["verification"]
        assert verification["also_removed_suggestion_ids"] == ["suggest.del1"]
        (note,) = verification["notes"]
        assert "suggest.del1" in note and "suggest.rep1" in note
        assert "last character it marked" in note

    @pytest.mark.asyncio
    async def test_resolved_suggestion_is_null_when_it_was_never_listed(self):
        service = _batch_service({}, document=EMPTY_READ)
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="never.listed",
            )
        )

        verification = result["verification"]
        assert verification["resolved_suggestion"] is None
        assert verification["expected_text"] is None
        assert verification["matches_expectation"] is None
        # The end state is still verified: the id is not pending any more.
        assert verification["still_pending"] is False

    @pytest.mark.asyncio
    async def test_verify_false_still_remembers_the_resolution(self):
        _observe(REP1_RECORD)
        service = _batch_service({}, document=ACCEPTED_READ)
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.rep1",
                verify=False,
            )
        )

        assert result["verification"]["source"] == "skipped"
        assert _get_calls(service) == []
        assert "You accepted it yourself" in suggestion_ledger.explain_missing(
            EMAIL, DOC, "suggest.rep1"
        )

    @pytest.mark.asyncio
    async def test_accepting_a_deletion_verifies_the_text_is_gone(self):
        """post_text is empty for a pure deletion, so there is nothing to
        find: the check flips to "is the struck text actually gone"."""
        _observe(
            {
                "suggestion_id": "suggest.del1",
                "type": "deletion",
                "pre_text": " cruel",
                "post_text": "",
                "context_before": "Hello",
                "context_after": " world.\n",
            }
        )
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.del1"]}]},
            document=fx.build_tabs_payload(
                [("t.0", fx.build_doc([fx.paragraph(fx.run("Hello world.\n"))]))]
            ),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.del1",
            )
        )

        verification = result["verification"]
        assert verification["expected_text"] == ""
        assert verification["resulting_text"] == "Hello world.\n"
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_a_deletion_that_did_not_land_is_flagged(self):
        _observe(
            {
                "suggestion_id": "suggest.del1",
                "type": "deletion",
                "pre_text": " cruel",
                "post_text": "",
                "context_before": "Hello",
                "context_after": " world.\n",
            }
        )
        service = _batch_service(
            {},
            document=fx.build_tabs_payload(
                [("t.0", fx.build_doc([fx.paragraph(fx.run("Hello cruel world.\n"))]))]
            ),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.del1",
            )
        )
        assert result["verification"]["matches_expectation"] is False


class TestVerificationIsNotDecidedByTheEchoClip:
    """A verdict must be a statement about the document, not about the echo.

    ``_locate`` used to return a window clipped to ``ECHO_MAX_CHARS`` (200)
    while ``_verify_resolution`` compared the UNCLIPPED ``pre_text`` /
    ``post_text`` against it. Any suggestion longer than
    ``ECHO_MAX_CHARS - CONTEXT_WINDOW`` (~160 characters) therefore had its
    verdict decided by the truncation:

    - accepting a long **deletion** forced ``removed_text not in
      resulting_text`` TRUE, so ``matches_expectation: true`` was emitted
      with no check having occurred -- fail-open verification on the one
      destructive path this tool has;
    - accepting a long **replacement** (or rejecting a long insertion) forced
      ``expected_text in resulting_text`` FALSE, raising a false alarm about
      a write that had landed perfectly, which an agent may "fix" by
      re-suggesting into a customer document.

    Invisible to the prod e2e suite because its fixtures use short strings,
    so these cases are pinned here at 300 characters -- comfortably past both
    the 200-char echo cap and the 160-char effective threshold.
    """

    #: Past ECHO_MAX_CHARS (200) so a clipped window cannot contain it.
    LONG_OLD = "old-" + "x" * 296
    LONG_NEW = "new-" + "y" * 296
    HEAD = "Head. "
    TAIL = " Tail.\n"

    @classmethod
    def _body(cls, middle: str) -> dict:
        """A one-paragraph document reading HEAD + ``middle`` + TAIL."""
        return fx.build_tabs_payload(
            [
                (
                    "t.0",
                    fx.build_doc([fx.paragraph(fx.run(cls.HEAD + middle + cls.TAIL))]),
                )
            ]
        )

    @classmethod
    def _record(cls, sid: str, kind: str, pre: str, post: str) -> dict:
        return {
            "suggestion_id": sid,
            "type": kind,
            "pre_text": pre,
            "post_text": post,
            "context_before": cls.HEAD,
            "context_after": cls.TAIL,
            "segment": "body",
            "segment_id": None,
            "tab_id": None,
            "start_index": 1 + len(cls.HEAD),
            "end_index": 1 + len(cls.HEAD) + len(pre or post),
            "summary_text": "long edit",
            "status": "OPEN",
        }

    async def _resolve(self, action: str, record: dict, middle: str) -> dict:
        _observe(record)
        sid = record["suggestion_id"]
        service = _batch_service(
            {"suggestionResponses": [{f"{action}edSuggestionIds": [sid]}]},
            document=self._body(middle),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)
        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action=action,
                suggestion_id=sid,
            )
        )
        return result["verification"]

    # -- the two fail-open cases (long text GONE is the expectation) -------

    @pytest.mark.asyncio
    async def test_accepting_a_long_deletion_that_landed_is_true(self):
        verification = await self._resolve(
            "accept",
            self._record("s.del", "deletion", self.LONG_OLD, ""),
            middle="",
        )
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_accepting_a_long_deletion_that_did_not_land_is_false(self):
        """The fail-open regression: this returned True with no check run."""
        verification = await self._resolve(
            "accept",
            self._record("s.del", "deletion", self.LONG_OLD, ""),
            middle=self.LONG_OLD,
        )
        assert verification["matches_expectation"] is False

    @pytest.mark.asyncio
    async def test_rejecting_a_long_insertion_that_reverted_is_true(self):
        verification = await self._resolve(
            "reject",
            self._record("s.ins", "insertion", "", self.LONG_NEW),
            middle="",
        )
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_rejecting_a_long_insertion_still_present_is_false(self):
        """The same fail-open shape on the reject path."""
        verification = await self._resolve(
            "reject",
            self._record("s.ins", "insertion", "", self.LONG_NEW),
            middle=self.LONG_NEW,
        )
        assert verification["matches_expectation"] is False

    # -- the two false-alarm cases (long text PRESENT is the expectation) --

    @pytest.mark.asyncio
    async def test_accepting_a_long_replacement_that_landed_is_true(self):
        """The false-alarm regression: this returned False on a clean write."""
        verification = await self._resolve(
            "accept",
            self._record("s.rep", "replacement", self.LONG_OLD, self.LONG_NEW),
            middle=self.LONG_NEW,
        )
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_accepting_a_long_replacement_that_did_not_land_is_false(self):
        verification = await self._resolve(
            "accept",
            self._record("s.rep", "replacement", self.LONG_OLD, self.LONG_NEW),
            middle=self.LONG_OLD,
        )
        assert verification["matches_expectation"] is False

    @pytest.mark.asyncio
    async def test_rejecting_a_long_replacement_that_reverted_is_true(self):
        verification = await self._resolve(
            "reject",
            self._record("s.rep", "replacement", self.LONG_OLD, self.LONG_NEW),
            middle=self.LONG_OLD,
        )
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_accepting_a_long_insertion_that_landed_is_true(self):
        verification = await self._resolve(
            "accept",
            self._record("s.ins", "insertion", "", self.LONG_NEW),
            middle=self.LONG_NEW,
        )
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_rejecting_a_long_deletion_that_reverted_is_true(self):
        verification = await self._resolve(
            "reject",
            self._record("s.del", "deletion", self.LONG_OLD, ""),
            middle=self.LONG_OLD,
        )
        assert verification["matches_expectation"] is True

    # -- the echo stays a receipt --------------------------------------

    @pytest.mark.asyncio
    async def test_the_echo_is_still_clipped_though_the_check_was_not(self):
        """Comparing on the full window must not blow up the context cost."""
        verification = await self._resolve(
            "accept",
            self._record("s.rep", "replacement", self.LONG_OLD, self.LONG_NEW),
            middle=self.LONG_NEW,
        )
        resulting = verification["resulting_text"]
        assert verification["matches_expectation"] is True
        # The verdict was decided on a window wider than what is echoed: the
        # echoed copy does not even contain the text it was checked against.
        assert self.LONG_NEW not in resulting
        assert len(resulting) == write_tools.ECHO_MAX_CHARS + 1
        assert resulting.endswith(write_tools.TRUNCATION_MARKER)
        assert len(verification["expected_text"]) == write_tools.ECHO_MAX_CHARS + 1


class TestFreeVerificationEchoes:
    """reply_to_doc_thread / create_anchored_doc_comment verify for free."""

    @pytest.mark.asyncio
    async def test_reply_echoes_the_stored_post_without_reading(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {"addCommentReply": {"post": {"postId": "p1", "content": "Hi"}}}
                ],
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

        assert result["content"] == "Hi"
        assert result["verification"] == {
            "source": "batch_update_response",
            "saved": True,
            "stored_content": "Hi",
            "matches_request": True,
        }
        assert _get_calls(service) == []

    @pytest.mark.asyncio
    async def test_reply_flags_content_the_api_stored_differently(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "addCommentReply": {
                            "post": {"postId": "p1", "content": "Hi (trimmed)"}
                        }
                    }
                ],
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
        assert result["verification"]["matches_request"] is False

    @pytest.mark.asyncio
    async def test_anchored_comment_echoes_the_text_it_actually_anchored_to(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "insertComment": {
                            "commentThread": {
                                "commentId": "c7",
                                "plainTextQuote": "The q",
                                "headPost": {"postId": "p7", "content": "Needs work"},
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
                content="Needs work",
                start_index=1,
                end_index=6,
            )
        )

        assert result["verification"] == {
            "source": "batch_update_response",
            "saved": True,
            "anchored_range": {
                "segment": "body",
                "segment_id": None,
                "tab_id": None,
                "start_index": 1,
                "end_index": 6,
            },
            "anchored_text": "The q",
            "stored_content": "Needs work",
            "matches_request": True,
        }
        assert _get_calls(service) == []

    @pytest.mark.asyncio
    async def test_long_echoes_are_truncated(self):
        long_content = "x" * (write_tools.ECHO_MAX_CHARS + 50)
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "addCommentReply": {
                            "post": {"postId": "p1", "content": long_content}
                        }
                    }
                ],
            }
        )
        fn = _unwrap(write_tools.reply_to_doc_thread)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content=long_content,
                comment_id="c1",
            )
        )
        stored = result["verification"]["stored_content"]
        assert len(stored) == write_tools.ECHO_MAX_CHARS + 1
        assert stored.endswith(write_tools.TRUNCATION_MARKER)
        # matches_request compares the UNTRUNCATED strings.
        assert result["verification"]["matches_request"] is True


# ---------------------------------------------------------------------------
# Fix 2 -- a missing id says WHY it is missing
# ---------------------------------------------------------------------------


class TestMissingSuggestionErrors:
    async def _accept(self, service, suggestion_id):
        """Land a real accept, so the ledger has something to explain from."""
        fn = _unwrap(write_tools.manage_document_suggestion)
        return await fn(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            action="accept",
            suggestion_id=suggestion_id,
        )

    @pytest.mark.asyncio
    async def test_proven_cause_we_resolved_that_very_id(self):
        _observe(REP1_RECORD)
        ok = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        await self._accept(ok, "suggest.rep1")

        again = _failing_service(
            _http_error(404, missing_suggestion_404("suggest.rep1"))
        )
        fn = _unwrap(write_tools.manage_document_suggestion)
        with pytest.raises(UserInputError) as excinfo:
            await fn(
                again,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.rep1",
            )
        message = str(excinfo.value)
        assert "no longer exists" in message
        assert "You accepted it yourself" in message
        assert "list_document_suggestions" in message
        # The API's own words are preserved, so the taxonomy still sees them.
        assert "does not exist" in message

    @pytest.mark.asyncio
    async def test_collateral_cause_names_the_suggestion_that_removed_it(self):
        _observe(REP1_RECORD, {"suggestion_id": "sug.bob.1"})
        accepted = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        await self._accept(accepted, "suggest.rep1")

        failing = _failing_service(_http_error(400, missing_suggestion_400()))
        fn = _unwrap(write_tools.manage_document_suggestion)
        with pytest.raises(UserInputError) as excinfo:
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="sug.bob.1",
            )
        message = str(excinfo.value)
        assert "'suggest.rep1'" in message
        assert "gone from the read right after" in message
        assert "last marked character" in message

    @pytest.mark.asyncio
    async def test_may_have_been_removed_when_causation_is_unproven(self):
        """We resolved something, but never saw this id disappear: the
        message must offer the possibility, never assert it."""
        _observe(REP1_RECORD)
        accepted = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        await self._accept(accepted, "suggest.rep1")

        failing = _failing_service(_http_error(400, missing_suggestion_400()))
        fn = _unwrap(write_tools.manage_document_suggestion)
        with pytest.raises(UserInputError) as excinfo:
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="sug.bob.1",
            )
        message = str(excinfo.value)
        assert "MAY have removed it" in message
        assert "not proven" in message

    @pytest.mark.asyncio
    async def test_unknown_id_says_the_id_is_probably_wrong(self):
        _observe(REP1_RECORD)
        failing = _failing_service(_http_error(404, missing_suggestion_404()))
        fn = _unwrap(write_tools.manage_document_suggestion)

        with pytest.raises(UserInputError) as excinfo:
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="sug.bob.1",
            )
        message = str(excinfo.value)
        assert "most likely the id is wrong" in message
        assert "MAY have removed" not in message

    @pytest.mark.asyncio
    async def test_never_read_the_document_says_so(self):
        failing = _failing_service(_http_error(404, missing_suggestion_404()))
        fn = _unwrap(write_tools.manage_document_suggestion)

        with pytest.raises(UserInputError) as excinfo:
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="sug.bob.1",
            )
        assert "has not read this document" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_reply_to_a_missing_suggestion_thread_is_explained_too(self):
        _observe(REP1_RECORD)
        accepted = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
            document=ACCEPTED_READ,
        )
        await self._accept(accepted, "suggest.rep1")

        failing = _failing_service(
            _http_error(404, missing_suggestion_404("suggest.rep1"))
        )
        fn = _unwrap(write_tools.reply_to_doc_thread)
        with pytest.raises(UserInputError) as excinfo:
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="why?",
                suggestion_id="suggest.rep1",
            )
        assert "reply_to_doc_thread" in str(excinfo.value)
        assert "You accepted it yourself" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_comment_thread_error_is_left_alone(self):
        """Only suggestion ids get the ledger treatment; a comment-thread
        failure must reach handle_http_errors unchanged."""
        failing = _failing_service(
            _http_error(400, b'{"error": {"message": "comment c1 was not found"}}')
        )
        fn = _unwrap(write_tools.reply_to_doc_thread)

        with pytest.raises(HttpError):
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="hi",
                comment_id="c1",
            )

    @pytest.mark.asyncio
    async def test_an_error_about_a_different_id_is_not_reinterpreted(self):
        """The API named some other suggestion: not our call's id, so the
        message is not ours to rewrite."""
        _observe(REP1_RECORD)
        failing = _failing_service(_http_error(404, missing_suggestion_404()))
        fn = _unwrap(write_tools.manage_document_suggestion)

        with pytest.raises(HttpError):
            await fn(
                failing,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.rep1",
            )


class TestMergedEditEcho:
    """Verified against prod 2026-07-30 (e2e/last_run.md): editing inside an
    existing same-author suggestion merges into it and the response carries
    NO created id. The echo must not go silent in exactly that case."""

    @pytest.mark.asyncio
    async def test_no_created_id_falls_back_to_the_edited_range(self):
        service = _batch_service({}, document=REPLACEMENT_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=9,
                text="XY",
            )
        )

        verification = result["verification"]
        assert verification["created_suggestions"] == []
        (echo,) = verification["suggestions_at_edit_range"]
        assert echo["suggestion_id"] == "suggest.rep1"
        assert echo["pre_text"] == "morning"
        assert "merges into it" in verification["notes"][0]

    @pytest.mark.asyncio
    async def test_a_suggestion_elsewhere_is_not_claimed(self):
        service = _batch_service({}, document=REPLACEMENT_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=500,
                text="XY",
            )
        )

        verification = result["verification"]
        assert verification["created_suggestions"] == []
        assert "suggestions_at_edit_range" not in verification

    @pytest.mark.asyncio
    async def test_an_attributable_id_wins_over_the_range_fallback(self):
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["suggest.rep1"]}]},
            document=REPLACEMENT_READ,
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=9,
                text="XY",
            )
        )

        verification = result["verification"]
        assert [s["suggestion_id"] for s in verification["created_suggestions"]] == [
            "suggest.rep1"
        ]
        assert "suggestions_at_edit_range" not in verification

    @pytest.mark.asyncio
    async def test_index_zero_is_writable_in_a_segment_but_not_the_body(self):
        """A header/footer/footnote is numbered from its own start, so 0 is
        a position there -- verified against the live API 2026-07-31. The
        blanket `start_index >= 1` rule made the first character of every
        such segment unwritable."""
        service = _batch_service({}, document=REPLACEMENT_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        with pytest.raises(UserInputError, match="section break"):
            await fn(
                service, user_google_email=EMAIL, document_id=DOC,
                start_index=0, text="X",
            )
        json.loads(
            await fn(
                service, user_google_email=EMAIL, document_id=DOC,
                start_index=0, text="X", segment_id="kix.h1",
            )
        )
        request = service.documents.return_value.batchUpdate.call_args.kwargs["body"][
            "requests"
        ][0]
        assert request["insertText"]["location"] == {
            "index": 0,
            "segmentId": "kix.h1",
        }

    @pytest.mark.asyncio
    async def test_a_suggestion_in_another_segment_is_not_at_the_edit(self):
        """Docs numbers a header from its own start, so a header suggestion
        can carry the same numbers as a body edit without being anywhere
        near it. Echoing it would tell the caller its merge landed in a
        place it did not."""
        document = fx.build_doc(
            [fx.paragraph(fx.run("Body only.\n"))],
            headers={
                "kix.h1": [
                    fx.paragraph(
                        fx.run("Head "), fx.run("edit", ins=["suggest.hdr1"]),
                        fx.run("\n"),
                    )
                ]
            },
        )
        service = _batch_service({}, document=fx.build_tabs_payload([("t.0", document)]))
        fn = _unwrap(write_tools.suggest_doc_edit)

        # The header suggestion is [5, 9); this body edit sits on the same
        # numbers, in the body.
        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="XY",
            )
        )

        verification = result["verification"]
        assert verification["created_suggestions"] == []
        assert "suggestions_at_edit_range" not in verification

        # Naming the header finds it.
        in_header = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="XY",
                segment_id="kix.h1",
            )
        )
        (echo,) = in_header["verification"]["suggestions_at_edit_range"]
        assert echo["suggestion_id"] == "suggest.hdr1"


# ---------------------------------------------------------------------------
# Round 2 -- an index is only half of an address, on the WRITE path
# ---------------------------------------------------------------------------

#: Body suggestion and header suggestion in the same tab.
DOC_BODY_AND_HEADER = fx.build_doc(
    [fx.paragraph(fx.run("Good "), fx.run("morning", dels=["suggest.rep1"]),
                  fx.run("evening", ins=["suggest.rep1"]), fx.run("\n"))],
    headers={
        "kix.h1": [
            fx.paragraph(fx.run("DRAFT", ins=["suggest.hdr1"]), fx.run(" header\n"))
        ]
    },
)

#: Two tabs, each with a suggestion on the SAME local numbers.
TWO_TAB_READ = fx.build_tabs_payload(
    [
        ("t.0", fx.build_doc([fx.paragraph(fx.run("One "),
                                           fx.run("alpha", ins=["suggest.t0"]),
                                           fx.run(".\n"))])),
        ("t.second", fx.build_doc([fx.paragraph(fx.run("Two "),
                                                fx.run("bravo", ins=["suggest.t1"]),
                                                fx.run(".\n"))])),
    ]
)

ADDRESS_KEYS = {"segment", "segment_id", "tab_id", "start_index", "end_index"}


class TestEveryEchoedIndexCarriesItsAddress:
    """The BLOCKER: an echoed index the agent can hand back to a write tool
    has to say WHICH (tab, segment) it is numbered in. ``suggest_doc_edit``
    and ``create_anchored_doc_comment`` default to the body of the default
    tab, so a bare header index aimed back at them writes into the body of a
    customer document. Index 0 fails loud on the floor check; nothing else
    does."""

    @pytest.mark.asyncio
    async def test_created_suggestion_echo_is_a_complete_address(self):
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["suggest.hdr1"]}]},
            document=fx.build_tabs_payload([("t.0", DOC_BODY_AND_HEADER)]),
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=0,
                text="DRAFT",
                segment_id="kix.h1",
            )
        )

        (echo,) = result["verification"]["created_suggestions"]
        assert ADDRESS_KEYS <= set(echo)
        assert echo["segment"] == "header"
        assert echo["segment_id"] == "kix.h1"
        assert echo["tab_id"] == "t.0"
        assert echo["start_index"] == 0

    @pytest.mark.asyncio
    async def test_merged_edit_echo_is_a_complete_address(self):
        service = _batch_service(
            {}, document=fx.build_tabs_payload([("t.0", DOC_BODY_AND_HEADER)])
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=1,
                text="XY",
                segment_id="kix.h1",
            )
        )

        (echo,) = result["verification"]["suggestions_at_edit_range"]
        assert echo["suggestion_id"] == "suggest.hdr1"
        assert echo["segment_id"] == "kix.h1"
        assert echo["tab_id"] == "t.0"
        # The space the range was read in is stated, not left to be inferred.
        assert result["verification"]["range_scope"] == {
            "segment": "header",
            "segment_id": "kix.h1",
            "tab_id": "t.0",
        }

    @pytest.mark.asyncio
    async def test_resolved_suggestion_echo_is_a_complete_address(self):
        """The ledger drops nothing an index needs: it is what
        ``resolved_suggestion`` is built from."""
        _observe(
            {
                "suggestion_id": "suggest.hdr1",
                "type": "insertion",
                "pre_text": "",
                "post_text": "DRAFT",
                "context_before": "",
                "context_after": " header\n",
                "segment": "header",
                "segment_id": "kix.h1",
                "tab_id": "t.0",
                "start_index": 0,
                "end_index": 5,
                "summary_text": "Add: “DRAFT”",
                "status": "OPEN",
            }
        )
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.hdr1"]}]},
            document=fx.build_tabs_payload([("t.0", fx.DOC_EMPTY)]),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.hdr1",
            )
        )

        echo = result["verification"]["resolved_suggestion"]
        assert ADDRESS_KEYS <= set(echo)
        assert echo["segment"] == "header"
        assert echo["segment_id"] == "kix.h1"
        assert echo["tab_id"] == "t.0"

    def test_the_ledger_keeps_the_address_it_was_given(self):
        suggestion_ledger.observe(EMAIL, DOC, [REP1_RECORD])
        kept = suggestion_ledger.record_of(EMAIL, DOC, "suggest.rep1")
        assert ADDRESS_KEYS <= set(kept)
        assert kept["tab_id"] == "t.0"
        assert kept["segment"] == "body"


class TestOverlapSemanticsAreShared:
    """HIGH 1: the listing REFUSES to read an index range in a multi-tab
    document without a tab_id; the write echo used to guess, and echoed
    overlapping suggestions from EVERY tab under the claim "your edit merged
    into it". Both now go through :mod:`gdocs_preview.address`, and this test
    is what stops them drifting apart again."""

    RECORDS = [
        {"suggestion_id": "b.t0", "segment": "body", "segment_id": None,
         "tab_id": "t.0", "start_index": 5, "end_index": 9},
        {"suggestion_id": "h.t0", "segment": "header", "segment_id": "kix.h1",
         "tab_id": "t.0", "start_index": 5, "end_index": 9},
        {"suggestion_id": "b.t1", "segment": "body", "segment_id": None,
         "tab_id": "t.second", "start_index": 5, "end_index": 9},
    ]

    #: The DOCUMENT's tabs. ``RECORDS`` occupy only two of the three.
    TAB_IDS = ("t.0", "t.second", "t.third")

    @staticmethod
    def _read_side(records, *, tab_ids, segment_id, tab_id):
        """The listing's range filter: ``("refused", why)`` or ``("ok", ids)``."""
        try:
            kept, _ = review_page.filter_records(
                records, tab_ids=tab_ids, start_index=0, end_index=10_000,
                segment_id=segment_id, tab_id=tab_id,
            )
        except ValueError as error:
            return ("refused", str(error))
        return ("ok", [r["suggestion_id"] for r in kept])

    @staticmethod
    def _write_side(records, *, tab_ids, segment_id, tab_id):
        """The write echo's ``suggestions_at_edit_range``, same shape."""
        try:
            scope = address.resolve_range_scope(
                records, tab_ids=tab_ids, segment_id=segment_id, tab_id=tab_id
            )
        except ValueError as error:
            return ("refused", str(error))
        return (
            "ok",
            [
                r["suggestion_id"]
                for r in records
                if write_tools._overlaps(r, (0, 9_999), scope)
            ],
        )

    @pytest.mark.parametrize(
        "segment_id,tab_id",
        [
            (None, None),  # multi-tab, unnamed: BOTH must refuse
            (None, "t.0"),
            (None, "t.second"),
            ("kix.h1", "t.0"),
            ("kix.h1", None),
            ("kix.nope", "t.0"),
        ],
    )
    def test_both_paths_answer_a_range_identically(self, segment_id, tab_id):
        """Same verdict AND same reason -- including refusing together."""
        assert self._read_side(
            self.RECORDS, tab_ids=self.TAB_IDS, segment_id=segment_id, tab_id=tab_id
        ) == self._write_side(
            self.RECORDS, tab_ids=self.TAB_IDS, segment_id=segment_id, tab_id=tab_id
        )

    def test_both_paths_refuse_a_multi_tab_range_without_a_tab_id(self):
        for side in (self._read_side, self._write_side):
            verdict, reason = side(
                self.RECORDS, tab_ids=self.TAB_IDS, segment_id=None, tab_id=None
            )
            assert verdict == "refused"
            assert "needs a tab_id" in reason

    def test_a_single_tab_document_resolves_implicitly_on_both_paths(self):
        single = [r for r in self.RECORDS if r["tab_id"] == "t.0"]
        for side in (self._read_side, self._write_side):
            assert side(single, tab_ids=("t.0",), segment_id=None, tab_id=None) == (
                "ok",
                ["b.t0"],
            )

    def test_a_tab_holding_no_cards_still_forces_a_tab_id(self):
        """HIGH 1: the round-1/2 class surviving UNDER the refusal.

        The refusal counted the tabs the RECORDS occupy, which is a
        different question from how many tabs the document has. A three-tab
        document whose cards all sit in tab B looks single-tab from the
        records, so the refusal never fired and the omitted ``tab_id``
        resolved silently to B -- and a caller meaning the default tab got
        tab B's cards echoed as "the suggestion(s) at the edited range".
        """
        in_one_tab = [r for r in self.RECORDS if r["tab_id"] == "t.second"]
        # The premise: from the records alone this document looks single-tab.
        assert {r["tab_id"] for r in in_one_tab} == {"t.second"}

        for side in (self._read_side, self._write_side):
            verdict, reason = side(
                in_one_tab, tab_ids=self.TAB_IDS, segment_id=None, tab_id=None
            )
            assert verdict == "refused"
            assert "3 tabs" in reason
            # And the answer it USED to give, which was a wrong-tab answer.
            assert side(
                in_one_tab, tab_ids=("t.second",), segment_id=None, tab_id=None
            ) == ("ok", ["b.t1"])

    @pytest.mark.asyncio
    async def test_the_write_echo_declines_rather_than_naming_another_tab(self):
        """Naming a suggestion from a tab the edit never touched, and
        asserting the edit merged into it, hands the agent a wrong id it may
        go on to reply to or accept."""
        service = _batch_service({}, document=TWO_TAB_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="XY",
            )
        )

        verification = result["verification"]
        assert "suggestions_at_edit_range" not in verification
        assert "needs a tab_id" in verification["suggestions_at_edit_range_unavailable"]
        assert "more than one tab" in verification["notes"][0]

    @pytest.mark.asyncio
    async def test_naming_the_tab_answers_it(self):
        service = _batch_service({}, document=TWO_TAB_READ)
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="XY",
                tab_id="t.second",
            )
        )

        (echo,) = result["verification"]["suggestions_at_edit_range"]
        assert echo["suggestion_id"] == "suggest.t1"
        assert echo["tab_id"] == "t.second"


class TestResolutionIsLocatedInItsOwnSegment:
    """HIGH 2: ``_locate`` used to search the merged body text of every tab.
    A suggestion at the very start of a header has ``context_before == ""``
    (analysis.py), so the empty anchor returned the head of the BODY and
    ``matches_expectation`` for a header resolution was computed there."""

    #: The header suggestion accepted: "DRAFT header\n" stays in the header.
    ACCEPTED_HEADER_READ = fx.build_tabs_payload(
        [
            (
                "t.0",
                fx.build_doc(
                    [fx.paragraph(fx.run("Good evening\n"))],
                    headers={"kix.h1": [fx.paragraph(fx.run("DRAFT header\n"))]},
                ),
            )
        ]
    )

    HEADER_RECORD = {
        "suggestion_id": "suggest.hdr1",
        "type": "insertion",
        "pre_text": "",
        "post_text": "DRAFT",
        "context_before": "",  # at the very start of the header
        "context_after": " header\n",
        "segment": "header",
        "segment_id": "kix.h1",
        "tab_id": "t.0",
        "start_index": 0,
        "end_index": 5,
    }

    @pytest.mark.asyncio
    async def test_a_header_resolution_reads_the_header_not_the_body(self):
        _observe(self.HEADER_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.hdr1"]}]},
            document=self.ACCEPTED_HEADER_READ,
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.hdr1",
            )
        )

        verification = result["verification"]
        assert verification["resulting_text"] == "DRAFT header\n"
        assert "Good evening" not in verification["resulting_text"]
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_an_empty_anchor_no_longer_answers_from_the_body(self):
        """Same construction, but the header did NOT get the text: the old
        body fallback found "DRAFT" in the body and called it a match."""
        _observe(self.HEADER_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.hdr1"]}]},
            document=fx.build_tabs_payload(
                [
                    (
                        "t.0",
                        fx.build_doc(
                            [fx.paragraph(fx.run("DRAFT body copy\n"))],
                            headers={"kix.h1": [fx.paragraph(fx.run(" header\n"))]},
                        ),
                    )
                ]
            ),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.hdr1",
            )
        )

        verification = result["verification"]
        assert verification["resulting_text"] == " header\n"
        assert verification["matches_expectation"] is False

    @pytest.mark.asyncio
    async def test_a_second_tab_resolution_is_not_located_in_the_first(self):
        _observe(
            {
                "suggestion_id": "suggest.t1",
                "type": "insertion",
                "pre_text": "",
                "post_text": "bravo",
                "context_before": "Two ",
                "context_after": ".\n",
                "segment": "body",
                "segment_id": None,
                "tab_id": "t.second",
                "start_index": 5,
                "end_index": 10,
            }
        )
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.t1"]}]},
            document=fx.build_tabs_payload(
                [
                    ("t.0", fx.build_doc([fx.paragraph(fx.run("Two alpha.\n"))])),
                    ("t.second", fx.build_doc([fx.paragraph(fx.run("Two bravo.\n"))])),
                ]
            ),
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.t1",
            )
        )

        verification = result["verification"]
        # "Two " matches in BOTH tabs; the merged text found the first one
        # and reported "Two alpha." as the result of a tab-2 accept.
        assert verification["resulting_text"] == "Two bravo.\n"
        assert verification["matches_expectation"] is True

    @pytest.mark.asyncio
    async def test_an_unlocatable_tab_reports_nothing_rather_than_guessing(self):
        """A record with no tab id, in a document with several: there is no
        honest window to return."""
        _observe(
            {
                "suggestion_id": "suggest.t1",
                "type": "insertion",
                "pre_text": "",
                "post_text": "bravo",
                "context_before": "Two ",
                "context_after": ".\n",
                "segment": "body",
                "segment_id": None,
                "tab_id": None,
                "start_index": 5,
                "end_index": 10,
            }
        )
        service = _batch_service(
            {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.t1"]}]},
            document=TWO_TAB_READ,
        )
        fn = _unwrap(write_tools.manage_document_suggestion)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id="suggest.t1",
            )
        )

        verification = result["verification"]
        assert verification["resulting_text"] is None
        assert verification["matches_expectation"] is None


class TestAnUnlocatedWindowSaysWhy:
    """HIGH 2: ``matches_expectation: null`` used to be four situations.

    A null verdict with no note is indistinguishable from "we never listed
    this id" (nothing is wrong), from an ambiguous multi-tab read (name the
    tab), from a degraded read that lost its tab ids (retry), and from a
    concurrent-edit anchor miss (somebody else changed the document). The
    agent's next move differs in every case, and none of them means the
    write failed -- which is the reading a bare null invites. Its sibling
    ``_verify_suggest`` already names its one ambiguity
    (``suggestions_at_edit_range_unavailable``); this is the same courtesy
    on the destructive path.
    """

    RECORD = {
        "suggestion_id": "suggest.t1",
        "type": "insertion",
        "pre_text": "",
        "post_text": "bravo",
        "context_before": "Two ",
        "context_after": ".\n",
        "segment": "body",
        "segment_id": None,
        "tab_id": "t.0",
        "start_index": 5,
        "end_index": 10,
    }

    @staticmethod
    async def _accept(service, suggestion_id="suggest.t1") -> dict:
        fn = _unwrap(write_tools.manage_document_suggestion)
        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id=suggestion_id,
            )
        )
        return result["verification"]

    @pytest.mark.asyncio
    async def test_an_id_we_never_listed_says_so(self):
        """The benign case, and the one a bare null hid most damagingly:
        nothing about the document is wrong."""
        verification = await self._accept(
            _batch_service({}, document=EMPTY_READ), suggestion_id="never.listed"
        )
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "suggestion_not_listed"
        (note,) = verification["notes"]
        assert "never listed" in note
        assert "NOT because the write failed" in note
        # The end state is still answered.
        assert verification["still_pending"] is False

    @pytest.mark.asyncio
    async def test_an_ambiguous_tab_says_which_tabs(self):
        _observe({**self.RECORD, "tab_id": None})
        verification = await self._accept(_batch_service({}, document=TWO_TAB_READ))
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "ambiguous_tab"
        (note,) = verification["notes"]
        assert "2 tabs" in note and "t.0" in note and "t.second" in note
        assert "list_document_suggestions(tab_id=...)" in note

    @pytest.mark.asyncio
    async def test_a_degraded_read_that_lost_the_tab_ids_says_so(self):
        """The GA documents.get carries no tabs at all, so a card listed in
        't.0' has no segment to be located in after the fallback."""
        _observe(self.RECORD)
        service = Mock()
        service.documents.return_value.batchUpdate.return_value.execute = Mock(
            return_value={}
        )

        def _get(**kwargs):
            if "commentsViewMode" in kwargs:
                raise preview_read.PreviewReadError("not enrolled")
            return Mock(
                execute=Mock(
                    return_value=fx.build_doc([fx.paragraph(fx.run("Two bravo.\n"))])
                )
            )

        service.documents.return_value.get.side_effect = _get

        verification = await self._accept(service)
        assert verification["read_source"] == "ga_documents_get"
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "segment_not_in_read"
        (note,) = verification["notes"]
        assert "ga_documents_get" in note
        assert "'t.0'" in note

    @pytest.mark.asyncio
    async def test_a_vanished_anchor_is_named_as_a_concurrent_edit(self):
        """The anchor is base text -- resolving cannot touch it -- so its
        disappearance is somebody else's edit, not this write's doing."""
        _observe(self.RECORD)
        verification = await self._accept(
            _batch_service(
                {},
                document=fx.build_tabs_payload(
                    [
                        (
                            "t.0",
                            fx.build_doc([fx.paragraph(fx.run("Wholly rewritten.\n"))]),
                        )
                    ]
                ),
            )
        )
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "anchor_not_found"
        (note,) = verification["notes"]
        assert "concurrent edit" in note

    @pytest.mark.asyncio
    async def test_a_located_window_carries_no_unavailable_key(self):
        _observe(self.RECORD)
        verification = await self._accept(
            _batch_service(
                {},
                document=fx.build_tabs_payload(
                    [("t.0", fx.build_doc([fx.paragraph(fx.run("Two bravo.\n"))]))]
                ),
            )
        )
        assert verification["matches_expectation"] is True
        assert "resulting_text_unavailable" not in verification
        assert "notes" not in verification

    def test_every_reason_has_a_note(self):
        """A reason code with no sentence behind it is a silent null again."""
        read = Mock(tab_ids=["t.0", "t.second"], source="preview_threads")
        for reason in write_tools.UNLOCATED_REASONS:
            note = write_tools._unlocated_note(
                reason,
                suggestion_id="s.1",
                record=self.RECORD,
                read=read,
            )
            assert "s.1" in note or "post-write read" in note
            assert "NOT because the write failed" in note
