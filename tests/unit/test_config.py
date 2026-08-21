"""Configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


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
