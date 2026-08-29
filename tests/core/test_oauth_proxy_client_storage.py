"""The OAuth-proxy Valkey client store must never fail open.

``configure_server_for_http`` builds the store that FastMCP's OAuth proxy uses
to persist dynamically registered client credentials. That store is supposed to
be Fernet-encrypted at rest. The Valkey branch used to construct the raw
``ValkeyStore`` first and wrap it only later, with the ``ImportError`` and
``ValueError`` handlers sitting *between* the two — so any failure in the
encryption setup left the unencrypted store bound while the log claimed a
fallback had happened.

The disk-backed branch had the same defect and is fixed the same way; both
branches now obey one contract.

These tests pin the distinct failure causes apart:

* missing store dependency -> warn (with the install hint) and leave
  ``client_storage`` unset, so FastMCP uses its own default;
* malformed configuration -> abort startup naming the offending environment
  variable, restoring ``parse_bool_env``'s loud-failure contract;
* rejected storage-encryption key -> abort startup, logged as an encryption
  failure and re-raised;
* TLS requested but unappliable -> abort startup, rather than connecting in
  cleartext while the log says otherwise.

In none of them may an unencrypted store reach ``GoogleProvider``. Separately,
a key *rotation* must degrade to a cache miss rather than raising, matching
FastMCP's own default store.
"""

import logging
import sys
from types import ModuleType, SimpleNamespace

import pytest

import core.server as server_module

VALKEY_MODULE = "key_value.aio.stores.valkey"
GLIDE_CONFIG_MODULE = "glide_shared.config"

_PROXY_ENV_VARS = (
    "WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_DB",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USERNAME",
    "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PASSWORD",
    "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY",
    "WORKSPACE_MCP_ALLOWED_CLIENT_REDIRECT_URIS",
    "FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY",
)


class FakeValkeyStore:
    """Stand-in for ``ValkeyStore``; the real one needs the valkey extra.

    The async methods exist only so the object satisfies the runtime-checkable
    ``AsyncKeyValue`` protocol that ``FernetEncryptionWrapper`` beartype-checks;
    nothing here is ever awaited.
    """

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        # The real ValkeyStore exposes this private Glide config, and the server
        # tunes TLS and timeouts through it. Having it here by default means the
        # ordinary tests exercise the branch that actually runs in production.
        self._client_config = SimpleNamespace(
            use_tls=False,
            request_timeout=None,
            advanced_config=None,
        )
        type(self).instances.append(self)

    async def get(self, key, *, collection=None):  # pragma: no cover - protocol
        return None

    async def ttl(self, key, *, collection=None):  # pragma: no cover - protocol
        return (None, None)

    async def put(
        self, key, value, *, collection=None, ttl=None
    ):  # pragma: no cover - protocol
        return None

    async def delete(self, key, *, collection=None):  # pragma: no cover - protocol
        return False

    async def get_many(self, keys, *, collection=None):  # pragma: no cover - protocol
        return [None for _ in keys]

    async def ttl_many(self, keys, *, collection=None):  # pragma: no cover - protocol
        return [(None, None) for _ in keys]

    async def put_many(
        self, keys, values, *, collection=None, ttl=None
    ):  # pragma: no cover - protocol
        return None

    async def delete_many(
        self, keys, *, collection=None
    ):  # pragma: no cover - protocol
        return 0


