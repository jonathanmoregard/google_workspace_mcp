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
import logging
import re
from unittest.mock import MagicMock, Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import UserInputError
from gdocs_preview import (
    address,
    analysis,
    curated_tools,
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

#: For reading the count word out of an agent-facing docstring, so that
#: growing a reason list without renumbering the prose is a test failure.
_NUMBER_WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


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


def _degraded_read_service(response=None, document=None):
    """A service whose post-write read DEGRADES to the GA documents.get.

    This is the read shape ``preview_read.read_for_review`` falls back to when
    the thread-bearing request fails: one unnamed body, ``tabs=[(None, doc)]``,
    ``read_source="ga_documents_get"``, and therefore no way to see into any
    named tab. It is reachable in production from an unenrolled project, an
    expired preview, a network blip on the raw authorized request, or a
    payload that parses with an empty ``tabs`` array.
    """
    service = Mock()
    service.documents.return_value.batchUpdate.return_value.execute = Mock(
        return_value={} if response is None else response
    )
    ga_document = (
        fx.build_doc([fx.paragraph(fx.run("Two bravo.\n"))])
        if document is None
        else document
    )

    def _get(**kwargs):
        if "commentsViewMode" in kwargs:
            raise preview_read.PreviewReadError("not enrolled")
        return Mock(execute=Mock(return_value=ga_document))

    service.documents.return_value.get.side_effect = _get
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


def _observe(*suggestions, complete: bool = True):
    """Seed the ledger the way a list_document_suggestions call would.

    ``complete`` is the listing read's coverage: True for the preview read
    (every tab, every segment), False for the GA fallback, which sees one
    unnamed body and cannot support any claim about the rest of the document.
    """
    suggestion_ledger.observe(EMAIL, DOC, list(suggestions), complete=complete)


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

        status = preview_status.get_status(EMAIL)
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

        status = preview_status.get_status(EMAIL)
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

        status = preview_status.get_status(EMAIL)
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

        status = preview_status.get_status(EMAIL)
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

        verification = result["verification"]
        assert verification["source"] == "skipped"
        assert verification["reason"] == "verify=false"
        assert _get_calls(service) == []
        # The documented keys are present and null rather than absent -- the
        # same rule the resolution path follows. ``created_suggestions`` is
        # null, NOT [], because created_suggestion_ids sits beside this block
        # and an empty echo would read as "the write created nothing".
        assert verification["created_suggestions"] is None
        assert verification["pending_suggestion_count"] is None
        assert verification["read_source"] is None
        (note,) = verification["notes"]
        assert "nothing verified this edit" in note
        assert "receipt for the REQUEST" in note

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
        assert "merge" in verification["notes"][0]


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
        suggestion_ledger.observe(EMAIL, CONC_DOC, [], complete=True)

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
        suggestion_ledger.observe(EMAIL, CONC_DOC, [], complete=True)

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

    @pytest.mark.asyncio
    async def test_a_degraded_listing_cannot_license_an_authorship_claim(self):
        """Source (2) is a subtraction, and both directions of it are bounded
        by what the read on that side could see.

        The "somebody else's card" branch already refused to describe itself
        against a degraded listing. The OURS branch did not check at all: a
        card of ours in any tab the degraded listing could not see is absent
        from ``before.ids``, ``me: true``, and went straight into
        ``created_suggestions`` -- the field whose docstring says this call
        made it. Nothing about that write created it; the previous read was
        simply blind. The API's own ``createdSuggestionIds`` is proof and is
        unaffected.
        """
        backend = FakeBackend(me="mockuser")
        backend.seed(CONC_SEED)
        # Ours, and already in the document before this call runs.
        preexisting = _other_editor_suggests(
            backend, editor="mockuser", text="OLD ", at=0
        )
        # The last listing degraded: one unnamed body, no tab ids, complete=False.
        suggestion_ledger.observe(EMAIL, CONC_DOC, [], complete=False)

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
        created = {s["suggestion_id"] for s in verification["created_suggestions"]}
        assert preexisting not in created, "pre-existing card claimed by this write"
        # What the API actually reported is still echoed, unchanged.
        assert created == set(result["created_suggestion_ids"])
        assert created, "the write's own suggestion must still be echoed"
        # And it is still reported -- just not as ours-by-this-call.
        appeared = {
            s["suggestion_id"] for s in verification["appeared_since_last_read"]
        }
        assert preexisting in appeared
        note = " ".join(verification["notes"])
        assert "may have been there all along" in note
        assert "names you as its author" in note
        assert "different author" not in note, note

    @pytest.mark.asyncio
    async def test_the_merge_note_does_not_delete_the_authorship_note(self):
        """Both are true of one write, so both have to survive it.

        ``verification["notes"] = [...]`` in the range-fallback branch
        overwrote the concurrent-authorship note appended just above it --
        on the merge path, which is precisely where a write comes back with
        no created id AND somebody else's card in the same read. The agent
        was left holding ``appeared_since_last_read`` with nothing saying it
        is not its own work.
        """
        _observe(
            {
                "suggestion_id": "s.mine",
                "type": "insertion",
                "pre_text": "",
                "post_text": "bold ",
                "segment": "body",
                "segment_id": None,
                "tab_id": "t.0",
                "start_index": 17,
                "end_index": 22,
            }
        )
        document = fx.build_doc(
            [
                fx.paragraph(
                    fx.run("The "),
                    fx.run("URGENT ", ins=["s.bob"]),
                    fx.run("brave"),
                    fx.run("bold ", ins=["s.mine"]),
                    fx.run("plan.\n"),
                )
            ]
        )
        service = _batch_service(
            # A merged edit: the API reports no created id at all.
            {},
            document=fx.build_tabs_payload(
                [("t.0", document)],
                suggestions=[
                    thread_for("s.mine"),
                    thread_for("s.bob", me=False, display_name="Bob"),
                ],
            ),
        )
        fn = _unwrap(write_tools.suggest_doc_edit)

        result = json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=17,
                text="bold ",
            )
        )

        verification = result["verification"]
        assert [
            s["suggestion_id"] for s in verification["appeared_since_last_read"]
        ] == ["s.bob"]
        assert [
            s["suggestion_id"] for s in verification["suggestions_at_edit_range"]
        ] == ["s.mine"]
        notes = verification["notes"]
        assert len(notes) == 2, notes
        assert any("Check the author before acting on it" in n for n in notes), notes
        assert any("merges into it" in n for n in notes), notes

    @pytest.mark.asyncio
    async def test_the_ambiguous_tab_note_does_not_delete_it_either(self):
        """The other assignment: a multi-tab document with no tab_id."""
        suggestion_ledger.observe(EMAIL, DOC, [], complete=True)
        service = _batch_service(
            {},
            document=fx.build_tabs_payload(
                [("t.0", fx.DOC_PLAIN_INSERTION), ("t.second", fx.DOC_SECOND_TAB)],
                suggestions=[
                    thread_for("suggest.ins1", me=False, display_name="Bob"),
                    thread_for("suggest.tab2", me=False, display_name="Bob"),
                ],
            ),
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

        verification = result["verification"]
        assert "suggestions_at_edit_range_unavailable" in verification
        notes = verification["notes"]
        assert len(notes) == 2, notes
        assert any("Check the author before acting on it" in n for n in notes), notes
        assert any("named no tab_id" in n for n in notes), notes

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
        assert "last marked character" in note
        # Rung 2 is observed-not-proven: a concurrent editor removing it in
        # the same window is indistinguishable from the GC rule firing.
        assert "observed, not proven" in note
        assert "another editor" in note

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

        verification = result["verification"]
        assert verification["source"] == "skipped"
        assert _get_calls(service) == []
        # The block keeps its documented shape. It returned five keys, while
        # the Returns block documents matches_expectation and friends
        # unconditionally -- so a client reading verification["matches_
        # expectation"] raised KeyError on exactly the path where nothing
        # checked the write, and one using .get could not tell "unknown" from
        # "this build does not report it".
        for key in (
            "read_source",
            "resolved_suggestion",
            "expected_text",
            "resulting_text",
            "matches_expectation",
            "pending_suggestion_count",
            "pending_suggestion_ids",
        ):
            assert key in verification, key
        assert verification["matches_expectation"] is None
        # Null, never 0: a count is a claim about the document, and no read
        # here supports one.
        assert verification["pending_suggestion_count"] is None
        # The one thing that IS known comes from this session's own listing.
        assert verification["resolved_suggestion"]["suggestion_id"] == "suggest.rep1"
        # And the value is inside the vocabulary the docstring enumerates.
        assert (
            verification["still_pending_unavailable"]
            in write_tools.STILL_PENDING_UNAVAILABLE_REASONS
        )
        # Remembered -- but offered as the likely cause, not a proven one.
        # ``verify=false`` bought exactly one thing: nothing looked at the
        # document, so nothing here saw the resolution take effect.
        explanation = suggestion_ledger.explain_missing(EMAIL, DOC, "suggest.rep1")
        assert "accept" in explanation
        assert "nothing here verified" in explanation
        assert "rather than a proven one" in explanation
        assert "You accepted it yourself" not in explanation, explanation

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


class TestVerificationIsNotDecidedByTheRepresentation:
    """A verdict must be a statement about the DOCUMENT, not about markers.

    ``_PostWriteRead`` used to keep ``render_document(...)["body_text"]`` --
    CriticMarkup-marked text, ``{-deleted-}`` / ``{+inserted+}`` -- while
    every value compared against it (``pre_text``, ``post_text``,
    ``context_before``) is BASE text, which carries no markers at all.
    Nothing stripped them, so the comparison ran between two different
    representations of the same document and each of its three failure modes
    is reachable from prod:

    1. **fail-open on the destructive path.** Prod splits a ``textRun`` at
       every style boundary (verified against the live API 2026-07-31:
       suggesting the deletion of "brave new" across a bold seam yields TWO
       deletion-marked runs), so the struck text renders ``{-brave-}{- new-}``
       and the base string "brave new" is not in it. The accept check is "is
       the struck text gone", and not-found was taken as gone:
       ``matches_expectation: true`` on a write that had NOT landed.
    2. **false alarm on the constructive path.** A still-pending neighbouring
       suggestion inside the resolved range renders a marker inside the text
       the accept promised, so ``expected_text in resulting_text`` was False
       about a write that landed perfectly -- which an agent may "fix" by
       re-suggesting into a customer document.
    3. **a fabricated diagnosis.** A marker inside the 40-character anchor
       made ``find(anchor)`` return -1, reported as ``anchor_not_found``,
       whose note asserts "the likeliest cause is a concurrent edit by
       another editor". Nobody edited anything.

    None of it is reproducible on mockdocs' coalescing alone, which is why
    ``mockdocs`` also grew style-split runs (see
    tests/mockdocs/test_tabs_and_segments.py).
    """

    @staticmethod
    def _record(**overrides) -> dict:
        record = {
            "suggestion_id": "s.1",
            "type": "deletion",
            "segment": "body",
            "segment_id": None,
            "tab_id": "t.0",
            "start_index": 7,
            "end_index": 16,
            "summary_text": "edit",
            "status": "OPEN",
        }
        record.update(overrides)
        return record

    @staticmethod
    async def _resolve(action: str, record: dict, document: dict) -> dict:
        _observe(record)
        sid = record["suggestion_id"]
        service = _batch_service(
            {"suggestionResponses": [{f"{action}edSuggestionIds": [sid]}]},
            document=document,
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

    # -- 1. the style-split (multi-run) deletion --------------------------

    #: What prod returns for "delete 'brave new'" when "brave" is bold and
    #: " new" is not: ONE suggestion id across TWO deletion-marked runs.
    STYLE_SPLIT_DELETION = fx.build_tabs_payload(
        [
            (
                "t.0",
                fx.build_doc(
                    [
                        fx.paragraph(
                            fx.run("Hello "),
                            fx.run("brave", dels=["s.1"]),
                            fx.run(" new", dels=["s.1"]),
                            fx.run(" world.\n"),
                        )
                    ]
                ),
            )
        ]
    )
    #: The same document once the accept landed.
    STYLE_SPLIT_ACCEPTED = fx.build_tabs_payload(
        [("t.0", fx.build_doc([fx.paragraph(fx.run("Hello  world.\n"))]))]
    )
    MULTI_RUN_RECORD = dict(
        pre_text="brave new",
        post_text="",
        context_before="Hello ",
        context_after=" world.\n",
    )

    @pytest.mark.asyncio
    async def test_a_multi_run_deletion_that_did_not_land_is_flagged(self):
        """The fail-open regression: this reported True with no check run.

        The struck text renders ``{-brave-}{- new-}``, so searching the
        rendered string for the base text "brave new" fails -- and failing to
        find the text an accept was supposed to remove was the evidence that
        it had been removed.
        """
        verification = await self._resolve(
            "accept",
            self._record(**self.MULTI_RUN_RECORD),
            self.STYLE_SPLIT_DELETION,
        )
        assert verification["matches_expectation"] is False, verification
        assert "brave new" in verification["resulting_text"], verification

    @pytest.mark.asyncio
    async def test_a_multi_run_deletion_that_landed_is_true(self):
        verification = await self._resolve(
            "accept",
            self._record(**self.MULTI_RUN_RECORD),
            self.STYLE_SPLIT_ACCEPTED,
        )
        assert verification["matches_expectation"] is True, verification
        assert "brave" not in verification["resulting_text"], verification

    @pytest.mark.asyncio
    async def test_rejecting_a_multi_run_deletion_keeps_the_text(self):
        """Reject expects the struck text BACK, unmarked and unsplit."""
        verification = await self._resolve(
            "reject",
            self._record(**self.MULTI_RUN_RECORD),
            fx.build_tabs_payload(
                [
                    (
                        "t.0",
                        fx.build_doc(
                            [fx.paragraph(fx.run("Hello brave new world.\n"))]
                        ),
                    )
                ]
            ),
        )
        assert verification["expected_text"] == "brave new"
        assert verification["matches_expectation"] is True, verification

    # -- 2. a still-pending neighbour inside the resolved range ------------

    @pytest.mark.asyncio
    async def test_a_pending_neighbour_inside_the_range_is_not_a_failure(self):
        """Accepting an insertion that brackets somebody else's pending one.

        ``s.1``'s post_text is "NEW-AMORE-A"; ``s.2``'s insertion sits
        between its two runs and stays pending, so the rendered text reads
        ``NEW-A{+XX+}MORE-A`` and the substring check said the accept had not
        landed. Base text has no markers and no such disagreement.
        """
        record = self._record(
            suggestion_id="s.1",
            type="insertion",
            pre_text="",
            post_text="NEW-AMORE-A",
            context_before="Start ",
            context_after=" end.\n",
        )
        verification = await self._resolve(
            "accept",
            record,
            fx.build_tabs_payload(
                [
                    (
                        "t.0",
                        fx.build_doc(
                            [
                                fx.paragraph(
                                    fx.run("Start "),
                                    fx.run("NEW-A"),
                                    fx.run("XX", ins=["s.2"]),
                                    fx.run("MORE-A"),
                                    fx.run(" end.\n"),
                                )
                            ]
                        ),
                    )
                ],
                suggestions=[thread_for("s.2")],
            ),
        )
        assert verification["matches_expectation"] is True, verification
        assert verification["still_pending"] is False
        assert verification["pending_suggestion_ids"] == ["s.2"]

    # -- 3. a marker inside the 40-character anchor ------------------------

    @pytest.mark.asyncio
    async def test_a_pending_neighbour_in_the_anchor_does_not_fabricate_a_cause(self):
        """Two pending cards ~11 characters apart; one is accepted.

        ``s.2``'s pending insertion falls inside ``s.1``'s ``context_before``
        window, so the rendered text reads ``one {+INSERTED-B +}two `` and
        ``find("one two ")`` returned -1. The tool then reported
        ``anchor_not_found`` and a note asserting a concurrent edit by another
        editor -- a diagnosis with nothing behind it, on a write that landed.
        """
        record = self._record(
            suggestion_id="s.1",
            type="deletion",
            pre_text="three",
            post_text="",
            context_before="one two ",
            context_after=" four.\n",
        )
        verification = await self._resolve(
            "accept",
            record,
            fx.build_tabs_payload(
                [
                    (
                        "t.0",
                        fx.build_doc(
                            [
                                fx.paragraph(
                                    fx.run("one "),
                                    fx.run("INSERTED-B ", ins=["s.2"]),
                                    fx.run("two "),
                                    fx.run(" four.\n"),
                                )
                            ]
                        ),
                    )
                ],
                suggestions=[thread_for("s.2")],
            ),
        )
        assert verification["matches_expectation"] is True, verification
        assert "resulting_text_unavailable" not in verification, verification
        assert "notes" not in verification, verification

    # -- the null verdict always names its reason --------------------------

    @pytest.mark.asyncio
    async def test_a_record_with_no_before_or_after_says_why_it_cannot_check(self):
        """MEDIUM: ``matches`` stayed null with ``unlocated`` null too.

        The docstring guarantees a null verdict always carries
        ``resulting_text_unavailable``; a record carrying neither pre_text nor
        post_text produced a bare null and broke it.
        """
        verification = await self._resolve(
            "accept",
            self._record(suggestion_id="s.1", context_before="", context_after=""),
            EMPTY_READ,
        )
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "nothing_to_compare"
        assert "list_document_suggestions" in verification["notes"][0]

    @pytest.mark.asyncio
    async def test_an_anchor_that_repeats_and_disagrees_gets_no_verdict(self):
        """A full-width anchor occurring twice, resolved at one of them.

        ``find`` took the first occurrence and answered about it. Two places
        read as the range and they disagree, so no verdict is reported --
        never a guess on the destructive path.
        """
        anchor = "A" * 40
        record = self._record(
            suggestion_id="s.1",
            type="deletion",
            pre_text="gone",
            post_text="",
            context_before=anchor,
            context_after="B" * 40,
        )
        body = anchor + "B" * 40 + anchor + "gone" + "B" * 40 + "\n"
        verification = await self._resolve(
            "accept",
            record,
            fx.build_tabs_payload(
                [("t.0", fx.build_doc([fx.paragraph(fx.run(body))]))]
            ),
        )
        assert verification["matches_expectation"] is None, verification
        assert verification["resulting_text_unavailable"] == "ambiguous_anchor"
        assert "repeats" in verification["notes"][0]

    @pytest.mark.asyncio
    async def test_the_mock_can_now_build_the_prod_payload_end_to_end(self):
        """The same case again, but through mockdocs rather than a fixture.

        The mock coalesced runs by mark set alone, so it could not produce a
        style-split deletion and this whole class was invisible to it. With
        ``Char.style`` it can: seed the bold span, suggest the deletion across
        the seam, accept it, and the tool must verify the accept against a
        document whose reviewer view reads ``{-brave-}{- new-}``.
        """
        backend = FakeBackend(me="mockuser")
        backend.seed(
            {
                "me": "mockuser",
                "documents": [
                    {
                        "document_id": "split-doc",
                        "text": "Hello brave new world.\n",
                        "suggestions": [
                            {"op": "style", "start": 6, "end": 11, "flags": ["bold"]},
                            {"op": "delete", "start": 6, "end": 15},
                        ],
                    }
                ],
            }
        )
        service = backend.docs_service()

        listing = json.loads(
            await _unwrap(curated_tools.list_document_suggestions)(
                service,
                user_google_email=EMAIL,
                document_id="split-doc",
                fields="full",
            )
        )
        (card,) = listing["suggestions"]
        assert card["pre_text"] == "brave new"

        view = json.loads(
            await _unwrap(curated_tools.get_doc_review_view)(
                service, user_google_email=EMAIL, document_id="split-doc"
            )
        )
        # The payload really is split: one card, two marked spans.
        assert view["body_text"] == "Hello {-brave-}{- new-} world.\n"

        result = json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service,
                user_google_email=EMAIL,
                document_id="split-doc",
                action="accept",
                suggestion_id=card["suggestion_id"],
            )
        )
        verification = result["verification"]
        assert verification["still_pending"] is False
        assert verification["matches_expectation"] is True, verification
        assert verification["resulting_text"] == "Hello  world.\n"

    def test_a_rendered_string_cannot_reach_the_check(self):
        """The type is the fix: marked text is a TypeError, not a wrong bool."""
        with pytest.raises(TypeError, match="segment_base_texts"):
            analysis.check_resolution(
                "Hello {-brave-}{- new-} world.\n",
                context_before="Hello ",
                context_after=" world.\n",
                expected_text="",
                removed_text="brave new",
            )
        with pytest.raises(TypeError, match="minted only by"):
            analysis.BaseText("Hello world.\n")


