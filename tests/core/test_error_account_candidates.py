"""AC3 — a failed call NAMES the other accounts and never tries them.

The affordance, not the action. A Docs 404/403, or a preview-gated write on an
account the API has told us is not enrolled, appends the other authenticated
accounts to the error and says plainly that nothing was attempted under them.

The load-bearing test here is
:func:`test_no_error_path_ever_fetches_another_accounts_credentials`: it fails
the moment any error path reaches into the credential store for a second
account, which is the one behaviour this whole feature exists to prevent
(``404 notFound`` covers both "no access" and "no such file", so an automatic
retry would walk the wallet on a typo'd document id).

Two properties keep the single-account fork merge-friendly and keep the suite
deterministic:

* with zero or one authenticated account the messages are byte-identical to
  what upstream produces today, and
* the hint is silent unless the store actually contains the account the failed
  call ran under — so a mocked test with an invented email never picks up the
  developer's real credential directory.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import auth.credential_store as credential_store
import core.account_directory as account_directory
import core.utils as utils
from auth.credential_store import LocalDirectoryCredentialStore

CALLER = "solo@example.com"
OTHER = "work@example.com"
THIRD = "home@example.com"

DOC_URI = "https://docs.googleapis.com/v1/documents/DOCID?alt=json"

#: The proto-parse rejection an unenrolled project gets for a preview request
#: field (verbatim from ``tests/gdocs_preview/test_write_tools.py``).
NOT_ENROLLED_BODY = (
    b'{"error": {"message": "Invalid JSON payload received. '
    b'Unknown name \\"insertComment\\" at \'requests[0]\'"}}'
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class RecordingStore(LocalDirectoryCredentialStore):
    """A real local store that remembers whose credentials were asked for."""

    def __init__(self, base_dir):
        super().__init__(base_dir=base_dir)
        self.credential_requests = []

    def get_credential(self, user_email):
        self.credential_requests.append(user_email)
        return super().get_credential(user_email)


def _store(tmp_path, emails=(), *, recording=False):
    base_dir = tmp_path / "creds"
    base_dir.mkdir(parents=True, exist_ok=True)
    for email in emails:
        (base_dir / f"{email}.json").write_text("{}", encoding="utf-8")
    factory = RecordingStore if recording else LocalDirectoryCredentialStore
    return factory(base_dir=str(base_dir))


def _use_store(monkeypatch, store):
    """Install the store everywhere a lookup could reach for it."""
    monkeypatch.setattr(credential_store, "_credential_store", store)
    monkeypatch.setattr(account_directory, "peek_credential_store", lambda: store)


def _single_user_mode(monkeypatch):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(utils, "is_oauth21_enabled", lambda: False)


def _http_error(status: int, message: str = "The requested entity was not found."):
    resp = MagicMock()
    resp.status = status
    resp.reason = "mock"
    body = ('{"error": {"code": %d, "message": "%s"}}' % (status, message)).encode()
    return HttpError(resp=resp, content=body, uri=DOC_URI)


def _tool(error, *, tool_name="get_doc_content"):
    """A decorated tool that fails with ``error`` and counts its own calls."""
    calls = []

    @utils.handle_http_errors(tool_name)
    async def failing_tool(*, user_google_email, document_id):
        calls.append(user_google_email)
        raise error

    return failing_tool, calls


async def _message(error, *, user_google_email=CALLER, tool_name="get_doc_content"):
    tool, calls = _tool(error, tool_name=tool_name)
    with pytest.raises(Exception) as excinfo:
        await tool(user_google_email=user_google_email, document_id="DOCID")
    assert len(calls) == 1, "the failed call must not be re-attempted"
    return str(excinfo.value), excinfo.value


def _todays_message(error, tool_name="get_doc_content", *, status=404):
    """The message upstream produces today, rebuilt longhand."""
    if status == 404:
        return f"API error in {tool_name}: {error}"
    return (
        f"API error in {tool_name}: {error}. "
        f"You might need to re-authenticate for user '{CALLER}'. "
        "LLM: Try 'start_google_auth' with the user's email "
        "and the appropriate service_name."
    )


# ---------------------------------------------------------------------------
# The hint builder
# ---------------------------------------------------------------------------


def test_hint_names_the_other_accounts(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER, THIRD]))

    hint = account_directory.candidate_account_hint(CALLER, account_directory.HINT_403)

    assert OTHER in hint
    assert THIRD in hint
    assert CALLER not in hint
    assert "No call was attempted" in hint


def test_404_hint_carries_the_unreachable_or_nonexistent_ambiguity(
    monkeypatch, tmp_path
):
    """Google's own ambiguity, stated as an ambiguity: notFound covers both."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    hint = account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)

    lowered = hint.lower()
    assert "cannot reach" in lowered
    assert "no such document exists" in lowered
    # It must NOT imply the document exists somewhere.
    assert "not evidence that the document exists" in lowered


