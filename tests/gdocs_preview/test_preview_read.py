"""Unit tests for the Developer Preview (tabs + threads) document read.

No network: the googleapiclient Resource and the ``AuthorizedSession``
fallback are both mocked. What is pinned here are the shapes verified
against the live API on 2026-07-30 (docs/preview-api-reference.md):

  - ``commentsViewMode`` is absent from public discovery, so the client
    raises ``TypeError`` and the fetch must fall back to a raw authorized
    request carrying all three query parameters;
  - tabs mode has no top-level ``body`` -- content lives under
    ``tabs[i].documentTab``, one GA-shaped Document per tab;
  - threads live under top-level ``suggestions`` / ``comments``, and every
    thread and reply carries an id and an author.
"""

from unittest.mock import Mock, patch

import pytest

from gdocs_preview import preview_read
from tests.gdocs_preview import fixtures as fx


class TestCredentialsFromService:
    def test_reads_the_authorized_http_credentials(self):
        service = Mock()
        service._http.credentials = "creds-object"
        assert preview_read.credentials_from_service(service) == "creds-object"

    def test_falls_back_to_the_private_credentials_attribute(self):
        service = Mock(spec=["_credentials"])
        service._credentials = "creds-object"
        assert preview_read.credentials_from_service(service) == "creds-object"

    def test_raises_when_no_credentials_are_reachable(self):
        service = Mock(spec=[])
        with pytest.raises(preview_read.PreviewReadError, match="credentials"):
            preview_read.credentials_from_service(service)