class TestVerificationIsNotDecidedByTheEchoClip:
    """A verdict must be a statement about the document, not about the echo.

    The post-write window used to be clipped to ``ECHO_MAX_CHARS`` (200)
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
            # ``requested_range``, not ``anchored_range``: the numbers are
            # what this call ASKED for. ``anchored_text`` is the only evidence
            # about where the comment actually landed.
            "requested_range": {
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
        assert "does not exist" in message
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
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=0,
                text="X",
            )
        json.loads(
            await fn(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=0,
                text="X",
                segment_id="kix.h1",
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
                        fx.run("Head "),
                        fx.run("edit", ins=["suggest.hdr1"]),
                        fx.run("\n"),
                    )
                ]
            },
        )
        service = _batch_service(
            {}, document=fx.build_tabs_payload([("t.0", document)])
        )
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
    [
        fx.paragraph(
            fx.run("Good "),
            fx.run("morning", dels=["suggest.rep1"]),
            fx.run("evening", ins=["suggest.rep1"]),
            fx.run("\n"),
        )
    ],
    headers={
        "kix.h1": [
            fx.paragraph(fx.run("DRAFT", ins=["suggest.hdr1"]), fx.run(" header\n"))
        ]
    },
)

#: Two tabs, each with a suggestion on the SAME local numbers.
TWO_TAB_READ = fx.build_tabs_payload(
    [
        (
            "t.0",
            fx.build_doc(
                [
                    fx.paragraph(
                        fx.run("One "),
                        fx.run("alpha", ins=["suggest.t0"]),
                        fx.run(".\n"),
                    )
                ]
            ),
        ),
        (
            "t.second",
            fx.build_doc(
                [
                    fx.paragraph(
                        fx.run("Two "),
                        fx.run("bravo", ins=["suggest.t1"]),
                        fx.run(".\n"),
                    )
                ]
            ),
        ),
    ]
)

#: The same two tabs after ``suggest.t1`` was accepted: the card is gone from
#: the pending set, so the only open question is what the text now reads --
#: which is the question an ambiguous tab leaves unanswerable. (Reusing
#: TWO_TAB_READ here made the resolved id STILL PENDING in the post-write
#: read, which is a decided "the write did not land" and not an unlocated
#: window at all.)
TWO_TAB_READ_RESOLVED = fx.build_tabs_payload(
    [
        (
            "t.0",
            fx.build_doc(
                [
                    fx.paragraph(
                        fx.run("One "),
                        fx.run("alpha", ins=["suggest.t0"]),
                        fx.run(".\n"),
                    )
                ]
            ),
        ),
        ("t.second", fx.build_doc([fx.paragraph(fx.run("Two bravo.\n"))])),
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
        suggestion_ledger.observe(EMAIL, DOC, [REP1_RECORD], complete=True)
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
        {
            "suggestion_id": "b.t0",
            "segment": "body",
            "segment_id": None,
            "tab_id": "t.0",
            "start_index": 5,
            "end_index": 9,
        },
        {
            "suggestion_id": "h.t0",
            "segment": "header",
            "segment_id": "kix.h1",
            "tab_id": "t.0",
            "start_index": 5,
            "end_index": 9,
        },
        {
            "suggestion_id": "b.t1",
            "segment": "body",
            "segment_id": None,
            "tab_id": "t.second",
            "start_index": 5,
            "end_index": 9,
        },
    ]

    #: The DOCUMENT's tabs. ``RECORDS`` occupy only two of the three.
    TAB_IDS = ("t.0", "t.second", "t.third")

    @staticmethod
    def _read_side(records, *, tab_ids, segment_id, tab_id):
        """The listing's range filter: ``("refused", why)`` or ``("ok", ids)``."""
        try:
            kept, _ = review_page.filter_records(
                records,
                tab_ids=tab_ids,
                start_index=0,
                end_index=10_000,
                segment_id=segment_id,
                tab_id=tab_id,
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
    """HIGH 2: the check used to search the merged body text of every tab.
    A suggestion at the very start of a header has ``context_before == ""``
    (analysis.py), so the empty anchor returned the head of the BODY and
    ``matches_expectation`` for a header resolution was computed there. The
    window now comes from ``segment_base_texts``, keyed by ``(tab, segment)``
    -- there is no merged string to search."""

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
        honest window to return.

        The accept LANDED (``suggest.t1`` is gone from the pending set), so
        the verdict is genuinely open and the missing window is the only
        thing wrong -- a read in which it was still pending would be a
        decided false, not an unlocated window.
        """
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
            document=TWO_TAB_READ_RESOLVED,
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
        assert verification["still_pending"] is False
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
        assert "`still_pending` is the evidence" in note
        # The end state is still answered.
        assert verification["still_pending"] is False

    @pytest.mark.asyncio
    async def test_an_ambiguous_tab_says_which_tabs(self):
        """The card carried no tab_id and the accept LANDED (it is gone from
        the pending set), so the only open question is the text -- which a
        two-tab read cannot answer without the tab."""
        _observe({**self.RECORD, "tab_id": None})
        verification = await self._accept(
            _batch_service({}, document=TWO_TAB_READ_RESOLVED)
        )
        assert verification["still_pending"] is False
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
        verification = await self._accept(_degraded_read_service())
        assert verification["read_source"] == "ga_documents_get"
        assert verification["matches_expectation"] is None
        assert verification["resulting_text_unavailable"] == "segment_not_in_read"
        note = next(n for n in verification["notes"] if "could not be located" in n)
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
                still_pending=False,
            )
            assert "s.1" in note or "post-write read" in note
            assert "`still_pending` is the evidence" in note

    def test_no_note_calls_a_null_still_pending_the_evidence(self):
        """Round 6: every sentence above ends by pointing the agent at
        ``still_pending`` as the evidence about the write -- which is exactly
        the field a blind read had no standing to fill in. When it is null
        there is nothing to point at, and saying otherwise is the same
        unfounded assertion one indirection further out."""
        read = Mock(tab_ids=["t.0", "t.second"], source="ga_documents_get")
        for reason in write_tools.UNLOCATED_REASONS:
            note = write_tools._unlocated_note(
                reason,
                suggestion_id="s.1",
                record=self.RECORD,
                read=read,
                still_pending=None,
            )
            assert "`still_pending` is the evidence" not in note, reason
            assert "NOTHING in this response reports" in note, reason
            assert "still_pending_unavailable" in note, reason

    def test_every_reason_has_its_OWN_note(self):
        """Round 5 MEDIUM: the fallback answered every unmatched reason with
        the ``anchor_not_found`` sentence.

        ``_unlocated_note`` ended in an unguarded ``return`` of the
        anchor-miss diagnosis -- "the likeliest cause is a concurrent edit by
        another editor" -- so a reason added to ``UNLOCATED_REASONS`` without
        a sentence of its own did not go quiet, it acquired a confident and
        wrong story about somebody else editing the document. Asserting a
        note *exists* per reason (the test above) did not catch that, because
        the fallback is a note.
        """
        read = Mock(tab_ids=["t.0", "t.second"], source="preview_threads")
        notes = {
            reason: write_tools._unlocated_note(
                reason,
                suggestion_id="s.1",
                record=self.RECORD,
                read=read,
                still_pending=False,
            )
            for reason in write_tools.UNLOCATED_REASONS
        }
        assert len(set(notes.values())) == len(write_tools.UNLOCATED_REASONS), notes

    def test_a_reason_nobody_wrote_a_sentence_for_gets_no_diagnosis(self, caplog):
        """An unknown code is a bug in this module, not a concurrent edit."""
        read = Mock(tab_ids=["t.0"], source="preview_threads")
        with caplog.at_level(logging.ERROR, logger="gdocs_preview.write_tools"):
            note = write_tools._unlocated_note(
                "reason_from_the_future",
                suggestion_id="s.1",
                record=self.RECORD,
                read=read,
                still_pending=False,
            )
        assert "reason_from_the_future" in note
        assert "concurrent edit" not in note
        assert "reason_from_the_future" in caplog.text

    def test_the_returns_contract_lists_the_reasons_the_code_can_send(self):
        """Round 5 MEDIUM: the agent-facing docstring named FOUR of six.

        ``ambiguous_anchor`` and ``nothing_to_compare`` were reachable and
        undocumented, so the only description of the tool an agent ever reads
        was missing two of the values it can receive. The enumeration is
        parsed back out of the docstring here: adding a reason without
        documenting it, or documenting them in a different order from
        ``UNLOCATED_REASONS``, fails.
        """
        doc = _unwrap(write_tools.manage_document_suggestion).__doc__
        match = re.search(
            r"names which of the (\w+)\s+reasons\s+it was:(.*)", doc, re.S
        )
        assert match, doc
        assert match.group(1) == _NUMBER_WORDS[len(write_tools.UNLOCATED_REASONS)]
        listed = re.findall(r"([a-z]+(?:_[a-z]+)+) \(", match.group(2))
        assert listed == list(write_tools.UNLOCATED_REASONS), listed


class TestAStillPendingSuggestionIsNeverAMatch:
    """Round 5 HIGH: a resolution that did not land reported success.

    ``still_pending`` (is the id in the post-write pending set?) and
    ``matches_expectation`` (does the range read what the card promised?)
    used to be written into the response independently, and for a REJECT
    they answer two different questions with only one of them informative:
    base text is IDENTICAL whether or not a reject took effect -- the
    suggestion's insertion is stripped from base text either way
    (``analysis._collect_segments``), its deletion kept either way -- so
    ``check_resolution`` answers ``True`` in both worlds. The API's own
    documented HTTP-200-no-op therefore emitted ``{"still_pending": true,
    "matches_expectation": true}``: a positive verdict standing beside the
    structural evidence that contradicts it, with no note.

    The accept half had the same hole for a style-only suggestion, whose
    ``pre_text`` and ``post_text`` are the same string (see the guard in
    ``analysis.check_resolution``), so this is not a reject-only patch: the
    verdict is now DERIVED from pending-set membership plus the text check
    (``write_tools._ResolutionVerdict``), and the two cannot disagree.
    """

    @staticmethod
    async def _resolve(action: str, record: dict, document: dict) -> dict:
        """Resolve ``record`` against a post-write read that did NOT change.

        The batchUpdate answers with no ``suggestionResponses`` at all, which
        is the shape of the API's 200-no-op.
        """
        _observe(record)
        service = _batch_service({}, document=document)
        result = json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action=action,
                suggestion_id=record["suggestion_id"],
            )
        )
        return result["verification"]

    #: "Hello world.\n" with " brave" suggested-INSERTED and still pending.
    INSERTION_UNCHANGED = fx.build_tabs_payload(
        [("t.0", fx.DOC_PLAIN_INSERTION)], suggestions=[thread_for("suggest.ins1")]
    )
    INSERTION_RECORD = {
        "suggestion_id": "suggest.ins1",
        "type": "insertion",
        "pre_text": "",
        "post_text": " brave",
        "context_before": "Hello",
        "context_after": " world.\n",
        "segment": "body",
        "segment_id": None,
        "tab_id": "t.0",
        "start_index": 6,
        "end_index": 12,
    }

    #: "Hello cruel world.\n" with " cruel" suggested-DELETED, still pending.
    DELETION_UNCHANGED = fx.build_tabs_payload(
        [("t.0", fx.DOC_PLAIN_DELETION)], suggestions=[thread_for("suggest.del1")]
    )
    DELETION_RECORD = {
        "suggestion_id": "suggest.del1",
        "type": "deletion",
        "pre_text": " cruel",
        "post_text": "",
        "context_before": "Hello",
        "context_after": " world.\n",
        "segment": "body",
        "segment_id": None,
        "tab_id": "t.0",
        "start_index": 6,
        "end_index": 12,
    }

    #: "Plain styled text.\n" with "styled" restyled, still pending.
    STYLE_UNCHANGED = fx.build_tabs_payload(
        [("t.0", fx.DOC_STYLE)], suggestions=[thread_for("suggest.sty1")]
    )
    STYLE_RECORD = {
        "suggestion_id": "suggest.sty1",
        "type": "style",
        "pre_text": "styled",
        "post_text": "styled",
        "context_before": "Plain ",
        "context_after": " text.\n",
        "segment": "body",
        "segment_id": None,
        "tab_id": "t.0",
        "start_index": 7,
        "end_index": 13,
    }

    @pytest.mark.asyncio
    async def test_a_reject_of_an_insertion_that_did_not_land(self):
        """Rejecting an insertion promises the base text stays as it is --
        which it also does when nothing happened at all."""
        verification = await self._resolve(
            "reject", self.INSERTION_RECORD, self.INSERTION_UNCHANGED
        )
        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification

    @pytest.mark.asyncio
    async def test_a_reject_of_a_deletion_that_did_not_land(self):
        """The mirror: rejecting a deletion promises the struck text stays,
        and a deletion is kept in base text whether or not it was rejected."""
        verification = await self._resolve(
            "reject", self.DELETION_RECORD, self.DELETION_UNCHANGED
        )
        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification

    @pytest.mark.asyncio
    async def test_an_accept_of_a_style_only_suggestion_that_did_not_land(self):
        """The accept half of the same hole: a style-only suggestion's
        before and after text are one string, so the text check is
        content-free and answered True on a write that never happened."""
        verification = await self._resolve(
            "accept", self.STYLE_RECORD, self.STYLE_UNCHANGED
        )
        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification

    @pytest.mark.asyncio
    async def test_an_accept_that_did_not_land(self):
        """The case that already worked, kept as the control: for an accept
        of a text-changing suggestion the base text DOES separate the two
        worlds, and the verdict must not regress to null."""
        verification = await self._resolve(
            "accept", self.INSERTION_RECORD, self.INSERTION_UNCHANGED
        )
        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification

    @pytest.mark.asyncio
    async def test_a_resolution_that_did_not_land_is_not_filed_as_one(self):
        """The verdict this call reported and the memory it left behind have
        to be the same claim.

        ``still_pending: true`` says the write did not take effect. The ledger
        was told "resolved" regardless, so a later "does not exist" for that
        id came back "You accepted it yourself at <t>; resolving a suggestion
        removes it" -- causation asserted from the single piece of evidence
        that contradicts it, sending the agent away from the real cause.
        """
        verification = await self._resolve(
            "accept", self.INSERTION_RECORD, self.INSERTION_UNCHANGED
        )
        assert verification["still_pending"] is True

        explanation = suggestion_ledger.explain_missing(
            EMAIL, DOC, self.INSERTION_RECORD["suggestion_id"]
        )
        assert "You accepted it yourself" not in explanation, explanation
        assert "still listed it as pending" in explanation

    @pytest.mark.asyncio
    async def test_the_note_says_the_pending_set_is_what_decided_it(self):
        verification = await self._resolve(
            "reject", self.INSERTION_RECORD, self.INSERTION_UNCHANGED
        )
        (note,) = verification["notes"]
        assert "suggest.ins1" in note
        assert "pending set" in note
        assert "list_document_suggestions" in note

    @pytest.mark.asyncio
    async def test_a_reject_that_did_land_is_still_true(self):
        """The fix must not make every reject unverifiable: with the card
        gone from the pending set the text check decides, as before."""
        verification = await self._resolve(
            "reject",
            self.INSERTION_RECORD,
            fx.build_tabs_payload(
                [("t.0", fx.build_doc([fx.paragraph(fx.run("Hello world.\n"))]))]
            ),
        )
        assert verification["still_pending"] is False, verification
        assert verification["matches_expectation"] is True, verification
        assert "notes" not in verification, verification

    def test_the_two_fields_cannot_be_assembled_into_a_contradiction(self):
        """The design-level half: not "we remembered to check" but "the pair
        has one producer and it cannot emit that pair"."""
        for text_check in (True, False, None):
            verdict = write_tools._ResolutionVerdict.derive(
                still_pending=True, text_check=text_check
            )
            assert verdict.matches_expectation is False
            verdict = write_tools._ResolutionVerdict.derive(
                still_pending=False, text_check=text_check
            )
            assert verdict.matches_expectation is text_check
        with pytest.raises(ValueError, match="matches_expectation"):
            write_tools._ResolutionVerdict(
                still_pending=True, text_check=True, matches_expectation=True
            )
        with pytest.raises(ValueError, match="matches_expectation"):
            write_tools._ResolutionVerdict(
                still_pending=True, text_check=None, matches_expectation=None
            )


# ---------------------------------------------------------------------------
# Round 6 -- evidence that cannot bear on the question answers UNKNOWN
# ---------------------------------------------------------------------------

#: A card in the SECOND tab of a two-tab document, as a listing reports it.
T1_RECORD = {
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


class TestAReadThatCannotSeeTheTabSaysNothingAboutIt:
    """Round 6 HIGH 1: ``still_pending`` was a subtraction, not an observation.

    ``still_pending=suggestion_id in read.records`` treats absence as proof
    the resolution landed. On a degraded post-write read -- the GA
    ``documents.get`` fallback, which returns one unnamed body and no tab ids
    at all -- a card in ``t.0`` or ``t.second`` is absent because the read is
    BLIND, and the answer came out ``still_pending: false`` on the destructive
    accept path. Round 5 made the contradictory PAIR unrepresentable; the
    inputs to the derivation were still free to be unfounded, and the
    derivation faithfully turned one into a confident output.

    Worse, the surrounding prose pointed the agent AT that field:
    ``_NOT_A_FAILED_WRITE`` ("`still_pending` is the evidence about whether
    the resolution itself took effect") shipped in the very note that fires
    on this path.
    """

    @staticmethod
    async def _accept(service, suggestion_id="suggest.t1") -> dict:
        result = json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id=suggestion_id,
            )
        )
        return result["verification"]

    @pytest.mark.asyncio
    async def test_a_blind_read_does_not_report_the_resolution_as_landed(self):
        _observe(T1_RECORD)
        verification = await self._accept(_degraded_read_service())

        assert verification["read_source"] == "ga_documents_get"
        assert verification["still_pending"] is None, verification
        assert verification["still_pending_unavailable"] == "segment_not_in_read"
        assert verification["matches_expectation"] is None, verification

    @pytest.mark.asyncio
    async def test_the_note_says_the_absence_is_not_evidence(self):
        _observe(T1_RECORD)
        verification = await self._accept(_degraded_read_service())

        note = next(n for n in verification["notes"] if "UNKNOWN" in n)
        assert "suggest.t1" in note
        assert "t.second" in note
        assert "NOT evidence" in note
        assert "list_document_suggestions" in note

    @pytest.mark.asyncio
    async def test_no_note_still_points_at_still_pending_as_the_evidence(self):
        """The aggravating half: the prose that shipped beside the bad field
        told the agent to trust it."""
        _observe(T1_RECORD)
        verification = await self._accept(_degraded_read_service())

        for note in verification["notes"]:
            assert "`still_pending` is the evidence" not in note, note

    @pytest.mark.asyncio
    async def test_an_id_we_never_listed_is_unknown_on_a_blind_read_too(self):
        """No record means no address, so a read that did not cover the
        document cannot even name the space it failed to look in."""
        _observe(complete=False)
        verification = await self._accept(
            _degraded_read_service(), suggestion_id="never.listed"
        )

        assert verification["still_pending"] is None, verification
        assert verification["still_pending_unavailable"] == "read_incomplete"
        note = next(n for n in verification["notes"] if "UNKNOWN" in n)
        assert "which tab it lives in" in note

    @pytest.mark.asyncio
    async def test_presence_is_decisive_whatever_the_read_saw(self):
        """The asymmetry that makes this safe: a read that LISTED the id has
        observed it, so a still-pending card is still a decided false -- only
        absence needs coverage behind it."""
        _observe({**T1_RECORD, "tab_id": None})
        verification = await self._accept(
            _degraded_read_service(
                document=fx.build_doc(
                    [
                        fx.paragraph(
                            fx.run("Two "),
                            fx.run("bravo", ins=["suggest.t1"]),
                            fx.run(".\n"),
                        )
                    ]
                )
            )
        )

        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification
        assert "still_pending_unavailable" not in verification

    @pytest.mark.asyncio
    async def test_a_complete_read_still_answers_every_id(self):
        """The control: coverage is what licenses the answer, and the preview
        read has it -- for a card in the tab it walked and for one it never
        listed alike."""
        _observe(T1_RECORD)
        verification = await self._accept(
            _batch_service({}, document=TWO_TAB_READ_RESOLVED)
        )

        assert verification["read_source"] == "preview_threads"
        assert verification["still_pending"] is False, verification
        assert "still_pending_unavailable" not in verification
        assert verification["matches_expectation"] is True, verification

    @pytest.mark.asyncio
    async def test_an_ambiguous_tab_while_still_pending(self):
        """Round 6 also noted the gap: ``ambiguous_tab`` was only ever
        exercised on a resolution that HAD landed.

        The card carried no tab_id and the document has two, so no text
        comparison is possible -- but the id is right there in the pending set
        of a read that saw both tabs, so the verdict is a decided false and
        BOTH notes have something to say.
        """
        _observe({**T1_RECORD, "tab_id": None})
        verification = await self._accept(_batch_service({}, document=TWO_TAB_READ))

        assert verification["still_pending"] is True, verification
        assert verification["matches_expectation"] is False, verification
        assert verification["resulting_text_unavailable"] == "ambiguous_tab"
        assert "still_pending_unavailable" not in verification
        pending_note = next(n for n in verification["notes"] if "pending set" in n)
        tab_note = next(n for n in verification["notes"] if "2 tabs" in n)
        assert "did not take effect" in pending_note
        # The text note may still point at still_pending: it is populated.
        assert "`still_pending` is the evidence" in tab_note

    @pytest.mark.asyncio
    async def test_the_pending_count_says_it_is_only_what_was_seen(self):
        """My own sweep, same class one level up: ``pending_suggestion_count``
        and ``pending_suggestion_ids`` read as the DOCUMENT's pending set, and
        off a degraded read they are one unnamed body's. An agent that sees
        ``pending_suggestion_count: 0`` stops reviewing."""
        _observe(T1_RECORD)
        verification = await self._accept(_degraded_read_service())

        assert verification["pending_suggestion_count"] == 0
        assert verification["pending_suggestions_are_partial"] is True
        note = next(n for n in verification["notes"] if "count of 0" in n)
        assert "does NOT mean the document has no pending suggestions" in note

    @pytest.mark.asyncio
    async def test_a_complete_read_makes_no_such_disclaimer(self):
        _observe(T1_RECORD)
        verification = await self._accept(
            _batch_service({}, document=TWO_TAB_READ_RESOLVED)
        )
        assert "pending_suggestions_are_partial" not in verification

    def test_the_verdict_propagates_unknown_rather_than_collapsing_it(self):
        for text_check in (True, False, None):
            verdict = write_tools._ResolutionVerdict.derive(
                still_pending=None, text_check=text_check
            )
            assert verdict.matches_expectation is None, text_check
        # A positive verdict requires the pending set to have been OBSERVED.
        with pytest.raises(ValueError, match="matches_expectation"):
            write_tools._ResolutionVerdict(
                still_pending=None, text_check=True, matches_expectation=True
            )
        with pytest.raises(ValueError, match="matches_expectation"):
            write_tools._ResolutionVerdict(
                still_pending=None, text_check=False, matches_expectation=False
            )
        assert write_tools._ResolutionVerdict.unknown().matches_expectation is None

    def test_a_derivation_failure_never_fails_a_landed_write(self, caplog):
        """Round 6 MEDIUM 6: ``__post_init__`` raising on the post-write path
        turns a destructive write that LANDED into a tool error, which is
        exactly what ``_post_write_read`` refuses to do. The raise stays for
        direct construction; the call site degrades to the verdict that claims
        nothing."""
        assert write_tools._ResolutionVerdict.unknown().still_pending is None
        with caplog.at_level(logging.ERROR, logger="gdocs_preview.write_tools"):
            with pytest.raises(ValueError):
                write_tools._ResolutionVerdict(
                    still_pending=False, text_check=True, matches_expectation=False
                )