def test_hint_is_empty_with_no_accounts(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)
        == ""
    )


def test_hint_is_empty_with_exactly_one_account(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER]))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_403)
        == ""
    )


def test_hint_is_empty_when_the_caller_is_not_in_the_store(monkeypatch, tmp_path):
    """ "The others" is only meaningful once the store covers the account in play."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [OTHER, THIRD]))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)
        == ""
    )


def test_hint_matches_the_caller_case_insensitively(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    hint = account_directory.candidate_account_hint(
        "Solo@Example.com", account_directory.HINT_403
    )

    assert OTHER in hint
    assert "Solo@Example.com" not in hint


def test_hint_is_empty_when_the_caller_account_is_unknown(monkeypatch, tmp_path):
    """``handle_http_errors`` substitutes 'N/A' when there is no email kwarg."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    assert (
        account_directory.candidate_account_hint("N/A", account_directory.HINT_404)
        == ""
    )
    assert (
        account_directory.candidate_account_hint(None, account_directory.HINT_404) == ""
    )


def test_hint_is_empty_in_gateway_mode(monkeypatch):
    """The store is shared across principals: naming them is a cross-tenant leak."""
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: True)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)

    def fail_if_called():
        raise AssertionError("the shared store must not be enumerated")

    _use_store(monkeypatch, SimpleNamespace(list_users=fail_if_called))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)
        == ""
    )


def test_hint_is_empty_in_oauth21_mode(monkeypatch):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: True)

    def fail_if_called():
        raise AssertionError("the shared store must not be enumerated")

    _use_store(monkeypatch, SimpleNamespace(list_users=fail_if_called))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_403)
        == ""
    )


def test_hint_is_empty_when_the_store_cannot_be_enumerated(monkeypatch):
    """An unreadable store is "cannot tell", never "there are no others"."""
    _single_user_mode(monkeypatch)

    def boom():
        raise PermissionError("Permission denied")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)
        == ""
    )


def test_hint_never_raises(monkeypatch):
    """An error path that throws while formatting an error hides the real one."""
    _single_user_mode(monkeypatch)

    def boom():
        raise RuntimeError("bucket on fire")

    monkeypatch.setattr(account_directory, "peek_credential_store", boom)

    assert (
        account_directory.candidate_account_hint(CALLER, account_directory.HINT_404)
        == ""
    )