class FakeValkeyStoreWithoutGlideConfig(FakeValkeyStore):
    """Variant that does NOT expose the private Glide config the server tunes.

    ``ValkeyStore`` really does carry ``_client_config`` today; this models the
    day it stops, which is the only way the server's TLS and timeout settings
    can go unapplied.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        try:
            del self._client_config
        except AttributeError:  # pragma: no cover - defensive
            pass


@pytest.fixture
def captured(monkeypatch):
    """Stub out everything around the storage block and capture provider kwargs."""
    captured_kwargs = {}

    class FakeGoogleProvider:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.client_registration_options = SimpleNamespace(default_scopes=None)
            self._default_scope_str = ""
            self._cimd_manager = SimpleNamespace(default_scope="")

    for name in _PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(FakeValkeyStore, "instances", [])

    monkeypatch.setattr(server_module, "get_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(server_module, "GoogleProvider", FakeGoogleProvider)
    monkeypatch.setattr(
        server_module,
        "get_current_scopes",
        lambda: ["https://www.googleapis.com/auth/userinfo.email", "openid"],
    )
    monkeypatch.setattr(server_module, "set_auth_provider", lambda provider: None)
    monkeypatch.setattr(server_module, "_auth_provider", server_module._auth_provider)
    monkeypatch.setattr(server_module.server, "auth", server_module.server.auth)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            is_oauth21_enabled=lambda: True,
            is_configured=lambda: True,
            is_public_client=lambda: False,
            is_external_oauth21_provider=lambda: False,
            client_id="client-id",
            client_secret="client-secret-with-plenty-of-entropy",
            get_oauth_base_url=lambda: "https://workspace-mcp.example.test",
            redirect_path="/oauth2callback",
        ),
    )
    return captured_kwargs


class FakeAdvancedGlideClientConfiguration:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStore, with_glide=True):
    """Make the Valkey backend importable; the venv lacks the valkey extra."""
    module = ModuleType(VALKEY_MODULE)
    module.ValkeyStore = store_cls
    monkeypatch.setitem(sys.modules, VALKEY_MODULE, module)
    if with_glide:
        glide = ModuleType("glide_shared")
        glide_config = ModuleType(GLIDE_CONFIG_MODULE)
        glide_config.AdvancedGlideClientConfiguration = (
            FakeAdvancedGlideClientConfiguration
        )
        glide.config = glide_config
        monkeypatch.setitem(sys.modules, "glide_shared", glide)
        monkeypatch.setitem(sys.modules, GLIDE_CONFIG_MODULE, glide_config)


def _select_valkey(monkeypatch, host="localhost"):
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "valkey")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", host)


def test_valkey_store_is_wrapped_in_encryption_on_the_happy_path(monkeypatch, captured):
    """Baseline: the store handed to the provider is the encrypted wrapper."""
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)

    server_module.configure_server_for_http()

    client_storage = captured["client_storage"]
    assert isinstance(client_storage, FernetEncryptionWrapper)
    assert isinstance(client_storage.key_value, FakeValkeyStore)


def test_glide_tls_and_timeouts_are_still_applied_to_the_wrapped_store(
    monkeypatch, captured
):
    """Moving the Glide import earlier must not drop the tuning it enables."""
    _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStore)
    _select_valkey(monkeypatch, host="valkey.internal.example")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    server_module.configure_server_for_http()

    store = captured["client_storage"].key_value
    assert store._client_config.use_tls is True
    assert store._client_config.request_timeout == 5000
    assert store._client_config.advanced_config.kwargs == {"connection_timeout": 10000}


def test_missing_glide_config_aborts_when_tls_was_requested(
    monkeypatch, captured, caplog
):
    """A dropped TLS setting must never pass silently.

    Degrading is right for a timeout and wrong for transport encryption: the
    connection would be made in cleartext while the startup line below still
    reported ``tls=True``.
    """
    _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStoreWithoutGlideConfig)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        with pytest.raises(RuntimeError, match="would be made in cleartext"):
            server_module.configure_server_for_http()

    assert "client_storage" not in captured
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "tls=True" not in messages


def test_missing_glide_config_without_tls_warns_and_drops_only_timeouts(
    monkeypatch, captured, caplog
):
    """With no TLS requested, the same condition costs only the tuning knobs.

    The timeouts are set explicitly here so the nulling in the server actually
    has something to null: with both variables unset the "timeout set to"
    assertion below would pass even if those lines were deleted.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStoreWithoutGlideConfig)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", "1234")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", "4321")

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert isinstance(captured["client_storage"], FernetEncryptionWrapper)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "_client_config" in messages
    assert "tls=False" in messages
    assert "timeout set to" not in messages, (
        "a timeout that was not applied must not be logged as if it were"
    )