class TestCollateralIsOnlyClaimedWhereItWasObserved:
    """Round 6 HIGH 2: the collateral diff ran against the same blind read.

    ``known_before - read.live_ids`` subtracts a multi-tab ledger from a read
    that can see one unnamed body, so every live suggestion in every other tab
    falls out of it -- and each was reported as ``also_removed_suggestion_ids``
    with ``collateral_note``'s "accepting 'Y' also removed it, because that
    removed the last character it marked. Its comment thread went with it."
    That is fabricated causation about a customer's document, and it is
    DURABLE: ``record_resolution`` pops those ids, so ``explain_missing``
    repeats the claim for the rest of the session.
    """

    #: The ledger a multi-tab listing leaves behind: one card per tab.
    LEDGER = (REP1_RECORD, T1_RECORD)

    @staticmethod
    async def _accept(service, suggestion_id="suggest.rep1") -> dict:
        result = json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="accept",
                suggestion_id=suggestion_id,
            )
        )
        return result["verification"]

    @pytest.mark.asyncio
    async def test_a_blind_read_reports_no_collateral_at_all(self):
        _observe(*self.LEDGER)
        verification = await self._accept(
            _degraded_read_service(
                {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
                document=fx.build_doc([fx.paragraph(fx.run("Good evening\n"))]),
            )
        )

        assert "also_removed_suggestion_ids" not in verification, verification
        for note in verification.get("notes", []):
            assert "last character it marked" not in note, note

    @pytest.mark.asyncio
    async def test_it_says_the_collateral_is_unknown_rather_than_empty(self):
        """ "No collateral" and "we could not look" are different answers, and
        the agent's next move differs."""
        _observe(*self.LEDGER)
        verification = await self._accept(
            _degraded_read_service(
                {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
                document=fx.build_doc([fx.paragraph(fx.run("Good evening\n"))]),
            )
        )

        assert verification["also_removed_suggestion_ids_unavailable"] == (
            "read_incomplete"
        )
        note = next(n for n in verification["notes"] if "suggest.t1" in n)
        assert "UNKNOWN" in note
        assert "nothing has been recorded against" in note

    @pytest.mark.asyncio
    async def test_the_fabricated_cause_does_not_outlive_the_response(self):
        """The durable half. ``record_resolution`` pops the ids it is told
        about, so an unfounded collateral claim is repeated by every later
        "that id does not exist" error."""
        _observe(*self.LEDGER)
        await self._accept(
            _degraded_read_service(
                {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
                document=fx.build_doc([fx.paragraph(fx.run("Good evening\n"))]),
            )
        )

        explanation = suggestion_ledger.explain_missing(EMAIL, DOC, "suggest.t1")
        # Rung 2 of the honesty ladder -- "it was listed before you accepted X
        # and gone from the read right after, so that accept removed it" -- is
        # the claim that must not be reachable from a read that never looked.
        assert "gone from the read right after" not in explanation, explanation
        assert "so that accept removed it" not in explanation, explanation
        # Rung 3 is fine, and is what it falls back to: we DID resolve
        # something here, and resolving can remove others. It says "MAY".
        assert "not proven" in explanation and "MAY" in explanation, explanation

    @pytest.mark.asyncio
    async def test_a_complete_read_still_reports_real_collateral(self):
        """The control: the whole point of the feature survives. A read that
        walked both tabs and found only one card DID observe the other's
        absence."""
        _observe(*self.LEDGER)
        verification = await self._accept(
            _batch_service(
                {"suggestionResponses": [{"acceptedSuggestionIds": ["suggest.rep1"]}]},
                document=ACCEPTED_READ,
            )
        )

        assert verification["also_removed_suggestion_ids"] == ["suggest.t1"]
        assert "also_removed_suggestion_ids_unavailable" not in verification
        assert any("last marked character" in n for n in verification["notes"])
        assert any("observed, not proven" in n for n in verification["notes"])

    @pytest.mark.asyncio
    async def test_the_suggest_path_diffs_the_same_way(self):
        """``_verify_suggest`` carries the twin of the same subtraction."""
        _observe(*self.LEDGER)
        result = json.loads(
            await _unwrap(write_tools.suggest_doc_edit)(
                _degraded_read_service(
                    {"suggestionResponses": [{"createdSuggestionIds": ["suggest.new"]}]}
                ),
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="x",
            )
        )

        verification = result["verification"]
        assert "also_removed_suggestion_ids" not in verification, verification
        assert verification["also_removed_suggestion_ids_unavailable"] == (
            "read_incomplete"
        )
        for note in verification.get("notes", []):
            assert "merges" not in note, note

    @pytest.mark.asyncio
    async def test_a_degraded_listing_does_not_make_a_card_newly_appeared(self):
        """Round 6 MEDIUM 7, the mirror direction: "appeared between the last
        listing and this write" is a claim about the LISTING, and a blind
        listing never saw the other tabs at all."""
        _observe(REP1_RECORD, complete=False)
        result = json.loads(
            await _unwrap(write_tools.suggest_doc_edit)(
                _batch_service({}, document=TWO_TAB_READ),
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="x",
            )
        )

        note = next(
            n for n in result["verification"]["notes"] if "NOT reported as created" in n
        )
        assert "appeared between the last listing" not in note, note
        assert "may have been there all along" in note


class TestAnUnverifiedResolutionSaysSo:
    """Round 6 HIGH 3: ``{"source": "skipped"}`` beside a populated id list.

    Both non-verified returns sit inside a response whose top level reads
    ``rejected_suggestion_ids: ["sug.x"]`` and ``comment_update_state:
    "ALL_SAVED"`` -- byte-for-byte the shape prod returns for a reject that
    resolved NOTHING. The warning that the response ids alone do not say the
    write landed fired only on the verified path, i.e. only where there was
    other evidence anyway.
    """

    @staticmethod
    async def _reject(service, **extra) -> dict:
        return json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                action="reject",
                suggestion_id="suggest.rep1",
                **extra,
            )
        )

    @pytest.mark.asyncio
    async def test_verify_false_warns_that_the_ids_are_not_evidence(self):
        _observe(REP1_RECORD)
        result = await self._reject(
            _batch_service(
                {
                    "suggestionResponses": [
                        {"rejectedSuggestionIds": ["suggest.rep1"]}
                    ],
                    "commentUpdateState": "ALL_SAVED",
                },
                document=ACCEPTED_READ,
            ),
            verify=False,
        )

        verification = result["verification"]
        assert result["rejected_suggestion_ids"] == ["suggest.rep1"]
        assert result["comment_update_state"] == "ALL_SAVED"
        assert verification["source"] == "skipped"
        assert verification["still_pending"] is None
        (note,) = verification["notes"]
        assert "nothing verified this reject" in note
        assert "rejected_suggestion_ids" in note
        assert "receipt for the REQUEST" in note
        assert "list_document_suggestions" in note

    @pytest.mark.asyncio
    async def test_a_failed_verification_read_warns_the_same_way(self):
        _observe(REP1_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"rejectedSuggestionIds": ["suggest.rep1"]}]},
        )
        service.documents.return_value.get.side_effect = RuntimeError("read exploded")

        verification = (await self._reject(service))["verification"]

        assert verification["source"] == "unavailable"
        assert verification["still_pending"] is None
        (note,) = verification["notes"]
        assert "nothing verified this reject" in note
        assert "receipt for the REQUEST" in note

    @pytest.mark.asyncio
    async def test_the_landed_write_is_still_not_failed_by_any_of_this(self):
        """The rule that outranks everything here: verification never turns a
        mutation that happened into an error."""
        _observe(REP1_RECORD)
        service = _batch_service(
            {"suggestionResponses": [{"rejectedSuggestionIds": ["suggest.rep1"]}]},
        )
        service.documents.return_value.get.side_effect = RuntimeError("read exploded")

        result = await self._reject(service)
        assert result["rejected_suggestion_ids"] == ["suggest.rep1"]


