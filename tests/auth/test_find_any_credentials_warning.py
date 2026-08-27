"""AC7 — ``_find_any_credentials`` picks ``users[0]``; it must say so out loud.

With two accounts stored and no ``USER_GOOGLE_EMAIL`` configured, single-user
mode binds to whichever email sorts first. That stays true (this is a
diagnostic, not a new failure mode) but it must no longer be silent.
"""

import logging

import auth.google_auth as google_auth


class _FakeStore:
    def __init__(self, users, credentials=None):
        self._users = list(users)
        self._credentials = credentials or {}

    def list_users(self):
        return list(self._users)

    def get_credential(self, user_email):
        return self._credentials.get(user_email)


def _install(monkeypatch, store):
    monkeypatch.setattr("auth.google_auth.get_credential_store", lambda: store)


def test_multiple_accounts_still_pick_the_first_but_warn(monkeypatch, caplog):
    sentinel = object()
    store = _FakeStore(
        ["amy@example.com", "zoe@example.com"],
        {"amy@example.com": sentinel, "zoe@example.com": object()},
    )
    _install(monkeypatch, store)

    with caplog.at_level(logging.WARNING):
        credentials, email = google_auth._find_any_credentials()

    assert credentials is sentinel
    assert email == "amy@example.com"

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an arbitrary account pick must be announced"
    text = " ".join(r.getMessage() for r in warnings)
    assert "amy@example.com" in text
    assert "zoe@example.com" in text
    assert "USER_GOOGLE_EMAIL" in text


def test_single_account_does_not_warn(monkeypatch, caplog):
    sentinel = object()
    store = _FakeStore(["amy@example.com"], {"amy@example.com": sentinel})
    _install(monkeypatch, store)

    with caplog.at_level(logging.WARNING):
        credentials, email = google_auth._find_any_credentials()

    assert credentials is sentinel
    assert email == "amy@example.com"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_no_accounts_does_not_warn(monkeypatch, caplog):
    _install(monkeypatch, _FakeStore([]))

    with caplog.at_level(logging.WARNING):
        credentials, email = google_auth._find_any_credentials()

    assert credentials is None
    assert email is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_warning_fires_even_when_the_chosen_credential_fails_to_load(
    monkeypatch, caplog
):
    store = _FakeStore(["amy@example.com", "zoe@example.com"], {})
    _install(monkeypatch, store)

    with caplog.at_level(logging.WARNING):
        credentials, email = google_auth._find_any_credentials()

    assert credentials is None
    assert email is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "zoe@example.com" in text
