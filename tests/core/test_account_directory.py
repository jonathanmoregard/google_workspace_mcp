"""Tests for the fork-owned multi-account directory.

The most important test in this file is
``test_single_account_instructions_are_byte_identical``: this fork stays
merge-friendly with upstream, and with exactly one authenticated account the
server instructions string must not move by a single byte.
"""

import json
import os
import socket
import sys
from types import SimpleNamespace

import pytest

import core.account_directory as account_directory
import core.server  # noqa: F401 - _tool_registered reads the server from sys.modules
from auth.credential_store import LocalDirectoryCredentialStore

# The verbatim single-account instructions as of core/server.py:310-312. Written
# out longhand (not built from the module under test) so that a change to the
# builder cannot quietly change what "unchanged" means.
EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS = (
    "Connected Google account: solo@example.com\n"
    "\n"
    "When using Google Workspace tools, always use `solo@example.com` as the "
    "`user_google_email` parameter. Do not ask the user for their email address."
)


def _local_store(tmp_path, emails=(), *, create=True):
    """A real LocalDirectoryCredentialStore holding ``emails``."""
    base_dir = tmp_path / "creds"
    if create:
        base_dir.mkdir(parents=True, exist_ok=True)
    for email in emails:
        (base_dir / f"{email}.json").write_text("{}", encoding="utf-8")
    return LocalDirectoryCredentialStore(base_dir=str(base_dir))


def _use_store(monkeypatch, store):
    monkeypatch.setattr(account_directory, "peek_credential_store", lambda: store)


def _single_user_mode(monkeypatch):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)


def _no_preview_module(monkeypatch):
    monkeypatch.delitem(
        sys.modules, account_directory.PREVIEW_STATUS_MODULE, raising=False
    )


# --------------------------------------------------------------------------
# AC2 — server instructions
# --------------------------------------------------------------------------


def test_single_account_instructions_are_byte_identical(monkeypatch, tmp_path):
    """One authenticated account: the string must equal today's, byte for byte."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["solo@example.com"]))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_zero_accounts_keeps_the_single_account_instructions(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_one_stored_account_that_is_not_the_default_stays_single_account(
    monkeypatch, tmp_path
):
    """Multi-account output appears only when the store really holds >1 account."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["other@example.com"]))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_no_configured_default_yields_no_instructions(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["a@example.com", "b@example.com"]))

    assert account_directory.build_server_instructions(None) is None
    assert account_directory.build_server_instructions("") is None


def test_gateway_mode_yields_no_instructions(monkeypatch, tmp_path):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: True)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)
    _use_store(monkeypatch, _local_store(tmp_path, ["a@example.com", "b@example.com"]))

    assert account_directory.build_server_instructions("solo@example.com") is None