def test_unwritable_use_tls_aborts_when_tls_was_requested(
    monkeypatch, captured, caplog
):
    """A renamed ``use_tls`` must fail loudly, not create a dead attribute.

    ``glide_config.use_tls = True`` succeeds on any plain object, so the
    assignment running is not evidence that TLS is on. Without the presence
    check the connection would be cleartext while the line below reports
    ``tls=True``.
    """

    class ConfigWithRenamedTlsField:
        def __init__(self):
            self.tls_enabled = False  # the hypothetical new name
            self.request_timeout = None
            self.advanced_config = None

    class StoreWithRenamedTlsField(FakeValkeyStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client_config = ConfigWithRenamedTlsField()

    _install_fake_valkey(monkeypatch, store_cls=StoreWithRenamedTlsField)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        with pytest.raises(RuntimeError, match="would be made in cleartext"):
            server_module.configure_server_for_http()

    assert "client_storage" not in captured
    assert "tls=True" not in "\n".join(r.getMessage() for r in caplog.records)


def test_use_tls_that_does_not_stick_aborts_when_tls_was_requested(
    monkeypatch, captured
):
    """The read-back also catches a config that accepts writes and ignores them."""

    class ConfigThatIgnoresTlsWrites:
        def __init__(self):
            self.request_timeout = None
            self.advanced_config = None

        @property
        def use_tls(self):
            return False

        @use_tls.setter
        def use_tls(self, value):  # accepts, discards
            pass

    class StoreThatIgnoresTlsWrites(FakeValkeyStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client_config = ConfigThatIgnoresTlsWrites()

    _install_fake_valkey(monkeypatch, store_cls=StoreThatIgnoresTlsWrites)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    with pytest.raises(RuntimeError, match="would be made in cleartext"):
        server_module.configure_server_for_http()

    assert "client_storage" not in captured


def test_read_only_use_tls_without_tls_requested_warns_instead_of_crashing(
    monkeypatch, captured, caplog
):
    """``hasattr`` passing does not mean the assignment will succeed.

    A read-only property, a frozen dataclass or ``__slots__`` all raise on the
    write. That must not escape and abort startup on the path whose contract is
    warn-and-continue: TLS was never requested here, so nothing security-
    relevant is lost.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    class ConfigWithReadOnlyTls:
        def __init__(self):
            self.request_timeout = None
            self.advanced_config = None

        @property
        def use_tls(self):
            return False

    class StoreWithReadOnlyTls(FakeValkeyStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client_config = ConfigWithReadOnlyTls()

    _install_fake_valkey(monkeypatch, store_cls=StoreWithReadOnlyTls)
    _select_valkey(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert isinstance(captured["client_storage"], FernetEncryptionWrapper)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "tls=False" in messages


def test_read_only_use_tls_still_aborts_when_tls_was_requested(monkeypatch, captured):
    """The other half of the contract stays intact."""

    class ConfigWithReadOnlyTls:
        def __init__(self):
            self.request_timeout = None
            self.advanced_config = None

        @property
        def use_tls(self):
            return False

    class StoreWithReadOnlyTls(FakeValkeyStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client_config = ConfigWithReadOnlyTls()

    _install_fake_valkey(monkeypatch, store_cls=StoreWithReadOnlyTls)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    with pytest.raises(RuntimeError, match="would be made in cleartext"):
        server_module.configure_server_for_http()

    assert "client_storage" not in captured


def test_unapplied_timeouts_are_not_logged_as_applied(monkeypatch, captured, caplog):
    """Same detection as the TLS guard, one line down.

    ``request_timeout`` and ``advanced_config`` were assigned with no presence
    check and no read-back, so a renamed field created a dead attribute while
    the startup log still reported the timeout as set. The outcome differs from
    TLS — a dead timeout warns rather than refusing — but the detection must not.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    class ConfigWithRenamedTimeouts:
        """Only ``use_tls`` survives under its old name."""

        def __init__(self):
            self.use_tls = False
            self.req_timeout = None  # was request_timeout
            self.advanced = None  # was advanced_config

    class StoreWithRenamedTimeouts(FakeValkeyStore):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client_config = ConfigWithRenamedTimeouts()

    _install_fake_valkey(monkeypatch, store_cls=StoreWithRenamedTimeouts)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", "1234")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", "4321")

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert isinstance(captured["client_storage"], FernetEncryptionWrapper), (
        "a dead timeout field must not cost the store, only the timeout"
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "timeout set to" not in messages, (
        "a timeout that did not take effect must not be reported as applied"
    )
    assert "request_timeout" in messages
    assert "advanced_config" in messages


@pytest.mark.parametrize("backend", ["valkeyy", "redis", "file", "diskk"])
def test_unknown_storage_backend_aborts_and_names_the_variable(
    monkeypatch, captured, backend
):
    """A typo'd backend used to match no branch and fall through silently.

    That put every replica on its own per-instance store while the operator
    believed they had configured a shared one — the same silent downgrade the
    rest of this block now refuses.
    """
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", backend)

    with pytest.raises(
        ValueError, match="WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND must be one of"
    ):
        server_module.configure_server_for_http()

    assert "client_storage" not in captured


def test_storage_backend_is_trimmed_and_case_insensitive(
    monkeypatch, captured, tmp_path
):
    """Validation must not reject what the existing normalisation accepts."""
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "  DISK ")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )

    server_module.configure_server_for_http()

    assert isinstance(captured["client_storage"], FernetEncryptionWrapper)


