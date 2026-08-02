"""An API failure must never be returned as a successful result.

The repo standard (``HANDOVER.md`` §4) is that no response asserts anything
its evidence does not support, and that an error is never shaped like a
success. A tool that catches an ``HttpError`` and ``return``\\ s it as prose
breaks both halves at once: the MCP layer leaves ``is_error`` unset, so the
caller is told the call succeeded, and the body then makes a claim
("Table creation failed") that the tool has no evidence for.

This was not hypothetical. Building the e2e write-quota pacer on
2026-08-02, ``update_doc_headers_footers`` was caught swallowing a live 429
and returning it as a successful result; the quota guard saw a success, did
not retry, and the test failed on an assertion about the response body -- a
quota failure wearing the costume of a product bug
(``docs/findings/e2e-quota.md``).

Every test here drives the tool with the API raising, and asserts the tool
RAISES rather than returns. ``handle_http_errors`` -- already wrapping every
one of these tools in production -- is what then turns the raise into the
error the MCP layer marks. The one case that is deliberately allowed to
succeed is the table whose cells could not be populated: the table itself
exists by then, so failing would invite a retry that creates a second one.
That path must still SAY it degraded, which is asserted here too.
"""

from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

from core.utils import TransientNetworkError
from gdocs import docs_tools


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


#: A verbatim-shaped Docs write-quota rejection. The marker strings below
#: ("429", "Quota exceeded", "WriteRequestsPerMinutePerUser") are the ones
#: ``e2e/quota.py:is_rate_limit_error`` keys on, so a body carrying them is
#: exactly what a quota guard would have to parse back out of a success.
_QUOTA_BODY = (
    b'{"error":{"code":429,"message":"Quota exceeded for quota metric '
    b"'Quota group for write operations' and limit 'Quota group for write "
    b"operations per minute per user' of service 'docs.googleapis.com'.\","
    b'"status":"RESOURCE_EXHAUSTED","details":[{"reason":"RATE_LIMIT_EXCEEDED",'
    b'"metadata":{"quota_limit":"WriteRequestsPerMinutePerUser"}}]}}'
)


def rate_limited() -> HttpError:
    """The 429 the Docs API returns once a user is over 60 writes/minute."""
    return HttpError(
        resp=Mock(status=429),
        content=_QUOTA_BODY,
        uri="https://docs.googleapis.com/v1/documents/abc:batchUpdate?alt=json",
    )


def assert_is_a_quota_failure(excinfo) -> None:
    """The raised error must still carry what made it retryable."""
    message = str(excinfo.value)
    assert "429" in message, message
    assert "Quota exceeded" in message, message


DOC_ID = "a" * 25


class TestUpdateDocHeadersFooters:
    """The site measured against prod on 2026-08-02."""

    @staticmethod
    def _service(*, write_error: Exception | None = None) -> Mock:
        service = Mock()
        api = service.documents.return_value
        api.get.return_value.execute.return_value = {
            "documentStyle": {"defaultHeaderId": "hdr-1"},
            "headers": {"hdr-1": {"content": []}},
            "footers": {},
            "body": {"content": []},
        }
        if write_error is not None:
            api.batchUpdate.return_value.execute.side_effect = write_error
        else:
            api.batchUpdate.return_value.execute.return_value = {"replies": [{}]}
        return service

    @pytest.mark.asyncio
    async def test_a_rate_limited_write_raises_instead_of_returning_prose(self):
        service = self._service(write_error=rate_limited())

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.update_doc_headers_footers)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                section_type="header",
                content="Board Draft",
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_a_failing_read_raises_rather_than_reporting_no_header(self):
        """A 429 on the document read used to become the sentence
        'No header found in document and automatic creation failed' -- a
        claim about the DOCUMENT derived from a failure of the READ."""
        service = Mock()
        service.documents.return_value.get.return_value.execute.side_effect = (
            rate_limited()
        )

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.update_doc_headers_footers)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                section_type="header",
                content="Board Draft",
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_a_failing_section_creation_raises(self):
        """Creating the missing header is a write too, and a 429 on it is
        not evidence that the document refuses headers."""
        service = Mock()
        api = service.documents.return_value
        api.get.return_value.execute.return_value = {
            "headers": {},
            "footers": {},
            "body": {"content": []},
        }
        api.batchUpdate.return_value.execute.side_effect = rate_limited()

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.update_doc_headers_footers)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                section_type="header",
                content="Board Draft",
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_the_lagging_segment_retry_still_happens_before_raising(self):
        """The one retry that exists for a real reason -- a freshly created
        segment whose content lags -- must survive the fix. First write
        fails, second succeeds, and the tool reports success."""
        service = self._service()
        api = service.documents.return_value
        api.batchUpdate.return_value.execute.side_effect = [
            HttpError(
                resp=Mock(status=400),
                content=b'{"error":{"message":"Invalid range"}}',
                uri="https://docs.googleapis.com/v1/documents/abc:batchUpdate",
            ),
            {"replies": [{}]},
        ]

        result = await _unwrap(docs_tools.update_doc_headers_footers)(
            service=service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            section_type="header",
            content="Board Draft",
        )

        assert "Updated header content" in result
        assert api.batchUpdate.return_value.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_a_validation_failure_is_still_returned_as_prose(self):
        """Only API failures change shape. The tool's own input checks are
        not errors about the world and keep answering in words."""
        result = await _unwrap(docs_tools.update_doc_headers_footers)(
            service=Mock(),
            user_google_email="user@example.com",
            document_id=DOC_ID,
            section_type="sidebar",
            content="Board Draft",
        )

        assert result.startswith("Error: ")
        assert "header" in result and "footer" in result