class TestAThreadWriteReportsWhatTheApiSaid:
    """Round 6 MEDIUM 4: ``"saved": comment_update_state == "ALL_SAVED"``.

    An ASSERTION wearing a comparison. A response with no state at all
    produced ``saved: false`` beside a fully populated stored Post -- post_id,
    create_time and content equal to what was sent -- which reads as "your
    reply did not save". The agent's remedy for that is to send it again, and
    a duplicate reply in a customer's document cannot be un-sent.
    """

    @staticmethod
    def _reply_service(state=None):
        response = {
            "replies": [
                {"addCommentReply": {"post": {"postId": "p1", "content": "Hi"}}}
            ]
        }
        if state is not None:
            response["commentUpdateState"] = state
        return _batch_service(response)

    @staticmethod
    async def _reply(service) -> dict:
        return json.loads(
            await _unwrap(write_tools.reply_to_doc_thread)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                reply_content="Hi",
                comment_id="c1",
            )
        )

    @pytest.mark.asyncio
    async def test_a_missing_state_is_unknown_not_a_failure(self):
        verification = (await self._reply(self._reply_service()))["verification"]

        assert verification["saved"] is None, verification
        assert verification["saved_unavailable"] == "no_comment_update_state"
        # The stored Post is still reported -- as evidence about the CONTENT.
        assert verification["matches_request"] is True
        (note,) = verification["notes"]
        assert "UNKNOWN -- not false" in note
        assert "duplicate" in note
        assert "'p1'" in note

    @pytest.mark.asyncio
    async def test_all_saved_is_still_a_yes(self):
        verification = (await self._reply(self._reply_service("ALL_SAVED")))[
            "verification"
        ]
        assert verification["saved"] is True
        assert "saved_unavailable" not in verification
        assert "notes" not in verification

    @pytest.mark.asyncio
    async def test_an_anchored_comment_without_a_state_is_unknown_too(self):
        service = _batch_service(
            {
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
                ]
            }
        )
        result = json.loads(
            await _unwrap(write_tools.create_anchored_doc_comment)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="Needs work",
                start_index=1,
                end_index=6,
            )
        )

        verification = result["verification"]
        assert verification["saved"] is None, verification
        assert verification["saved_unavailable"] == "no_comment_update_state"
        (note,) = verification["notes"]
        assert "UNKNOWN -- not false" in note
        assert "duplicate" in note

    def test_saved_cannot_be_asserted_past_the_evidence(self):
        for state, expected in (
            ("ALL_SAVED", True),
            ("ALL_FAILED_UNKNOWN_REASON", False),
            (None, None),
            ("COMMENT_UPDATE_STATE_UNSPECIFIED", None),
            ("SOME_FUTURE_STATE", None),
        ):
            verdict = write_tools._ThreadWriteVerdict.derive(state)
            assert verdict.saved is expected, state
        with pytest.raises(ValueError, match="saved"):
            write_tools._ThreadWriteVerdict(comment_update_state=None, saved=True)
        with pytest.raises(ValueError, match="saved"):
            write_tools._ThreadWriteVerdict(comment_update_state=None, saved=False)


