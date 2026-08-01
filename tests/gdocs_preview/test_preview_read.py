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


def _network_error(name: str) -> Exception:
    """One of the transport failures a raw authorized GET can raise.

    ``requests`` for the request itself, ``google.auth`` for the token
    refresh it does first -- same network, two libraries.
    """
    import google.auth.exceptions
    import requests.exceptions

    for module in (requests.exceptions, google.auth.exceptions):
        if hasattr(module, name):
            return getattr(module, name)(f"simulated {name}")
    raise AssertionError(f"no such transport error: {name}")  # pragma: no cover


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

    @pytest.mark.parametrize(
        "error_name",
        ["ConnectionError", "ReadTimeout", "TransportError"],
    )
    @pytest.mark.asyncio
    async def test_a_network_failure_is_a_read_error_not_an_escaped_exception(
        self, error_name
    ):
        """``read_for_review`` degrades on PreviewReadError and on nothing
        else, so a refused connection used to hard-fail a read the GA path
        could have answered -- and the post-write verification read with it.
        """
        service = Mock()
        service._http.credentials = "creds-object"
        service.documents.return_value.get.side_effect = TypeError(
            "Got an unexpected keyword argument commentsViewMode"
        )
        session = Mock()
        session.get.side_effect = _network_error(error_name)

        with patch(
            "google.auth.transport.requests.AuthorizedSession", return_value=session
        ):
            with pytest.raises(preview_read.PreviewReadError, match=error_name):
                await preview_read.fetch_document_with_threads(
                    service, "doc-1", "SUGGESTIONS_INLINE"
                )
        # The session is still closed on the failure path.
        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_network_failure_degrades_the_read_instead_of_failing_it(self):
        service = Mock()
        service._http.credentials = "creds-object"
        service.documents.return_value.get.side_effect = TypeError(
            "Got an unexpected keyword argument commentsViewMode"
        )
        session = Mock()
        session.get.side_effect = _network_error("ConnectionError")

        async def _ga(*args, **kwargs):
            return fx.DOC_PLAIN_INSERTION

        with patch(
            "google.auth.transport.requests.AuthorizedSession", return_value=session
        ):
            with patch.object(preview_read, "fetch_ga_document", _ga):
                read = await preview_read.read_for_review(
                    service,
                    "doc-1",
                    "SUGGESTIONS_INLINE",
                    user_google_email="reviewer@example.com",
                )

        assert read.source == preview_read.READ_SOURCE_GA
        assert "ConnectionError" in read.degraded_reason


class TestAnEmptyTabsArrayIsNotAnEmptyDocument:
    """MEDIUM: ``tabs: []`` parses, carries no content, and said nothing.

    Every tool downstream then answered from nothing -- ``suggestion_count:
    0``, ``body_text: ""`` -- with ``read_source: "preview_threads"`` and no
    ``degraded_notice``, so "this read saw nothing" was delivered in the
    words of "the document has no suggestions".
    """

    @pytest.mark.asyncio
    async def test_it_degrades_to_the_ga_read_and_says_so(self):
        service = Mock()
        empty = dict(fx.TABS_PAYLOAD, tabs=[])

        async def _fetch(*args, **kwargs):
            return empty

        async def _ga(*args, **kwargs):
            return fx.DOC_PLAIN_INSERTION

        with patch.object(preview_read, "fetch_document_with_threads", _fetch):
            with patch.object(preview_read, "fetch_ga_document", _ga):
                read = await preview_read.read_for_review(
                    service,
                    "doc-1",
                    "SUGGESTIONS_INLINE",
                    user_google_email="reviewer@example.com",
                )

        assert read.source == preview_read.READ_SOURCE_GA
        assert "empty tabs array" in read.degraded_reason
        # And the content the GA read CAN see is returned, rather than the
        # empty answer the preview payload implied.
        assert read.tabs == [(None, fx.DOC_PLAIN_INSERTION)]


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
        assert tab.metadata == {
            "tab_id": "t.0",
            "title": "Tab 1",
            "index": 0,
            "parent_tab_id": None,
            "nesting_level": 0,
        }
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
        # It cannot see the tab tree AT ALL, so it claims no position in one.
        assert tab.parent_tab_id is None
        assert tab.nesting_level is None

    def test_a_nested_tab_reports_its_parent_and_depth(self):
        """``index`` alone cannot tell a child from a sibling.

        Measured against prod 2026-08-01: ``index`` is the position among
        SIBLINGS, so a document whose first tab has a child answers with two
        tabs at ``index: 0``. Without the parent, the flattened inventory
        presents the nested tab as a second top-level tab that collides with
        the first.
        """
        payload = fx.build_tabs_payload(
            [("t.0", fx.DOC_PLAIN_INSERTION), ("t.second", fx.DOC_SECOND_TAB)]
        )
        payload["tabs"][0]["childTabs"] = [
            {
                "tabProperties": {"tabId": "t.child", "title": "Child", "index": 0},
                "documentTab": {"body": fx.DOC_SECOND_TAB["body"]},
                "childTabs": [
                    {
                        "tabProperties": {
                            "tabId": "t.grandchild",
                            "title": "Grandchild",
                            "index": 0,
                        },
                        "documentTab": {"body": fx.DOC_SECOND_TAB["body"]},
                    }
                ],
            }
        ]
        tabs = preview_read.tab_documents(payload)
        assert [t.tab_id for t in tabs] == [
            "t.0",
            "t.child",
            "t.grandchild",
            "t.second",
        ]
        assert [t.parent_tab_id for t in tabs] == [None, "t.0", "t.child", None]
        assert [t.nesting_level for t in tabs] == [0, 1, 2, 0]
        # The collision the hierarchy fields are here to resolve.
        assert [t.index for t in tabs] == [0, 0, 0, 1]

    def test_nesting_is_read_off_the_walk_not_off_tabproperties(self):
        """proto3 omits ``nestingLevel: 0`` and a top-level tab carries no
        ``parentTabId``, so the fields are absent exactly where they would
        have to be read as defaults. The walk cannot omit a position."""
        payload = fx.build_tabs_payload([("t.0", fx.DOC_PLAIN_INSERTION)])
        assert "nestingLevel" not in payload["tabs"][0]["tabProperties"]
        assert "parentTabId" not in payload["tabs"][0]["tabProperties"]
        (tab,) = preview_read.tab_documents(payload)
        assert tab.nesting_level == 0
        assert tab.parent_tab_id is None


