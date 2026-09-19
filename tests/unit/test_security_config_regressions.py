"""Internet-facing settings refuse unsafe deployment values (SEC-06, S10)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _internet_settings(environment: str, **changes: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": environment,
        "jwt_secret": "synthetic-signing-key-with-more-than-32-characters",
        "meta_app_secret": "synthetic-meta-app-secret",
        "meta_verify_token": "synthetic-meta-verify-token",
        "cors_origins": ["https://app.example.com"],
        "trusted_proxy_ips": ["10.89.0.10"],
        "app_public_url": "https://app.example.com",
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_internet_facing_defaults_disable_interactive_docs(environment: str) -> None:
    settings = _internet_settings(environment)
    assert settings.docs_enabled is False
    assert settings.cors_origins == ["https://app.example.com"]


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    ("change", "diagnostic"),
    [
        ({"docs_enabled": True}, "DOCS_ENABLED"),
        ({"cors_origins": ["*"]}, "CORS_ORIGINS"),
        ({"cors_origins": ["null"]}, "CORS_ORIGINS"),
        ({"cors_origins": ["http://app.example.com"]}, "CORS_ORIGINS"),
        ({"cors_origins": ["https://*.example.com"]}, "CORS_ORIGINS"),
        ({"trusted_proxy_ips": ["0.0.0.0/0"]}, "TRUSTED_PROXY_IPS"),
        ({"trusted_proxy_ips": ["::/0"]}, "TRUSTED_PROXY_IPS"),
        ({"trusted_proxy_ips": ["8.8.8.0/24"]}, "TRUSTED_PROXY_IPS"),
        ({"rate_limit_enabled": False}, "RATE_LIMIT_ENABLED"),
        ({"app_public_url": "https://app.example.com@evil.example"}, "APP_PUBLIC_URL"),
        ({"app_public_url": "http://app.example.com"}, "APP_PUBLIC_URL"),
        ({"app_public_url": "https://app.example.com/?next=evil"}, "APP_PUBLIC_URL"),
        ({"app_public_url": "https://app.example.com/#fragment"}, "APP_PUBLIC_URL"),
    ],
)
def test_unsafe_internet_setting_is_rejected(
    environment: str, change: dict[str, Any], diagnostic: str
) -> None:
    with pytest.raises(ValidationError, match=diagnostic):
        _internet_settings(environment, **change)