class TestBatchUpdateDoc:
    @pytest.mark.asyncio
    async def test_a_rate_limited_batch_raises_instead_of_returning_prose(self):
        service = Mock()
        api = service.documents.return_value
        api.get.return_value.execute.return_value = {
            "documentStyle": {},
            "body": {"content": []},
        }
        api.batchUpdate.return_value.execute.side_effect = rate_limited()

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.batch_update_doc)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                operations=[{"type": "insert_text", "index": 1, "text": "hi"}],
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_an_invalid_operation_is_still_returned_as_prose(self):
        """Operation validation raises ValueError internally and is the
        caller's mistake, not the API's: it keeps its returned guidance."""
        service = Mock()
        service.documents.return_value.get.return_value.execute.return_value = {
            "documentStyle": {},
            "body": {"content": []},
        }

        result = await _unwrap(docs_tools.batch_update_doc)(
            service=service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            operations=[{"type": "insert_text"}],
        )

        assert result.startswith("Error: ")
        assert "Missing required field" in result

    @pytest.mark.asyncio
    async def test_the_duplicate_header_rewrite_survives_as_an_input_error(self):
        """The API's 'already exists' is the caller aiming the wrong tool at
        the job. It keeps its actionable rewrite -- but as a raised input
        error, not as a successful result."""
        from core.utils import UserInputError

        service = Mock()
        api = service.documents.return_value
        api.get.return_value.execute.return_value = {
            "documentStyle": {},
            "body": {"content": []},
        }
        api.batchUpdate.return_value.execute.side_effect = HttpError(
            resp=Mock(status=400),
            content=(
                b'{"error":{"message":"Invalid requests[0].createHeader: '
                b'A default header already exists."}}'
            ),
            uri="https://docs.googleapis.com/v1/documents/abc:batchUpdate",
        )

        with pytest.raises(UserInputError) as excinfo:
            await _unwrap(docs_tools.batch_update_doc)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                operations=[
                    {"type": "create_header_footer", "section_type": "header"}
                ],
            )

        assert "update_doc_headers_footers" in str(excinfo.value)