class TestTheRangeEchoIsLabelledAsRequested:
    """Round 6 MEDIUM 5: ``anchored_range`` echoed the REQUESTED range.

    The tool makes no read and the API does not echo a resolved range, so
    those five numbers are the argument list coming back. Under a name that
    says "this is where the comment is anchored", an off-by-one range reads as
    confirmed. ``anchored_text`` (plainTextQuote) is the evidence, and it is
    the only thing in the block that came from the document.
    """

    @pytest.mark.asyncio
    async def test_the_echoed_numbers_say_they_are_the_request(self):
        service = _batch_service(
            {
                "commentUpdateState": "ALL_SAVED",
                "replies": [
                    {
                        "insertComment": {
                            "commentThread": {
                                "commentId": "c7",
                                # The comment really anchored one character
                                # off what was asked for.
                                "plainTextQuote": "he qu",
                                "headPost": {"postId": "p7", "content": "?"},
                            }
                        }
                    }
                ],
            }
        )
        result = json.loads(
            await _unwrap(write_tools.create_anchored_doc_comment)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                content="?",
                start_index=1,
                end_index=6,
            )
        )

        verification = result["verification"]
        assert "anchored_range" not in verification, verification
        assert verification["requested_range"]["start_index"] == 1
        assert verification["anchored_text"] == "he qu"
        doc = _unwrap(write_tools.create_anchored_doc_comment).__doc__
        assert "requested_range is the range this call ASKED for" in doc