def test_multi_account_instructions_name_default_and_others(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(
        monkeypatch,
        _local_store(tmp_path, ["solo@example.com", "work@example.com"]),
    )

    instructions = account_directory.build_server_instructions("solo@example.com")

    assert instructions != EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    assert instructions.startswith("Connected Google account: solo@example.com")
    assert "work@example.com" in instructions


def test_multi_account_instructions_state_the_routing_rule(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(
        monkeypatch,
        _local_store(tmp_path, ["solo@example.com", "work@example.com"]),
    )

    instructions = account_directory.build_server_instructions("solo@example.com")

    lowered = instructions.lower()
    # use the default
    assert "solo@example.com" in instructions
    # switch only on an explicit instruction from the user
    assert "explicit" in lowered
    # never retry another account automatically
    assert "never retry" in lowered


def test_instructions_fall_back_when_resolving_the_store_raises_value_error(
    monkeypatch,
):
    """A GCS deployment must not fail to start because of this feature."""
    _single_user_mode(monkeypatch)

    def boom():
        raise ValueError("GCSCredentialStore requires MCP_ENABLE_OAUTH21=true.")

    monkeypatch.setattr(account_directory, "peek_credential_store", boom)

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_instructions_fall_back_when_list_users_raises_not_implemented(monkeypatch):
    _single_user_mode(monkeypatch)

    def boom():
        raise NotImplementedError("GCSCredentialStore does not support listing users.")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_instructions_fall_back_on_an_unexpected_store_exception(monkeypatch):
    _single_user_mode(monkeypatch)

    def boom():
        raise RuntimeError("bucket on fire")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_instructions_fall_back_when_the_store_is_unreadable(monkeypatch, tmp_path):
    """An unreadable store reads as "cannot tell", never as "many accounts"."""
    _single_user_mode(monkeypatch)

    def boom():
        raise PermissionError("Permission denied")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


# --------------------------------------------------------------------------
# AC1 — enumeration
# --------------------------------------------------------------------------


def test_enumerate_accounts_returns_sorted_emails(monkeypatch, tmp_path):
    _use_store(
        monkeypatch, _local_store(tmp_path, ["zoe@example.com", "amy@example.com"])
    )

    directory = account_directory.enumerate_accounts()

    assert directory.status == account_directory.STORE_OK
    assert directory.emails == ("amy@example.com", "zoe@example.com")


def test_enumerate_accounts_reports_a_missing_directory_as_zero_accounts(
    monkeypatch, tmp_path
):
    _use_store(monkeypatch, _local_store(tmp_path, create=False))

    directory = account_directory.enumerate_accounts()

    assert directory.emails == ()
    assert directory.status == account_directory.STORE_OK
    assert "does not exist" in (directory.detail or "")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can read a 0o000 directory",
)
def test_enumerate_accounts_distinguishes_unreadable_from_empty(monkeypatch, tmp_path):
    """list_users() returns [] for both; the report must not say "0 accounts"."""
    store = _local_store(tmp_path, ["a@example.com"])
    os.chmod(store.base_dir, 0o000)
    try:
        assert store.list_users() == []  # the ambiguity we are guarding against
        _use_store(monkeypatch, store)

        directory = account_directory.enumerate_accounts()
    finally:
        os.chmod(store.base_dir, 0o700)

    assert directory.emails == ()
    assert directory.status == account_directory.STORE_UNREADABLE
    assert directory.detail


def test_enumerate_accounts_reports_gcs_not_implemented(monkeypatch):
    def boom():
        raise NotImplementedError("GCSCredentialStore does not support listing users.")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    directory = account_directory.enumerate_accounts()

    assert directory.emails == ()
    assert directory.status == account_directory.STORE_UNSUPPORTED
    assert "listing users" in (directory.detail or "")


def test_enumerate_accounts_reports_an_unavailable_store(monkeypatch):
    def boom():
        raise ValueError("Unsupported WORKSPACE_MCP_CREDENTIAL_STORE_BACKEND: 'nope'.")

    monkeypatch.setattr(account_directory, "peek_credential_store", boom)

    directory = account_directory.enumerate_accounts()

    assert directory.emails == ()
    assert directory.status == account_directory.STORE_UNAVAILABLE
    assert "Unsupported" in (directory.detail or "")


# --------------------------------------------------------------------------
# AC1 — Developer Preview capability, read from cache only
# --------------------------------------------------------------------------


def test_preview_capability_is_unknown_when_docs_preview_is_not_loaded(monkeypatch):
    _no_preview_module(monkeypatch)

    capability = account_directory.preview_capability("solo@example.com")

    assert capability == {
        "availability": "unknown",
        "source": None,
        "checked_at": None,
    }


def test_preview_capability_reads_the_loaded_module(monkeypatch):
    fake = SimpleNamespace(
        get_status=lambda email: {
            "availability": "available",
            "evidence": {"message": "doc-id-1234"},
            "source": "probe",
            "checked_at": "2026-08-25T10:00:00+0000",
        }
    )
    monkeypatch.setitem(sys.modules, account_directory.PREVIEW_STATUS_MODULE, fake)

    capability = account_directory.preview_capability("solo@example.com")

    assert capability["availability"] == "available"
    assert capability["source"] == "probe"
    assert capability["checked_at"] == "2026-08-25T10:00:00+0000"
    # evidence carries document ids: it is deliberately not reported here.
    assert "evidence" not in capability


def test_preview_capability_never_imports_gdocs_preview(monkeypatch):
    """Importing gdocs_preview from core would be a cycle AND would register
    all seven preview tools as a decorator side effect."""
    _no_preview_module(monkeypatch)
    real_import = __import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith("gdocs_preview"), f"must not import {name}"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    assert account_directory.preview_capability("solo@example.com")["availability"] == (
        "unknown"
    )


def test_preview_capability_survives_a_broken_status_module(monkeypatch):
    def boom(email):  # noqa: ARG001
        raise RuntimeError("status table corrupted")

    monkeypatch.setitem(
        sys.modules,
        account_directory.PREVIEW_STATUS_MODULE,
        SimpleNamespace(get_status=boom),
    )

    assert (
        account_directory.preview_capability("solo@example.com")["availability"]
        == "unknown"
    )


# --------------------------------------------------------------------------
# AC1 — the report
# --------------------------------------------------------------------------


def test_report_lists_accounts_and_marks_the_default(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    report = account_directory.build_account_report("solo@example.com")

    assert report["identity_mode"] == "single_user"
    assert report["default_account"] == "solo@example.com"
    assert report["accounts_enumerated"] is True
    assert [a["email"] for a in report["accounts"]] == [
        "solo@example.com",
        "work@example.com",
    ]
    assert [a["is_default"] for a in report["accounts"]] == [True, False]
    assert all(
        a["docs_preview"]["availability"] == "unknown" for a in report["accounts"]
    )
    assert report["probed"] is False


def test_report_flags_an_unreadable_store_instead_of_reporting_zero_accounts(
    monkeypatch,
):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)

    def boom():
        raise PermissionError("Permission denied: /creds")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    report = account_directory.build_account_report("solo@example.com")

    assert report["accounts"] == []
    assert report["accounts_enumerated"] is False
    assert report["store_status"] != account_directory.STORE_OK
    assert any("could not be read" in note.lower() for note in report["notes"])


def test_report_flags_a_backend_that_cannot_list_users(monkeypatch):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)

    def boom():
        raise NotImplementedError("GCSCredentialStore does not support listing users.")

    _use_store(monkeypatch, SimpleNamespace(list_users=boom))

    report = account_directory.build_account_report("solo@example.com")

    assert report["store_status"] == account_directory.STORE_UNSUPPORTED
    assert report["accounts_enumerated"] is False
    assert any("cannot list" in note.lower() for note in report["notes"])