class TestCreateTableWithData:
    @pytest.mark.asyncio
    async def test_a_rate_limited_creation_raises_instead_of_claiming_failure(self):
        """'Table creation failed' as a SUCCESSFUL result is wrong twice:
        wrong shape, and a claim about the table rather than about the call."""
        service = Mock()
        api = service.documents.return_value
        api.batchUpdate.return_value.execute.side_effect = rate_limited()
        api.get.return_value.execute.return_value = {"body": {"content": []}}

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.create_table_with_data)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                table_data=[["a", "b"], ["c", "d"]],
                index=1,
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_the_document_end_retry_survives_the_fix(self):
        """create_table_with_data retries at index-1 when the API says the
        index is past the end. That retry keyed off a RETURNED error string;
        it must keep working now that the failure is raised."""
        service = Mock()
        api = service.documents.return_value
        past_end = HttpError(
            resp=Mock(status=400),
            content=(
                b'{"error":{"message":"Invalid requests[0].insertTable: Index 5 '
                b'must be less than the end index of the referenced segment, 5."}}'
            ),
            uri="https://docs.googleapis.com/v1/documents/abc:batchUpdate",
        )
        # One rejection, then an unbounded supply of successes: the retry
        # creates the table and then populates its cells.
        calls = {"n": 0}

        def _execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise past_end
            return {"replies": [{}]}

        api.batchUpdate.return_value.execute.side_effect = _execute
        api.get.return_value.execute.return_value = {
            "body": {
                "content": [
                    {
                        "startIndex": 4,
                        "endIndex": 12,
                        "table": {
                            "rows": 1,
                            "columns": 1,
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "startIndex": 5,
                                            "endIndex": 7,
                                            "content": [
                                                {
                                                    "startIndex": 5,
                                                    "endIndex": 7,
                                                    "paragraph": {"elements": []},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                        },
                    }
                ]
            }
        }

        result = await _unwrap(docs_tools.create_table_with_data)(
            service=service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            table_data=[["a"]],
            index=5,
        )

        assert "Index: 4" in result

    @pytest.mark.asyncio
    async def test_a_created_table_whose_cells_fail_degrades_and_says_so(self):
        """The deliberate degrade. By the time population fails the table
        exists, so raising would invite a retry that creates a second one.
        It stays a success -- and has to say what did not happen."""
        service = Mock()
        api = service.documents.return_value
        # The table is created, then every later write is rate limited.
        calls = {"n": 0}

        def _execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"replies": [{}]}
            raise rate_limited()

        api.batchUpdate.return_value.execute.side_effect = _execute
        api.get.return_value.execute.return_value = {
            "body": {
                "content": [
                    {
                        "startIndex": 1,
                        "endIndex": 12,
                        "table": {
                            "rows": 1,
                            "columns": 1,
                            "tableRows": [
                                {
                                    "tableCells": [
                                        {
                                            "startIndex": 2,
                                            "endIndex": 4,
                                            "content": [
                                                {
                                                    "startIndex": 2,
                                                    "endIndex": 4,
                                                    "paragraph": {"elements": []},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                        },
                    }
                ]
            }
        }

        result = await _unwrap(docs_tools.create_table_with_data)(
            service=service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            table_data=[["a"]],
            index=1,
        )

        assert result.startswith("PARTIAL SUCCESS")
        assert "Do not retry table creation" in result
        assert "429" in result


class TestExportDocToPdf:
    @staticmethod
    def _doc_metadata() -> dict:
        return {
            "id": DOC_ID,
            "name": "Board Draft",
            "mimeType": "application/vnd.google-apps.document",
            "webViewLink": "https://docs.google.com/document/d/x/edit",
        }

    @pytest.mark.asyncio
    async def test_a_failing_metadata_read_raises(self):
        service = Mock()
        service.files.return_value.get.return_value.execute.side_effect = (
            rate_limited()
        )

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.export_doc_to_pdf)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_a_failing_export_raises(self):
        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = (
            self._doc_metadata()
        )
        service.files.return_value.export_media.side_effect = rate_limited()

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.export_doc_to_pdf)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_a_failing_upload_raises_and_still_says_the_pdf_was_built(
        self, monkeypatch
    ):
        """Nothing was saved, so this is a failure -- but the fact that the
        bytes existed is the caller's cue that a retry is cheap."""
        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = (
            self._doc_metadata()
        )

        class _Downloader:
            def __init__(self, fh, request):
                self._fh = fh

            def next_chunk(self):
                self._fh.write(b"%PDF-1.4 fake")
                return None, True

        monkeypatch.setattr(docs_tools, "MediaIoBaseDownload", _Downloader)
        service.files.return_value.create.return_value.execute.side_effect = (
            rate_limited()
        )

        with pytest.raises(Exception) as excinfo:
            await _unwrap(docs_tools.export_doc_to_pdf)(
                service=service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
            )

        message = str(excinfo.value)
        assert "429" in message
        assert "generated" in message.lower()


class TestInsertDocImage:
    @pytest.mark.asyncio
    async def test_a_failing_drive_lookup_raises(self):
        docs_service = Mock()
        drive_service = Mock()
        drive_service.files.return_value.get.return_value.execute.side_effect = (
            rate_limited()
        )

        with pytest.raises(HttpError) as excinfo:
            await _unwrap(docs_tools.insert_doc_image)(
                docs_service=docs_service,
                drive_service=drive_service,
                user_google_email="user@example.com",
                document_id=DOC_ID,
                image_source="drivefileid123",
                index=1,
            )

        assert_is_a_quota_failure(excinfo)

    @pytest.mark.asyncio
    async def test_a_non_image_file_is_still_returned_as_prose(self):
        """The Drive call succeeded; this is a fact about the file, not a
        failure of the API."""
        docs_service = Mock()
        drive_service = Mock()
        drive_service.files.return_value.get.return_value.execute.return_value = {
            "id": "x",
            "name": "notes.txt",
            "mimeType": "text/plain",
        }

        result = await _unwrap(docs_tools.insert_doc_image)(
            docs_service=docs_service,
            drive_service=drive_service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            image_source="drivefileid123",
            index=1,
        )

        assert result.startswith("Error: ")
        assert "not an image" in result


class TestGetDocAsMarkdown:
    @pytest.mark.asyncio
    async def test_a_timed_out_read_raises_a_transient_network_error(self):
        """A timeout is the network failing, not the document being empty.
        ``TransientNetworkError`` is the type ``handle_http_errors`` already
        passes through unwrapped."""
        import asyncio

        drive_service = Mock()
        docs_service = Mock()

        async def _timeout(awaitable, timeout):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError()

        original = asyncio.wait_for
        asyncio.wait_for = _timeout
        try:
            with pytest.raises(TransientNetworkError) as excinfo:
                await _unwrap(docs_tools.get_doc_as_markdown)(
                    drive_service=drive_service,
                    docs_service=docs_service,
                    user_google_email="user@example.com",
                    document_id=DOC_ID,
                )
        finally:
            asyncio.wait_for = original

        assert "Timed out" in str(excinfo.value)
