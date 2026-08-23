"""Configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Run every test against a clean environment.

    ``_env_file=None`` only disables ``.env`` loading; real environment
    variables still take precedence. CI exports ENVIRONMENT and JWT_SECRET, so
    without this the assertions below would describe the runner instead of the
    code under test.
    """
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_local_and_safe():
    settings = Settings(_env_file=None)

    assert settings.app_name == "Wasla"
    assert settings.environment == "local"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.cors_origins == []
    assert not settings.is_production


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.wasla.test, https://admin.wasla.test")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.cors_origins == ["https://app.wasla.test", "https://admin.wasla.test"]


def test_cors_origins_accepts_json_array():
    settings = Settings(_env_file=None, cors_origins='["https://app.wasla.test"]')

    assert settings.cors_origins == ["https://app.wasla.test"]


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="chatty")


def test_production_rejects_placeholder_secret():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")


def test_production_rejects_debug_mode():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret="a" * 40, debug=True)


def test_production_accepts_hardened_configuration():
    settings = Settings(_env_file=None, environment="production", jwt_secret="a" * 40)

    assert settings.is_production
    assert not settings.debug


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_a_comma_separated_list_survives_the_environment(monkeypatch):
    """Read from the environment, not passed to the constructor, because those
    are different code paths and only one of them is how a container starts.

    pydantic-settings JSON-decodes a list field straight from the environment
    before any validator runs, so a plain comma-separated value raises at
    start-up unless the field is annotated `NoDecode`. Every list setting here
    is read that way in production, and a constructor-only test cannot see it -
    which is exactly how this shipped and was caught by a container refusing to
    boot rather than by the suite.
    """
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", "first-key,second-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.wasla.test,https://admin.wasla.test")

    settings = Settings(_env_file=None)

    assert settings.credential_encryption_keys == ["first-key", "second-key"]
    assert settings.cors_origins == ["https://app.wasla.test", "https://admin.wasla.test"]


def test_a_json_array_also_survives_the_environment(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", '["first-key"]')

    assert Settings(_env_file=None).credential_encryption_keys == ["first-key"]


def test_an_unset_list_is_empty_rather_than_an_error(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)

    assert Settings(_env_file=None).credential_encryption_keys == []