def test_rejected_encryption_key_does_not_leave_an_unencrypted_valkey_store(
    monkeypatch, captured, caplog
):
    """A ValueError out of ``Fernet(...)`` must abort, not fall back to plaintext.

    Before the fix this was caught by ``except ValueError``, which logged
    "falling back to default storage" while leaving the raw ``ValkeyStore``
    bound as ``client_storage``.
    """

    def _reject_key(*args, **kwargs):
        raise ValueError("Fernet key must be 32 url-safe base64-encoded bytes.")

    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setattr("cryptography.fernet.Fernet", _reject_key)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        with pytest.raises(ValueError):
            server_module.configure_server_for_http()

    assert "client_storage" not in captured, (
        "GoogleProvider must not be constructed at all once storage "
        "encryption has failed"
    )
    assert FakeValkeyStore.instances == [], (
        "the plaintext store must not even be built when its Fernet is rejected"
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "falling back to default storage" not in messages
    assert "encryption" in messages.lower()


def test_missing_glide_config_degrades_without_losing_the_store(
    monkeypatch, captured, caplog
):
    """The Glide timeout import used to run *after* ``ValkeyStore(...)``.

    An ``ImportError`` there was caught by the dependency handler, which logged
    "dependencies are not installed" and left the already-constructed,
    unencrypted store bound. It now runs before anything is built — and because
    it carries only the connection-timeout knob, its absence costs that knob
    rather than the shared store the operator explicitly asked for.
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStore, with_glide=False)
    # A non-loopback host makes the server apply default Glide timeouts, which
    # is what used to pull in ``glide_shared.config`` post-construction.
    _select_valkey(monkeypatch, host="valkey.internal.example")
    monkeypatch.setitem(sys.modules, "glide_shared", None)
    monkeypatch.setitem(sys.modules, GLIDE_CONFIG_MODULE, None)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    client_storage = captured["client_storage"]
    assert isinstance(client_storage, FernetEncryptionWrapper), (
        "a missing tuning symbol must not disqualify the requested store"
    )
    store = client_storage.key_value
    assert store._client_config.use_tls is False
    assert store._client_config.request_timeout == 5000
    assert store._client_config.advanced_config is None

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "AdvancedGlideClientConfiguration is unavailable" in messages
    assert "connection timeout cannot be applied" in messages
    assert "connection timeout set to" not in messages, (
        "a connection timeout that was not applied must not be logged as if it were"
    )


def test_missing_valkey_dependency_falls_back_without_a_store(
    monkeypatch, captured, caplog
):
    """Warn with the install hint and leave storage unset.

    ``setitem(..., None)`` rather than ``delitem``: deleting the entry only
    evicts the cache, so the next ``from ... import`` re-imports from disk and
    the test would pass for the wrong reason (this venv has no valkey extra) and
    flip the moment the extra is installed. A ``None`` entry makes the import
    machinery raise regardless of what is installed.
    """
    monkeypatch.setitem(sys.modules, VALKEY_MODULE, None)
    _select_valkey(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert captured["client_storage"] is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "dependencies are not installed" in messages
    assert "workspace-mcp[valkey]" in messages


def test_malformed_use_tls_flag_aborts_and_names_the_variable(
    monkeypatch, captured, caplog
):
    """``parse_bool_env`` fails loudly; this call site must not swallow it.

    Before the fix a typo'd ``WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS`` landed
    in the same ``except ValueError`` as a crypto failure, so the whole Valkey
    configuration was silently discarded and the operator was told something
    unrelated to what went wrong.
    """
    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "treu")

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        with pytest.raises(
            ValueError, match="WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS"
        ):
            server_module.configure_server_for_http()

    assert "client_storage" not in captured
    assert FakeValkeyStore.instances == []
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "falling back to default storage" not in messages


def test_malformed_port_aborts_and_names_the_variable(monkeypatch, captured):
    """The same applies to the integer settings parsed alongside it."""
    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT", "six-three-seven-nine")

    with pytest.raises(ValueError, match="WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT"):
        server_module.configure_server_for_http()

    assert "client_storage" not in captured
    assert FakeValkeyStore.instances == []


@pytest.mark.parametrize(
    "name",
    [
        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_PORT",
        "WORKSPACE_MCP_OAUTH_PROXY_VALKEY_DB",
    ],
)
def test_blank_setting_is_treated_as_unset(monkeypatch, captured, name):
    """One rule for blank across every one of these variables: blank == unset.

    ``docker compose`` substitutes an empty string for ``FOO=${FOO}`` when FOO
    is unset, so a blank value is a routine deployment artefact rather than a
    statement of intent. The typo this strictness exists to catch is always a
    non-empty value, and those still abort — see the tests above.
    """
    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setenv(name, "   ")

    server_module.configure_server_for_http()

    store = captured["client_storage"].key_value
    assert store.kwargs["port"] == 6379
    assert store.kwargs["db"] == 0


def test_blank_timeout_and_tls_settings_are_also_treated_as_unset(
    monkeypatch, captured
):
    """The same rule for the boolean and the no-default integers."""
    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", "  ")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", "")

    server_module.configure_server_for_http()

    store = captured["client_storage"].key_value
    # localhost, no TLS -> no default timeouts are applied either
    assert store._client_config.use_tls is False
    assert store._client_config.request_timeout is None
    assert store._client_config.advanced_config is None


def test_disk_store_is_wrapped_in_encryption_on_the_happy_path(
    monkeypatch, captured, tmp_path
):
    """Baseline for the disk-backed branch.

    Also pins that the store came from ``make_sanitized_file_store``: an
    ``isinstance`` check alone would still pass if the branch went back to
    building a bare ``FileTreeStore`` with no sanitization strategy.
    """
    from key_value.aio._utils.sanitization import HybridSanitizationStrategy
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.stores.filetree import FileTreeStore

    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )

    server_module.configure_server_for_http()

    client_storage = captured["client_storage"]
    assert isinstance(client_storage, FernetEncryptionWrapper)
    store = client_storage.key_value
    assert isinstance(store, FileTreeStore)
    assert isinstance(store._key_sanitization_strategy, HybridSanitizationStrategy)


def test_disk_import_failure_after_construction_leaves_no_plaintext_store(
    monkeypatch, captured, caplog, tmp_path
):
    """The disk branch used to have the same shape the Valkey branch had.

    Before the fix, ``client_storage`` was assigned the raw ``FileTreeStore``
    and only later
    reassigned to the encryption wrapper, with ``except ImportError`` sitting
    between the two. Any ``ImportError`` raised in that window — a lazy import
    inside a dependency is the realistic source — is caught by a handler that
    logs "Falling back to default storage" while the plaintext store stays
    bound.

    ``FernetEncryptionWrapper`` is re-imported from its own module on every
    call, so patching it there injects a single failure at exactly the point
    where the wrap was supposed to happen — after the plaintext store exists and
    after ``jwt_signing_key`` is already derived.
    """

    def _raise_import_error(**kwargs):
        raise ImportError("simulated lazy import failure inside the encryption wrapper")

    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )
    monkeypatch.setattr(
        "key_value.aio.wrappers.encryption.FernetEncryptionWrapper",
        _raise_import_error,
    )

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        with pytest.raises(ImportError):
            server_module.configure_server_for_http()

    assert "client_storage" not in captured, (
        "an ImportError in the disk branch must not leave the unencrypted "
        "FileTreeStore bound as client_storage"
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Falling back to" not in messages, (
        "the handler must not claim a fallback it did not perform"
    )


def test_missing_disk_dependency_falls_back_without_a_store(
    monkeypatch, captured, caplog, tmp_path
):
    """The disk branch's guarded import: warn and leave storage unset."""
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )
    monkeypatch.setitem(sys.modules, "core.storage", None)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert captured["client_storage"] is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Disk storage requested but its dependencies are not available" in messages
    assert "Fernet-encrypted local file store" in messages