def test_report_says_capability_is_unknown_not_unavailable_without_docs_preview(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["solo@example.com"]))

    report = account_directory.build_account_report("solo@example.com")

    assert report["docs_preview_loaded"] is False
    assert report["accounts"][0]["docs_preview"]["availability"] == "unknown"
    note = " ".join(report["notes"]).lower()
    assert "unknown" in note
    assert "unavailable" not in note


def test_report_notes_a_configured_default_with_no_stored_credentials(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["work@example.com", "home@example.com"])
    )

    report = account_directory.build_account_report("missing@example.com")

    assert any("missing@example.com" in note for note in report["notes"])


def test_report_carries_the_no_auto_switch_rule_when_several_accounts(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    report = account_directory.build_account_report("solo@example.com")

    assert any("never retry" in note.lower() for note in report["notes"])


def test_report_does_not_enumerate_in_gateway_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: True)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: False)

    def fail_if_called():
        raise AssertionError("the shared store must not be enumerated in gateway mode")

    _use_store(monkeypatch, SimpleNamespace(list_users=fail_if_called))

    report = account_directory.build_account_report("solo@example.com")

    assert report["identity_mode"] == "trusted_gateway"
    assert report["accounts"] == []
    assert report["accounts_enumerated"] is False
    assert report["default_account"] is None
    note = " ".join(report["notes"]).lower()
    assert "gateway" in note
    assert "cannot choose" in note