class TestCommentTabAttribution:
    """A ``CommentThread`` carries no tab field; its ``anchorId`` does.

    Verified against prod 2026-08-01: a three-tab document with one comment
    per tab answered with thread keys ``{anchorId, commentId, headPost,
    plainTextQuote, status}`` and NOTHING naming a tab -- while each tab's
    ``documentTab.commentAnchors`` held exactly its own anchor.
    """

    @staticmethod
    def _payload_with_anchors():
        payload = fx.build_tabs_payload(
            [("t.0", fx.DOC_PLAIN_INSERTION), ("t.second", fx.DOC_SECOND_TAB)],
            comments=[
                {
                    "commentId": "c1",
                    "anchorId": "kix.a1",
                    "status": "OPEN",
                    "headPost": {"postId": "c1", "content": "in the first tab"},
                },
                {
                    "commentId": "c2",
                    "anchorId": "kix.a2",
                    "status": "OPEN",
                    "headPost": {"postId": "c2", "content": "in the nested tab"},
                },
            ],
        )
        payload["tabs"][0]["documentTab"]["commentAnchors"] = {
            "kix.a1": {"anchorId": "kix.a1", "ranges": [{"endIndex": 5}]}
        }
        payload["tabs"][0]["childTabs"] = [
            {
                "tabProperties": {"tabId": "t.child", "title": "Child", "index": 0},
                "documentTab": {
                    "body": fx.DOC_SECOND_TAB["body"],
                    "commentAnchors": {
                        "kix.a2": {
                            "anchorId": "kix.a2",
                            "ranges": [{"startIndex": 1, "endIndex": 5}],
                        }
                    },
                },
            }
        ]
        return payload

    def test_a_comment_is_placed_by_its_anchor_including_in_a_child_tab(self):
        threads = preview_read.comment_threads(self._payload_with_anchors())
        assert {t["comment_id"]: t["tab_id"] for t in threads} == {
            "c1": "t.0",
            "c2": "t.child",
        }

    def test_an_unplaceable_comment_is_null_never_the_default_tab(self):
        payload = self._payload_with_anchors()
        payload["comments"].append(
            {"commentId": "c3", "status": "OPEN", "headPost": {"postId": "c3"}}
        )
        payload["comments"].append(
            {
                "commentId": "c4",
                "anchorId": "kix.gone",
                "status": "OPEN",
                "headPost": {"postId": "c4"},
            }
        )
        threads = {
            t["comment_id"]: t["tab_id"] for t in preview_read.comment_threads(payload)
        }
        assert threads["c3"] is None, "an unanchored thread has no tab to claim"
        assert threads["c4"] is None, "an anchor in no tab's map places nothing"

    def test_an_anchor_in_two_tabs_is_ambiguous_not_first_wins(self):
        payload = self._payload_with_anchors()
        payload["tabs"][1]["documentTab"]["commentAnchors"] = {
            "kix.a1": {"anchorId": "kix.a1", "ranges": [{"endIndex": 5}]}
        }
        assert preview_read.anchor_tab_ids(payload)["kix.a1"] is None

    def test_a_payload_without_tabs_places_nothing(self):
        assert preview_read.anchor_tab_ids(fx.DOC_PLAIN_INSERTION) == {}