# ---------------------------------------------------------------------------
# Key rotation: a stale record must degrade to a miss, not raise
# ---------------------------------------------------------------------------


def _configure_with_secret(monkeypatch, captured, secret, backing_store):
    """Build client_storage over ``backing_store`` using ``secret``."""
    module = ModuleType(VALKEY_MODULE)
    module.ValkeyStore = lambda **kwargs: backing_store
    monkeypatch.setitem(sys.modules, VALKEY_MODULE, module)
    monkeypatch.setitem(sys.modules, "glide_shared", None)
    monkeypatch.setitem(sys.modules, GLIDE_CONFIG_MODULE, None)
    _select_valkey(monkeypatch)
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            is_oauth21_enabled=lambda: True,
            is_configured=lambda: True,
            is_public_client=lambda: False,
            is_external_oauth21_provider=lambda: False,
            client_id="client-id",
            client_secret=secret,
            get_oauth_base_url=lambda: "https://workspace-mcp.example.test",
            redirect_path="/oauth2callback",
        ),
    )
    captured.clear()
    server_module.configure_server_for_http()
    return captured["client_storage"]


@pytest.mark.asyncio
async def test_client_secret_rotation_degrades_to_a_miss(monkeypatch, captured):
    """A record written under the previous secret must read as a cache miss.

    ``FernetEncryptionWrapper`` defaults to ``raise_on_decryption_error=True``,
    which turns every stale read after a ``GOOGLE_OAUTH_CLIENT_SECRET`` rotation
    into a ``DecryptionError``. FastMCP's own default store passes ``False`` so
    rotation just forces re-registration; these stores must agree with it.
    """
    from key_value.aio.stores.memory import MemoryStore

    backing = MemoryStore()

    before = _configure_with_secret(
        monkeypatch, captured, "secret-number-one-with-entropy", backing
    )
    await before.put("client-a", {"client_id": "abc"}, collection="clients")
    assert await before.get("client-a", collection="clients") == {"client_id": "abc"}

    after = _configure_with_secret(
        monkeypatch, captured, "secret-number-two-with-entropy", backing
    )
    assert await after.get("client-a", collection="clients") is None, (
        "a stale record must degrade to a miss, not raise DecryptionError"
    )