def test_report_does_not_enumerate_in_oauth21_mode(monkeypatch):
    monkeypatch.setattr(account_directory, "is_trust_gateway_identity", lambda: False)
    monkeypatch.setattr(account_directory, "is_oauth21_enabled", lambda: True)

    def fail_if_called():
        raise AssertionError(
            "the shared store must not be enumerated in OAuth 2.1 mode"
        )

    _use_store(monkeypatch, SimpleNamespace(list_users=fail_if_called))

    report = account_directory.build_account_report(None)

    assert report["identity_mode"] == "oauth21_multi_user"
    assert report["accounts_enumerated"] is False


def test_render_account_report_is_json(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["solo@example.com"]))

    payload = json.loads(account_directory.render_account_report("solo@example.com"))

    assert payload["accounts"][0]["email"] == "solo@example.com"


def test_report_opens_no_sockets(monkeypatch, tmp_path):
    """Zero network calls. Never probes."""
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    class _NoSocket(socket.socket):
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("list_google_accounts must not open a socket")

    monkeypatch.setattr(socket, "socket", _NoSocket)

    assert account_directory.render_account_report("solo@example.com")


# --------------------------------------------------------------------------
# AC7 — the arbitrary-pick warning
# --------------------------------------------------------------------------


def test_arbitrary_pick_warning_names_every_account(caplog):
    with caplog.at_level("WARNING", logger=account_directory.__name__):
        account_directory.warn_on_arbitrary_account_pick(
            ["amy@example.com", "zoe@example.com"], "amy@example.com"
        )

    message = caplog.text
    assert "amy@example.com" in message
    assert "zoe@example.com" in message
    assert "USER_GOOGLE_EMAIL" in message


def test_arbitrary_pick_warning_is_silent_for_a_single_account(caplog):
    with caplog.at_level("WARNING", logger=account_directory.__name__):
        account_directory.warn_on_arbitrary_account_pick(
            ["amy@example.com"], "amy@example.com"
        )

    assert caplog.text == ""


def test_arbitrary_pick_warning_is_silent_for_no_accounts(caplog):
    with caplog.at_level("WARNING", logger=account_directory.__name__):
        account_directory.warn_on_arbitrary_account_pick([], None)

    assert caplog.text == ""


# --------------------------------------------------------------------------
# F3 — one address, one account, however it is spelled
#
# `other_accounts()` folded case while `is_default`, the default-missing note
# and the instructions filter compared exactly. A case-variant
# USER_GOOGLE_EMAIL therefore reported "no stored credentials" AND listed the
# default as an account to switch to. Two spellings of one address must not
# become two accounts.
# --------------------------------------------------------------------------


def test_case_variant_default_is_recognised_as_the_default(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["Solo@Example.com"]))

    report = account_directory.build_account_report("solo@example.com")

    assert [account["is_default"] for account in report["accounts"]] == [True]
    assert not any("has no stored credentials" in note for note in report["notes"])


def test_case_variant_default_is_not_listed_as_another_account(monkeypatch, tmp_path):
    """The store's spelling differs from USER_GOOGLE_EMAIL's; still one account."""
    _single_user_mode(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["Solo@Example.com"]))

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_two_spellings_of_one_address_are_not_two_accounts(monkeypatch, tmp_path):
    """A store holding both spellings must not produce multi-account output."""
    _single_user_mode(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "SOLO@example.com"])
    )

    assert (
        account_directory.build_server_instructions("solo@example.com")
        == EXPECTED_SINGLE_ACCOUNT_INSTRUCTIONS
    )


def test_other_accounts_folds_case_for_the_caller_and_the_others(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(
        monkeypatch,
        _local_store(
            tmp_path, ["Solo@Example.com", "solo@example.com", "work@example.com"]
        ),
    )

    others = account_directory.other_accounts("SOLO@EXAMPLE.COM")

    assert others == ("work@example.com",)


def test_a_default_absent_from_the_store_is_still_reported_missing(
    monkeypatch, tmp_path
):
    """The folding fix must not swallow the genuinely-missing-default note."""
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["work@example.com"]))

    report = account_directory.build_account_report("solo@example.com")

    assert any("has no stored credentials" in note for note in report["notes"])


