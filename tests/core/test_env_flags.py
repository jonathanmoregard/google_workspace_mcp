"""One parser for the boolean mode flags, so the banner cannot lie.

``MCP_ENABLE_OAUTH21=1`` used to be three answers at once: ``auth/oauth_config.py``
compared the raw string to ``"true"`` and read it as OFF, ``auth/credential_store.py``
parsed it strictly and read it as ON, and the startup banner rendered it as ON.
The server therefore ran fully ``single_user`` — no protocol auth — while telling
the operator that OAuth 2.1 was enabled.

These tests pin the whole family to one answer per value, checked at every
reader rather than only at the parser.
"""

import os

import pytest

# Importing main loads .env and runs the startup module body; OAuth 2.1 mode
# changes tool schemas at decoration time. Pin the modes first, exactly as
# tests/test_main_permissions_tier.py does, so neither module's import can be
# steered by a developer's local .env whichever is collected first.
os.environ.setdefault("MCP_ENABLE_OAUTH21", "false")
os.environ.setdefault("WORKSPACE_MCP_STATELESS_MODE", "false")

import main  # noqa: E402
import auth.credential_store as credential_store  # noqa: E402
import core.server as core_server  # noqa: E402
from auth.oauth_config import OAuthConfig  # noqa: E402
from core.env_flags import parse_bool_env  # noqa: E402

#: Every value the flags accept, and the single answer it must produce.
FLAG_VALUES = [
    ("1", True),
    ("yes", True),
    ("on", True),
    ("true", True),
    ("TRUE", True),
    ("0", False),
    ("false", False),
    ("", False),
]

#: Vars that would make OAuthConfig() reject an otherwise valid combination.
_INTERFERING_VARS = (
    "MCP_ENABLE_OAUTH21",
    "TRUST_GATEWAY_IDENTITY",
    "EXTERNAL_OAUTH21_PROVIDER",
    "WORKSPACE_MCP_STATELESS_MODE",
    "GOOGLE_SERVICE_ACCOUNT_KEY_FILE",
    "GOOGLE_SERVICE_ACCOUNT_KEY_JSON",
)


@pytest.fixture
def clean_flags(monkeypatch):
    """Start from no mode flags set at all."""
    for var in _INTERFERING_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# --------------------------------------------------------------------------
# The parser itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", " true "]
)
def test_truthy_values(value):
    assert parse_bool_env(value) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", " ", None])
def test_falsy_values(value):
    assert parse_bool_env(value) is False


def test_an_unset_variable_is_false():
    assert "WORKSPACE_MCP_NOT_A_REAL_FLAG" not in os.environ
    assert parse_bool_env(os.getenv("WORKSPACE_MCP_NOT_A_REAL_FLAG")) is False


@pytest.mark.parametrize("value", ["treu", "ye", "maybe", "2", "enabled"])
def test_unrecognised_values_raise(value):
    with pytest.raises(ValueError, match="Invalid boolean env var"):
        parse_bool_env(value)


def test_there_is_exactly_one_parser():
    """The historical private names must be the shared function, not copies."""
    assert credential_store._parse_bool_env is parse_bool_env
    assert core_server._parse_bool_env is parse_bool_env


# --------------------------------------------------------------------------
# Every reader of the family agrees
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", FLAG_VALUES)
def test_every_reader_of_mcp_enable_oauth21_agrees(clean_flags, value, expected):
    clean_flags.setenv("MCP_ENABLE_OAUTH21", value)

    assert OAuthConfig().is_oauth21_enabled() is expected
    assert credential_store._parse_bool_env(os.getenv("MCP_ENABLE_OAUTH21")) is expected
    assert main._flag_field("MCP_ENABLE_OAUTH21")[2] == ("on" if expected else "off")


def test_mcp_enable_oauth21_unset_is_off_everywhere(clean_flags):
    assert OAuthConfig().is_oauth21_enabled() is False
    assert credential_store._parse_bool_env(os.getenv("MCP_ENABLE_OAUTH21")) is False
    assert main._flag_field("MCP_ENABLE_OAUTH21")[2] == "off"


@pytest.mark.parametrize("value,expected", FLAG_VALUES)
def test_every_reader_of_trust_gateway_identity_agrees(
    clean_flags, monkeypatch, value, expected
):
    # Prerequisites the mode validates once it is on; irrelevant when it is off.
    monkeypatch.setenv(
        "GATEWAY_IDENTITY_JWKS_URL", "https://gateway.example.com/.well-known/jwks.json"
    )
    monkeypatch.setenv("GATEWAY_IDENTITY_AUDIENCE", "workspace-mcp")
    clean_flags.setenv("TRUST_GATEWAY_IDENTITY", value)

    assert OAuthConfig().trust_gateway_identity is expected
    assert main._flag_field("TRUST_GATEWAY_IDENTITY")[2] == (
        "on" if expected else "off"
    )


@pytest.mark.parametrize("value,expected", FLAG_VALUES)
def test_every_reader_of_stateless_mode_agrees(clean_flags, value, expected):
    # Stateless mode is only valid alongside OAuth 2.1; set it the same way.
    clean_flags.setenv("MCP_ENABLE_OAUTH21", value)
    clean_flags.setenv("WORKSPACE_MCP_STATELESS_MODE", value)

    assert OAuthConfig().stateless_mode is expected
    assert main._flag_field("WORKSPACE_MCP_STATELESS_MODE")[2] == (
        "on" if expected else "off"
    )


@pytest.mark.parametrize("value,expected", FLAG_VALUES)
def test_external_oauth21_provider_agrees(clean_flags, value, expected):
    # The external provider mode requires OAuth 2.1; set it the same way.
    clean_flags.setenv("MCP_ENABLE_OAUTH21", value)
    clean_flags.setenv("EXTERNAL_OAUTH21_PROVIDER", value)

    assert OAuthConfig().is_external_oauth21_provider() is expected


# --------------------------------------------------------------------------
# Typos fail loudly instead of reading as "off"
# --------------------------------------------------------------------------


def test_a_typo_in_a_mode_flag_is_rejected_rather_than_silently_off(clean_flags):
    clean_flags.setenv("MCP_ENABLE_OAUTH21", "treu")

    with pytest.raises(ValueError, match="Invalid boolean env var"):
        OAuthConfig()


def test_the_banner_marks_an_unrecognised_value_instead_of_calling_it_off(clean_flags):
    clean_flags.setenv("MCP_ENABLE_OAUTH21", "treu")

    name, value, state = main._flag_field("MCP_ENABLE_OAUTH21")

    assert name == "MCP_ENABLE_OAUTH21"
    assert "treu" in value
    assert state == "warn"