@pytest.mark.asyncio
async def test_disk_client_secret_rotation_degrades_to_a_miss(
    monkeypatch, captured, tmp_path
):
    """Same contract for the disk-backed branch."""
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )

    def _configure(secret):
        monkeypatch.setattr(
            "auth.oauth_config.get_oauth_config",
            lambda: SimpleNamespace(
                is_oauth21_enabled=lambda: True,
                is_configured=lambda: True,
                is_public_client=lambda: False,
                is_external_oauth21_provider=lambda: False,
                client_id="client-id",
                client_secret=secret,
                get_oauth_base_url=lambda: "https://workspace-mcp.example.test",
                redirect_path="/oauth2callback",
            ),
        )
        captured.clear()
        server_module.configure_server_for_http()
        return captured["client_storage"]

    before = _configure("disk-secret-number-one-with-entropy")
    await before.put("client-a", {"client_id": "abc"}, collection="clients")
    assert await before.get("client-a", collection="clients") == {"client_id": "abc"}

    after = _configure("disk-secret-number-two-with-entropy")
    assert await after.get("client-a", collection="clients") is None


_PLAINTEXT_PURGE_HINT = "may hold OAuth client records written unencrypted"


def test_persistent_backends_warn_about_unremediated_plaintext(
    monkeypatch, captured, caplog, tmp_path
):
    """The fix stops new plaintext; it does not purge old.

    An unencrypted record is returned as-is rather than rejected, so a store
    that survived a pre-fix build keeps serving it. Only persistent backends can
    be holding any, so only those are warned.
    """
    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    valkey_messages = "\n".join(record.getMessage() for record in caplog.records)
    assert _PLAINTEXT_PURGE_HINT in valkey_messages

    caplog.clear()
    captured.clear()
    monkeypatch.delenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_HOST", raising=False)
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "disk")
    monkeypatch.setenv(
        "WORKSPACE_MCP_OAUTH_PROXY_DISK_DIRECTORY", str(tmp_path / "proxy")
    )

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert _PLAINTEXT_PURGE_HINT in "\n".join(
        record.getMessage() for record in caplog.records
    )