# --------------------------------------------------------------------------
# F4 — never name a tool this process did not register
#
# `list_google_accounts` is tiered `docs: core`, so `--tools calendar
# --tool-tier core` drops it while the instructions and the preview hint still
# told the agent to call it.
# --------------------------------------------------------------------------


def _tier_filter(monkeypatch, enabled):
    """Pin which tools are REGISTERED on the server, for one test.

    Registration, not ``core.tool_registry._enabled_tools``: that set is
    ``None`` — "no tier filtering", hence True for every name — in the default
    configuration and under a plain ``--tools`` selection, and it cannot see
    read-only or granular-permissions filtering, or a service that was never
    imported. The component table is where all of those land, so it is what
    :func:`core.account_directory._tool_registered` consults.

    ``core.server`` is imported at the top of this module on purpose:
    ``_tool_registered`` reads the server out of ``sys.modules`` and answers
    False when no server has been built, so without that import these tests
    would pass for the wrong reason.
    """
    import core.tool_registry as tool_registry

    monkeypatch.setattr(
        tool_registry,
        "get_tool_components",
        lambda server: {name: object() for name in enabled},
    )


def test_instructions_name_the_tool_when_it_is_registered(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _tier_filter(
        monkeypatch, {"list_google_accounts", "check_docs_review_capabilities"}
    )
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    instructions = account_directory.build_server_instructions("solo@example.com")

    assert "list_google_accounts" in instructions


def test_instructions_omit_the_tool_when_tier_filtering_dropped_it(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _tier_filter(monkeypatch, {"get_events", "list_calendars"})
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    instructions = account_directory.build_server_instructions("solo@example.com")

    assert "list_google_accounts" not in instructions
    # The routing rule is the load-bearing half and must survive.
    assert "work@example.com" in instructions
    assert "never retry" in instructions.lower()


def test_preview_hint_omits_the_tool_when_tier_filtering_dropped_it(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _tier_filter(monkeypatch, {"get_events"})
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    hint = account_directory.candidate_account_hint(
        "solo@example.com", account_directory.HINT_PREVIEW_UNAVAILABLE
    )

    assert "work@example.com" in hint
    assert "list_google_accounts" not in hint
    # The open-question sentence is not the part being dropped.
    assert "open question" in hint


def test_preview_hint_names_the_tool_when_it_is_registered(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _tier_filter(monkeypatch, {"list_google_accounts"})
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )

    hint = account_directory.candidate_account_hint(
        "solo@example.com", account_directory.HINT_PREVIEW_UNAVAILABLE
    )

    assert "list_google_accounts" in hint


# --------------------------------------------------------------------------
# R2-F2 — no wording may license switching account on an error alone.
#
# The instructions offered two switch conditions: an explicit instruction from
# the user, OR "when a tool error names one of the other accounts as the one to
# use". Nothing this server emits ever designates an account that way — the
# candidate hint NAMES accounts and then demands the user confirm before any
# call. The second condition was therefore satisfiable only by misreading the
# hint, which is precisely the automatic switch the whole feature exists to
# prevent.
# --------------------------------------------------------------------------


def _multi_account_text(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["solo@example.com", "work@example.com"])
    )
    return account_directory.build_server_instructions("solo@example.com")


def test_instructions_never_license_a_switch_on_an_error_alone(monkeypatch, tmp_path):
    instructions = _multi_account_text(monkeypatch, tmp_path)

    assert "as the one to use" not in instructions
    # Whatever the wording, an error must route the agent back to the user.
    assert "confirm" in instructions.lower()


def test_routing_note_never_licenses_a_switch_on_an_error_alone():
    note = account_directory._ROUTING_NOTE

    assert "as the one to use" not in note
    assert "confirm" in note.lower()


def test_the_candidate_hint_and_the_instructions_agree(monkeypatch, tmp_path):
    """The hint demands confirmation; the instructions must not contradict it."""
    instructions = _multi_account_text(monkeypatch, tmp_path)
    hint = account_directory.candidate_account_hint(
        "solo@example.com", account_directory.HINT_404
    )

    assert "confirm that with the user" in hint
    assert "confirm" in instructions.lower()


# --------------------------------------------------------------------------
# Round 3 — the fold rule is about IDENTITY, not about loadability
#
# The credential store keys its files by the exact spelling it was given. So
# folding, which decides whether two spellings are one account, must not be
# read as deciding whether a credential can be loaded under either of them.
# Marking a case-variant default "authenticated" while every call under it
# failed to find a file is a worse answer than the one it replaced.
# --------------------------------------------------------------------------


def test_a_case_variant_default_is_reported_as_unloadable(monkeypatch, tmp_path):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["Solo@Example.com"]))

    report = account_directory.build_account_report("solo@example.com")
    note = " ".join(report["notes"])

    assert "differs only in case" in note
    # It must name the spelling that actually works.
    assert "Set USER_GOOGLE_EMAIL to Solo@Example.com" in note
    # And it is still the same account, so it is still the default.
    assert [a["is_default"] for a in report["accounts"]] == [True]