class TestASuggestionWithNoContentMarkIsStillPending:
    """The pending set was the MODELLED set, and they are not the same.

    ``_PostWriteRead.records`` comes from walking the document's content
    marks. Measured against prod 2026-08-02 (docs/findings/coverage.md), a
    paragraph-style, bullet or table row/cell-style suggestion leaves none:
    the card exists only as an OPEN thread in the payload's top-level
    ``suggestions`` array. ``pending_state`` asked ``id in self.records``, so
    such a card was reported ``False`` -- "gone", "the resolution landed" --
    from a COMPLETE read that was listing it as OPEN in the very same
    payload. That is fail-open verification on the one destructive path this
    package has, and the read had the contradicting evidence in hand.
    """

    def _read(self, threads):
        payload = fx.build_tabs_payload(
            [("t.0", fx.DOC_PARAGRAPH_STYLE_ONLY)], suggestions=threads
        )
        tabs = preview_read.tab_documents(payload)
        return preview_read.ReviewRead(
            tabs=[(t.tab_id, t.document) for t in tabs],
            tab_metadata=[t.metadata for t in tabs],
            threads=preview_read.suggestion_threads_by_id(payload),
            source=preview_read.READ_SOURCE_PREVIEW,
            complete=True,
        )

    def test_the_analysis_layer_really_does_not_model_it(self):
        """The premise, asserted rather than assumed."""
        read = self._read([fx.PARAGRAPH_STYLE_THREAD])
        post = write_tools._PostWriteRead(read)
        assert post.records == {}
        assert post.pending_thread_ids == frozenset({"suggest.para1"})

    def test_an_open_thread_the_records_do_not_carry_is_pending(self):
        post = write_tools._PostWriteRead(self._read([fx.PARAGRAPH_STYLE_THREAD]))
        assert post.pending_state("suggest.para1", None) == (True, None)

    def test_a_rejected_thread_is_not_pending(self):
        """The other direction has to keep working: once the reject lands the
        thread stays behind with ``status: "REJECTED"``, and reporting THAT
        as still pending would be a false alarm on every resolved card."""
        post = write_tools._PostWriteRead(
            self._read([fx.PARAGRAPH_STYLE_THREAD_REJECTED])
        )
        assert post.pending_state("suggest.para1", None) == (False, None)

    def test_an_id_in_neither_place_is_still_gone_on_a_complete_read(self):
        post = write_tools._PostWriteRead(self._read([fx.PARAGRAPH_STYLE_THREAD]))
        assert post.pending_state("suggest.never-existed", None) == (False, None)

    def test_a_degraded_read_still_answers_unknown_not_false(self):
        """A GA read carries no thread array either, so the widened check must
        not turn its blindness into a confident absence."""
        read = preview_read.ReviewRead(
            tabs=[(None, fx.DOC_PARAGRAPH_STYLE_ONLY)],
            source=preview_read.READ_SOURCE_GA,
            degraded_reason="not enrolled",
        )
        post = write_tools._PostWriteRead(read)
        assert post.pending_thread_ids == frozenset()
        assert post.pending_state("suggest.para1", None) == (None, "read_incomplete")