@pytest.mark.parametrize("backend", ["memory", ""])
def test_non_persistent_backends_do_not_warn_about_plaintext(
    monkeypatch, captured, caplog, backend
):
    """Nothing survives a restart in these, so the warning would be noise.

    ``""`` is the unset case, where FastMCP builds its own default store.
    """
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", backend)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert _PLAINTEXT_PURGE_HINT not in messages


def test_no_plaintext_warning_when_the_valkey_store_was_not_built(
    monkeypatch, captured, caplog
):
    """Valkey requested but unavailable: there is no persistent store to purge."""
    monkeypatch.setitem(sys.modules, VALKEY_MODULE, None)
    _select_valkey(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert captured["client_storage"] is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert _PLAINTEXT_PURGE_HINT not in messages


def test_external_provider_mode_says_the_store_is_not_used(
    monkeypatch, captured, caplog
):
    """Pre-existing: ExternalOAuthProvider gets no client_storage.

    Not fixed here, but the "Using ValkeyStore" line must not be left as the
    operator's last word on which store is in effect.
    """

    class FakeExternalOAuthProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    _install_fake_valkey(monkeypatch)
    _select_valkey(monkeypatch)
    monkeypatch.setattr(
        "auth.external_oauth_provider.ExternalOAuthProvider",
        FakeExternalOAuthProvider,
    )
    monkeypatch.setattr(
        "auth.oauth_config.get_oauth_config",
        lambda: SimpleNamespace(
            is_oauth21_enabled=lambda: True,
            is_configured=lambda: True,
            is_public_client=lambda: False,
            is_external_oauth21_provider=lambda: True,
            client_id="client-id",
            client_secret="client-secret-with-plenty-of-entropy",
            get_oauth_base_url=lambda: "https://workspace-mcp.example.test",
            redirect_path="/oauth2callback",
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert "client_storage" not in captured, (
        "pre-existing behaviour, pinned here so a later fix is a deliberate change"
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "is NOT used" in messages
