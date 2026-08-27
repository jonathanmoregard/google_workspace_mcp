"""The OAuth-proxy Valkey client store must never fail open.

``configure_server_for_http`` builds the store that FastMCP's OAuth proxy uses
to persist dynamically registered client credentials. That store is supposed to
be Fernet-encrypted at rest. The Valkey branch used to construct the raw
``ValkeyStore`` first and wrap it only later, with the ``ImportError`` and
``ValueError`` handlers sitting *between* the two — so any failure in the
encryption setup left the unencrypted store bound while the log claimed a
fallback had happened.

These tests pin the three distinct failure causes apart:

* missing Valkey dependency -> warn (with the install hint) and leave
  ``client_storage`` unset, exactly as the disk-backed branch does;
* malformed Valkey configuration -> abort startup naming the offending
  environment variable, restoring ``parse_bool_env``'s loud-failure contract;
* rejected storage-encryption key -> abort startup, as the disk branch already
  does by not catching ``ValueError`` at all.

In none of them may an unencrypted store reach ``GoogleProvider``.
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


class FakeValkeyStoreWithGlideConfig(FakeValkeyStore):
    """Variant exposing the private Glide config the server tunes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._client_config = SimpleNamespace(
            use_tls=False,
            request_timeout=None,
            advanced_config=None,
        )


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
    _install_fake_valkey(monkeypatch, store_cls=FakeValkeyStoreWithGlideConfig)
    _select_valkey(monkeypatch, host="valkey.internal.example")
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "true")

    server_module.configure_server_for_http()

    store = captured["client_storage"].key_value
    assert store._client_config.use_tls is True
    assert store._client_config.request_timeout == 5000
    assert store._client_config.advanced_config.kwargs == {"connection_timeout": 10000}


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


def test_import_failure_after_store_construction_does_not_leave_plaintext_store(
    monkeypatch, captured, caplog
):
    """The Glide timeout import used to run *after* ``ValkeyStore(...)``.

    An ``ImportError`` there was caught by the dependency handler, which logged
    "dependencies are not installed" and left the already-constructed,
    unencrypted store bound. Both Valkey imports now happen before anything is
    built, so this can only end with no store at all.
    """
    _install_fake_valkey(
        monkeypatch, store_cls=FakeValkeyStoreWithGlideConfig, with_glide=False
    )
    # A non-loopback host makes the server apply default Glide timeouts, which
    # is what used to pull in ``glide_shared.config`` post-construction.
    _select_valkey(monkeypatch, host="valkey.internal.example")
    monkeypatch.setitem(sys.modules, "glide_shared", None)
    monkeypatch.setitem(sys.modules, GLIDE_CONFIG_MODULE, None)

    with caplog.at_level(logging.DEBUG, logger=server_module.logger.name):
        server_module.configure_server_for_http()

    assert captured["client_storage"] is None, (
        "a missing Valkey dependency must leave client_storage unset, "
        "never a raw unencrypted store"
    )
    assert FakeValkeyStore.instances == [], (
        "the store must not be constructed before its dependencies import"
    )


def test_missing_valkey_dependency_falls_back_without_a_store(
    monkeypatch, captured, caplog
):
    """Matches the disk branch: warn with the install hint, leave storage unset."""
    monkeypatch.delitem(sys.modules, VALKEY_MODULE, raising=False)
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