# ---------------------------------------------------------------------------
# Close-out round -- the pending COUNTS were still the modelled set
# ---------------------------------------------------------------------------

#: A complete preview read of a document whose ONLY pending card is one this
#: layer does not model (alignment, via ``updateParagraphStyle``).
PARAGRAPH_STYLE_ONLY_READ = fx.build_tabs_payload(
    [("t.0", fx.DOC_PARAGRAPH_STYLE_ONLY)], suggestions=[fx.PARAGRAPH_STYLE_THREAD]
)

#: The realistic mixed state: one text insertion this layer models
#: (``suggest.ins1``) and one alignment card it does not (``suggest.para1``).
TEXT_PLUS_PARAGRAPH_STYLE_READ = fx.build_tabs_payload(
    [("t.0", fx.DOC_TEXT_PLUS_PARAGRAPH_STYLE)],
    suggestions=[thread_for("suggest.ins1"), fx.PARAGRAPH_STYLE_THREAD],
)

#: The alignment card as a LISTING would have recorded it, if the analysis
#: layer had modelled it at the time (a heading suggestion does land on a run,
#: so it is modelled -- until a concurrent edit removes that run and leaves the
#: paragraph-style half behind with the thread still OPEN).
PARA_RECORD = {
    "suggestion_id": "suggest.para1",
    "type": "style",
    "pre_text": "Alpha line one.\n",
    "post_text": "Alpha line one.\n",
    "context_before": "",
    "context_after": "",
    "segment": "body",
    "segment_id": None,
    "tab_id": "t.0",
    "start_index": 1,
    "end_index": 17,
}