class TestFetch:
    @pytest.mark.asyncio
    async def test_uses_the_client_when_it_accepts_the_parameter(self):
        service = Mock()
        service.documents.return_value.get.return_value.execute = Mock(
            return_value=fx.TABS_PAYLOAD
        )

        payload = await preview_read.fetch_document_with_threads(
            service, "doc-1", "SUGGESTIONS_INLINE"
        )

        assert payload is fx.TABS_PAYLOAD
        service.documents.return_value.get.assert_called_once_with(
            documentId="doc-1",
            suggestionsViewMode="SUGGESTIONS_INLINE",
            commentsViewMode="COMMENTS_VIEW_MODE_INCLUDED",
            includeTabsContent=True,
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_a_raw_authorized_request(self):
        """The real client rejects commentsViewMode before sending anything
        (not in public discovery), so the raw path must carry the query."""
        service = Mock()
        service._http.credentials = "creds-object"
        service.documents.return_value.get.side_effect = TypeError(
            "Got an unexpected keyword argument commentsViewMode"
        )
        response = Mock(status_code=200)
        response.json.return_value = fx.TABS_PAYLOAD
        session = Mock()
        session.get.return_value = response

        with patch(
            "google.auth.transport.requests.AuthorizedSession", return_value=session
        ) as factory:
            payload = await preview_read.fetch_document_with_threads(
                service, "doc-1", "SUGGESTIONS_INLINE"
            )

        assert payload is fx.TABS_PAYLOAD
        factory.assert_called_once_with("creds-object")
        url, kwargs = session.get.call_args[0][0], session.get.call_args[1]
        assert url == "https://docs.googleapis.com/v1/documents/doc-1"
        assert kwargs["params"] == {
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
            "commentsViewMode": "COMMENTS_VIEW_MODE_INCLUDED",
            "includeTabsContent": "true",
        }
        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_unrelated_type_error_is_not_swallowed(self):
        service = Mock()
        service.documents.return_value.get.side_effect = TypeError("nope")
        with pytest.raises(TypeError, match="nope"):
            await preview_read.fetch_document_with_threads(
                service, "doc-1", "SUGGESTIONS_INLINE"
            )

    @pytest.mark.asyncio
    async def test_non_200_becomes_a_preview_read_error(self):
        service = Mock()
        service._http.credentials = "creds-object"
        service.documents.return_value.get.side_effect = TypeError(
            "Got an unexpected keyword argument commentsViewMode"
        )
        session = Mock()
        session.get.return_value = Mock(
            status_code=400,
            text='{"error": {"message": "Comments view mode may only be '
            'specified if tabs content is also requested."}}',
        )

        with patch(
            "google.auth.transport.requests.AuthorizedSession", return_value=session
        ):
            with pytest.raises(preview_read.PreviewReadError, match="HTTP 400"):
                await preview_read.fetch_document_with_threads(
                    service, "doc-1", "SUGGESTIONS_INLINE"
                )


class TestThreadNormalization:
    def test_suggestion_threads_keyed_by_id(self):
        threads = preview_read.suggestion_threads_by_id(fx.TABS_PAYLOAD)
        assert list(threads) == ["suggest.ins1"]
        thread = threads["suggest.ins1"]
        assert thread["head_post_id"] == "AAAApost1"
        assert thread["author"] == {
            "display_name": "Alice Reviewer",
            "me": False,
            "anonymous": None,
            "user": "users/123",
        }
        assert thread["status"] == "OPEN"
        assert thread["summary_text"] == "Add: “brave”"
        assert thread["replies"][0]["post_id"] == "AAAApost2"

    def test_missing_suggestions_array_yields_no_threads(self):
        assert preview_read.suggestion_threads_by_id(fx.DOC_PLAIN_INSERTION) == {}

    def test_comment_threads_expose_ids_and_authors(self):
        (thread,) = preview_read.comment_threads(fx.TABS_PAYLOAD)
        assert thread["comment_id"] == "AAAAcomment1"
        assert thread["anchor_id"] == "kix.anchor1"
        assert thread["post_id"] == "AAAAcomment1"
        assert thread["content"] == "why brave?"
        assert thread["quoted_text"] == "Hello"
        assert thread["status"] == "OPEN"
        assert thread["author"]["display_name"] == "Alice Reviewer"
        (reply,) = thread["replies"]
        assert reply["post_id"] == "AAAAcomment2"
        assert reply["author"]["display_name"] == "Bob Author"

    def test_missing_comments_array_yields_no_threads(self):
        assert preview_read.comment_threads(fx.TABS_PAYLOAD_MULTI) == []

    def test_author_is_none_not_a_hollow_record(self):
        assert preview_read.normalize_author(None) is None
        assert preview_read.normalize_post({})["author"] is None


class TestTabDocuments:
    def test_single_tab_is_reshaped_into_a_ga_document(self):
        (tab,) = preview_read.tab_documents(fx.TABS_PAYLOAD)
        assert tab.tab_id == "t.0"
        assert tab.metadata == {"tab_id": "t.0", "title": "Tab 1", "index": 0}
        assert tab.document["documentId"] == "doc-fixture-1"
        assert tab.document["title"] == "Fixture Doc"
        # The content is the GA shape analysis.py already walks.
        assert tab.document["body"] == fx.DOC_PLAIN_INSERTION["body"]

    def test_header_segments_move_with_their_tab(self):
        payload = fx.build_tabs_payload([("t.0", fx.DOC_HEADER)])
        (tab,) = preview_read.tab_documents(payload)
        assert tab.document["headers"] == fx.DOC_HEADER["headers"]

    def test_every_tab_is_returned_in_order(self):
        tabs = preview_read.tab_documents(fx.TABS_PAYLOAD_MULTI)
        assert [t.tab_id for t in tabs] == ["t.0", "t.second"]
        assert [t.index for t in tabs] == [0, 1]

    def test_child_tabs_follow_their_parent_depth_first(self):
        payload = fx.build_tabs_payload([("t.0", fx.DOC_PLAIN_INSERTION)])
        payload["tabs"][0]["childTabs"] = [
            {
                "tabProperties": {"tabId": "t.child", "title": "Child", "index": 0},
                "documentTab": {"body": fx.DOC_SECOND_TAB["body"]},
            }
        ]
        tabs = preview_read.tab_documents(payload)
        assert [t.tab_id for t in tabs] == ["t.0", "t.child"]

    def test_payload_without_tabs_yields_one_implicit_tab(self):
        """The GA fallback read goes through the same code path."""
        (tab,) = preview_read.tab_documents(fx.DOC_PLAIN_INSERTION)
        assert tab.tab_id is None
        assert tab.document is fx.DOC_PLAIN_INSERTION
