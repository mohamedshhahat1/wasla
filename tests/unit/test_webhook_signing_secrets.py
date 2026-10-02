"""Which secret verifies each Meta product's webhooks (OMNI-050)."""

from __future__ import annotations

import pytest

from app.core.config import Settings, webhook_signing_secret


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        cors_origins=[],
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("product", ["whatsapp", "instagram", "messenger"])
def test_every_product_falls_back_to_the_app_secret(product: str) -> None:
    assert webhook_signing_secret(_settings(meta_app_secret="app"), product) == "app"


def test_a_product_specific_secret_wins_for_its_product_only() -> None:
    settings = _settings(
        meta_app_secret="app",
        meta_instagram_app_secret="instagram-login",
        meta_messenger_app_secret="messenger",
    )

    assert webhook_signing_secret(settings, "instagram") == "instagram-login"
    assert webhook_signing_secret(settings, "messenger") == "messenger"
    assert webhook_signing_secret(settings, "whatsapp") == "app"


def test_nothing_configured_is_nothing() -> None:
    assert webhook_signing_secret(_settings(), "instagram") is None