async def _resolve(service, *, action="reject", suggestion_id="suggest.para1") -> dict:
    result = json.loads(
        await _unwrap(write_tools.manage_document_suggestion)(
            service,
            user_google_email=EMAIL,
            document_id=DOC,
            action=action,
            suggestion_id=suggestion_id,
        )
    )
    return result["verification"]


class TestThePendingCountsAccountForWhatTheApiCallsPending:
    """The read tools' ``unreported_suggestion_count`` fix, one level up.

    ``pending_suggestion_count`` / ``pending_suggestion_ids`` were
    ``len(read.records)`` / ``sorted(read.records)`` -- the MODELLED set --
    while ``still_pending`` beside them consults
    :attr:`_PostWriteRead.pending_thread_ids`, the API's own OPEN inventory.
    On a COMPLETE read of a document holding an unmodelled OPEN card the
    response therefore said ``pending_suggestion_count: 0`` with no
    ``pending_suggestions_are_partial`` flag to qualify it (the read WAS
    complete) -- a false absence claim -- and could print ``still_pending:
    true`` beside a ``pending_suggestion_ids`` that omits that very id: a
    response contradicting itself on the destructive path.
    """

    @pytest.mark.asyncio
    async def test_a_reject_that_did_not_land_is_reconcilable_with_the_counts(self):
        verification = await _resolve(
            _batch_service({}, document=PARAGRAPH_STYLE_ONLY_READ)
        )

        assert verification["read_source"] == "preview_threads"
        assert verification["still_pending"] is True, verification
        # The whole point: the id the response calls still-pending has to be
        # findable in the same response's own pending accounting.
        accounted = set(verification["pending_suggestion_ids"] or []) | {
            card["suggestion_id"]
            for card in verification.get("unreported_suggestions") or []
        }
        assert "suggest.para1" in accounted, verification

    @pytest.mark.asyncio
    async def test_a_complete_read_never_claims_an_empty_document(self):
        verification = await _resolve(
            _batch_service({}, document=PARAGRAPH_STYLE_ONLY_READ)
        )

        assert verification["pending_suggestion_count"] == 0
        assert "pending_suggestions_are_partial" not in verification
        # ...so the qualifier has to come from the unmodelled remainder.
        assert verification["unreported_suggestion_count"] == 1, verification
        assert verification["unreported_suggestions"][0]["summary_text"] == (
            "Format: alignment"
        )
        notice = verification["notice_unreported"]
        assert "Format: alignment" in notice
        assert "manage_document_suggestion" in notice

    @pytest.mark.asyncio
    async def test_the_suggest_path_carries_the_same_remainder(self):
        service = _batch_service(
            {"suggestionResponses": [{"createdSuggestionIds": ["suggest.ins1"]}]},
            document=TEXT_PLUS_PARAGRAPH_STYLE_READ,
        )
        result = json.loads(
            await _unwrap(write_tools.suggest_doc_edit)(
                service,
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=6,
                text="brave ",
            )
        )
        verification = result["verification"]

        assert verification["pending_suggestion_count"] == 1
        assert verification["unreported_suggestion_count"] == 1, verification
        assert [c["suggestion_id"] for c in verification["unreported_suggestions"]] == [
            "suggest.para1"
        ]

    @pytest.mark.asyncio
    async def test_a_document_with_nothing_unmodelled_says_zero(self):
        """The control: the block is an accounting number, not an alarm."""
        _observe(REP1_RECORD)
        verification = await _resolve(
            _batch_service({}, document=REPLACEMENT_READ),
            action="accept",
            suggestion_id="suggest.rep1",
        )
        assert verification["unreported_suggestion_count"] == 0
        assert "unreported_suggestions" not in verification
        assert "notice_unreported" not in verification

    @pytest.mark.asyncio
    async def test_a_degraded_read_refuses_the_number_rather_than_answering_zero(self):
        """A GA read carries no thread array, so the subtraction has no
        minuend: 0 there would be an absence claim from a read that never
        looked, which is the failure this whole block exists to retire."""
        _observe(T1_RECORD)
        verification = await _resolve(
            _degraded_read_service(), action="accept", suggestion_id="suggest.t1"
        )

        assert verification["unreported_suggestion_count"] is None, verification
        assert verification["unreported_suggestions_unavailable"] == "read_degraded"
        assert "never looked" in verification["notice_unreported"]

    @pytest.mark.asyncio
    async def test_the_unverified_paths_carry_the_key_nulled(self):
        """Same rule as ``pending_suggestion_count``: a documented key that
        vanishes makes an absent field and an unknown value one observation."""
        result = json.loads(
            await _unwrap(write_tools.manage_document_suggestion)(
                service := _batch_service({}),
                user_google_email=EMAIL,
                document_id=DOC,
                action="reject",
                suggestion_id="suggest.para1",
                verify=False,
            )
        )
        assert service is not None
        verification = result["verification"]
        assert verification["unreported_suggestion_count"] is None
        assert verification["unreported_suggestions_unavailable"] == "not_verified"

    @pytest.mark.asyncio
    async def test_the_unverified_suggest_path_too(self):
        result = json.loads(
            await _unwrap(write_tools.suggest_doc_edit)(
                _batch_service(),
                user_google_email=EMAIL,
                document_id=DOC,
                start_index=5,
                text="hello",
                verify=False,
            )
        )
        verification = result["verification"]
        assert verification["unreported_suggestion_count"] is None
        assert verification["unreported_suggestions_unavailable"] == "not_verified"

    def test_both_tools_document_the_key(self):
        for tool in (
            write_tools.manage_document_suggestion,
            write_tools.suggest_doc_edit,
        ):
            doc = _unwrap(tool).__doc__ or ""
            assert "unreported_suggestion_count" in doc, tool


class TestStillPresentIsNotTheSameFactAsCouldNotLook:
    """``absences()`` routed a still-OPEN id into the "we could not look" pile.

    ``pending_state`` answers ``True`` for an id the API still lists as
    pending but the analysis layer no longer describes. ``absences`` sent
    everything that was not a decided ``False`` into ``unattested``, and
    ``_collateral_unavailable_note`` then asserted *"that read did not cover
    the whole document"* about a read whose ``complete`` was ``True``. "Still
    present but unmodelled" and "we could not look" are different facts and
    must not share a sentence.
    """

    @pytest.mark.asyncio
    async def test_a_complete_read_is_never_described_as_partial(self):
        _observe(PARA_RECORD)
        verification = await _resolve(
            _batch_service({}, document=PARAGRAPH_STYLE_ONLY_READ),
            action="accept",
            suggestion_id="suggest.gone",
        )

        assert verification["read_source"] == "preview_threads"
        joined = " ".join(verification.get("notes") or [])
        assert "did not cover the whole document" not in joined, joined

    @pytest.mark.asyncio
    async def test_the_still_open_id_is_named_as_still_pending_not_as_unknown(self):
        _observe(PARA_RECORD)
        verification = await _resolve(
            _batch_service({}, document=PARAGRAPH_STYLE_ONLY_READ),
            action="accept",
            suggestion_id="suggest.gone",
        )

        assert verification["still_pending_unmodelled_suggestion_ids"] == [
            "suggest.para1"
        ]
        assert "also_removed_suggestion_ids" not in verification
        assert "also_removed_suggestion_ids_unavailable" not in verification
        note = next(n for n in verification["notes"] if "suggest.para1" in n)
        assert "still lists it as pending" in note
        assert "NOT removed" in note

    def test_absences_reports_three_groups(self):
        """The unit underneath: presence, absence, and no standing to say."""
        payload = fx.build_tabs_payload(
            [("t.0", fx.DOC_PARAGRAPH_STYLE_ONLY)],
            suggestions=[fx.PARAGRAPH_STYLE_THREAD],
        )
        tabs = preview_read.tab_documents(payload)
        read = write_tools._PostWriteRead(
            preview_read.ReviewRead(
                tabs=[(t.tab_id, t.document) for t in tabs],
                tab_metadata=[t.metadata for t in tabs],
                threads=preview_read.suggestion_threads_by_id(payload),
                source=preview_read.READ_SOURCE_PREVIEW,
                complete=True,
            )
        )
        gone, still_pending, unattested = read.absences(
            ["suggest.para1", "suggest.gone"],
            {"suggest.para1": PARA_RECORD, "suggest.gone": None},
        )
        assert gone == ["suggest.gone"]
        assert still_pending == ["suggest.para1"]
        assert unattested == []

    @pytest.mark.asyncio
    async def test_a_blind_read_still_reports_unknown(self):
        """The other branch has to keep working: on a degraded read the
        withheld-collateral sentence is TRUE and must still fire."""
        _observe(T1_RECORD, REP1_RECORD)
        verification = await _resolve(
            _degraded_read_service(), action="accept", suggestion_id="suggest.rep1"
        )

        assert verification["also_removed_suggestion_ids_unavailable"] == (
            "read_incomplete"
        )
        note = next(
            n for n in verification["notes"] if "did not cover the whole document" in n
        )
        assert "suggest.t1" in note