def test_hint_never_raises_on_an_unknown_kind(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    assert account_directory.candidate_account_hint(CALLER, "no_such_kind") == ""


def test_preview_hint_leaves_enrollment_granularity_open(monkeypatch, tmp_path):
    """Per-account or per-Cloud-project is undocumented; do not resolve it here."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    hint = account_directory.candidate_account_hint(
        CALLER, account_directory.HINT_PREVIEW_UNAVAILABLE
    )

    assert OTHER in hint
    assert "No call was attempted" in hint
    lowered = hint.lower()
    assert "open question" in lowered
    assert "list_google_accounts" in hint


def test_http_error_account_hint_only_fires_for_403_and_404(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    assert OTHER in account_directory.http_error_account_hint(CALLER, 403)
    assert OTHER in account_directory.http_error_account_hint(CALLER, 404)
    for status in (400, 401, 429, 500, None):
        assert account_directory.http_error_account_hint(CALLER, status) == ""


# ---------------------------------------------------------------------------
# handle_http_errors — the messages tools actually raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_message_names_the_other_accounts(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))
    error = _http_error(404)

    message, _ = await _message(error)

    assert message.startswith(f"API error in get_doc_content: {error}")
    assert OTHER in message
    assert "No call was attempted" in message
    assert "not evidence that the document exists" in message.lower()


@pytest.mark.asyncio
async def test_403_message_names_the_other_accounts(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))
    error = _http_error(403, "The caller does not have permission")

    message, _ = await _message(error)

    assert message.startswith(_todays_message(error, status=403))
    assert OTHER in message
    assert "No call was attempted" in message


@pytest.mark.asyncio
async def test_404_message_is_unchanged_with_a_single_account(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER]))
    error = _http_error(404)

    message, _ = await _message(error)

    assert message == _todays_message(error)


@pytest.mark.asyncio
async def test_404_message_is_unchanged_with_no_accounts(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path))
    error = _http_error(404)

    message, _ = await _message(error)

    assert message == _todays_message(error)


@pytest.mark.asyncio
async def test_403_message_is_unchanged_with_a_single_account(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER]))
    error = _http_error(403, "The caller does not have permission")

    message, _ = await _message(error)

    assert message == _todays_message(error, status=403)


@pytest.mark.asyncio
async def test_401_message_is_left_alone(monkeypatch, tmp_path):
    """A 401 is "nobody is authenticated", not "the wrong identity"."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))
    error = _http_error(401, "Invalid Credentials")

    message, _ = await _message(error)

    assert OTHER not in message
    assert "No call was attempted" not in message


@pytest.mark.asyncio
async def test_other_statuses_are_left_alone(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))
    error = _http_error(429, "Quota exceeded")

    message, _ = await _message(error)

    assert message == f"API error in get_doc_content: {error}"


@pytest.mark.asyncio
async def test_gateway_mode_leaks_no_addresses_into_the_error(monkeypatch, tmp_path):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: True)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)
    monkeypatch.setattr(utils, "is_oauth21_enabled", lambda: False)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))
    error = _http_error(404)

    message, _ = await _message(error)

    assert message == _todays_message(error)


@pytest.mark.asyncio
async def test_a_broken_hint_falls_back_to_todays_message(monkeypatch, tmp_path):
    """The real failure must survive a bug in the affordance lookup."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _store(tmp_path, [CALLER, OTHER]))

    def boom(*args, **kwargs):
        raise RuntimeError("hint builder exploded")

    monkeypatch.setattr(account_directory, "enumerate_accounts", boom)
    error = _http_error(404)

    message, raised = await _message(error)

    assert message == _todays_message(error)
    assert raised.__cause__ is error


# ---------------------------------------------------------------------------
# The guarantee: no path retries under a second account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_error_path_ever_fetches_another_accounts_credentials(
    monkeypatch, tmp_path
):
    """The central constraint of the whole feature, asserted directly.

    Naming the alternatives must never become trying them: on every error path
    that grew an affordance, the credential store is asked for nobody's
    credentials and the failing API call is issued exactly once.
    """
    from gdocs_preview import preview_status, write_tools

    _single_user_mode(monkeypatch)
    store = _store(tmp_path, [CALLER, OTHER], recording=True)
    _use_store(monkeypatch, store)
    preview_status.reset()

    for status in (404, 403):
        error = _http_error(status)
        tool, calls = _tool(error)
        with pytest.raises(Exception) as excinfo:
            await tool(user_google_email=CALLER, document_id="DOCID")
        assert OTHER in str(excinfo.value), "the affordance must have fired"
        assert calls == [CALLER], "the failed call must run once, under one account"

    service = MagicMock()
    execute = service.documents.return_value.batchUpdate.return_value.execute
    resp = MagicMock()
    resp.status = 400
    resp.reason = "mock"
    execute.side_effect = HttpError(resp=resp, content=NOT_ENROLLED_BODY, uri=DOC_URI)
    with pytest.raises(utils.UserInputError) as excinfo:
        await write_tools._execute_preview_batch_update(
            service,
            "suggest_doc_edit",
            "DOCID",
            [{"insertComment": {}}],
            user_google_email=CALLER,
        )
    assert OTHER in str(excinfo.value), "the affordance must have fired"
    assert execute.call_count == 1, "the preview write must not be re-attempted"

    preview_status.reset()
    assert store.credential_requests == [], (
        "an error path asked the credential store for credentials; naming the "
        "other accounts must never become calling under them"
    )