def test_an_exactly_matching_default_gets_no_case_warning(monkeypatch, tmp_path):
    """The control: the warning must not fire for the ordinary case."""
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(monkeypatch, _local_store(tmp_path, ["solo@example.com"]))

    note = " ".join(account_directory.build_account_report("solo@example.com")["notes"])

    assert "differs only in case" not in note
    assert "has no stored credentials" not in note


# --------------------------------------------------------------------------
# Round 3 — "use the default account" is not advice when there is no default
# --------------------------------------------------------------------------


def test_routing_note_tells_the_agent_to_ask_when_there_is_no_default(
    monkeypatch, tmp_path
):
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["amy@example.com", "zoe@example.com"])
    )

    note = " ".join(account_directory.build_account_report(None)["notes"])

    assert "Ask the user which account to use" in note
    assert "do not choose one yourself" in note
    # The agent must not be told to use a default that does not exist.
    assert "Use the default account" not in note
    # And the no-retry rule survives in this variant too.
    assert "Never retry" in note


def test_routing_note_names_the_default_when_there_is_one(monkeypatch, tmp_path):
    """The control: with a default configured the original note is used."""
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _use_store(
        monkeypatch, _local_store(tmp_path, ["amy@example.com", "zoe@example.com"])
    )

    note = " ".join(account_directory.build_account_report("amy@example.com")["notes"])

    assert "Use the default account" in note
    assert "Ask the user which account to use" not in note


# --------------------------------------------------------------------------
# Round 3 — _tool_registered asks about registration, not the selection set
# --------------------------------------------------------------------------


def test_tool_is_not_named_when_the_service_was_never_imported(monkeypatch, tmp_path):
    """The case the selection set cannot see.

    ``--tools docs`` leaves ``_enabled_tools`` as None, so the old check
    answered True for every name — including tools belonging to a service that
    was never imported and therefore registered nothing at all.
    """
    _single_user_mode(monkeypatch)
    _no_preview_module(monkeypatch)
    _tier_filter(monkeypatch, {"get_doc_content", "list_google_accounts"})
    _use_store(monkeypatch, _local_store(tmp_path, ["solo@example.com"]))

    note = " ".join(account_directory.build_account_report("solo@example.com")["notes"])

    assert "check_docs_review_capabilities" not in note


def test_nothing_is_named_when_no_server_has_been_built(monkeypatch):
    """Fails closed: no server in the process means no claim about its tools."""
    monkeypatch.delitem(sys.modules, "core.server", raising=False)

    assert account_directory._tool_registered("list_google_accounts") is False
