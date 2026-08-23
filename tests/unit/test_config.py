"""Configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

# Long enough to satisfy the signing-key rule, which now applies in every
# environment except `test`. Supplied by the helper below so that the tests
# about *other* settings do not each have to restate it - and so the ones
# that are genuinely about the secret stand out by not using the helper.
VALID_SECRET = "a" * 40


def _settings(**overrides) -> Settings:
    overrides.setdefault("jwt_secret", VALID_SECRET)
    return Settings(_env_file=None, **overrides)


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


def test_defaults_are_local():
    settings = _settings()

    assert settings.app_name == "Wasla"
    assert settings.environment == "local"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.cors_origins == []
    assert not settings.is_production
    # Forwarding headers are believed only from a listed peer, and the list is
    # empty until an operator names one. Anything else would mean a caller can
    # choose the address the authentication limiter counts them under.
    assert settings.trusted_proxy_ips == []


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_the_placeholder_secret_is_refused_outside_the_test_suite(environment):
    """The rule that used to apply only to `production`.

    Two ways the old shape let a real deployment through, and both are the sort
    that never announce themselves: `staging` is internet-reachable here - Meta
    has to deliver webhooks to it - and skipped the check entirely, and
    `environment` itself defaults to `local`, so a container started without
    ENVIRONMENT set skipped it too. The guard on the signing key was opt-in
    through a field defaulting to the unguarded side.
    """
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, environment=environment)


def test_a_short_secret_is_refused_even_when_it_is_not_the_placeholder():
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, environment="staging", jwt_secret="short")


def test_the_test_environment_is_the_only_one_exempt():
    """Exempt so the suite can build settings without ceremony, and it is the
    one environment that never listens on a network."""
    settings = Settings(_env_file=None, environment="test")

    assert settings.jwt_secret == "change-me"


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.wasla.test, https://admin.wasla.test")

    settings = _settings()

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.cors_origins == ["https://app.wasla.test", "https://admin.wasla.test"]


def test_cors_origins_accepts_json_array():
    settings = _settings(cors_origins='["https://app.wasla.test"]')

    assert settings.cors_origins == ["https://app.wasla.test"]


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        _settings(log_level="chatty")


def test_production_rejects_placeholder_secret():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production")


def test_production_rejects_debug_mode():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="production", jwt_secret="a" * 40, debug=True)


def test_production_rejects_interactive_docs():
    """The reference publishes every route and schema, platform administration
    included. It is a map, and it was previously on by default."""
    with pytest.raises(ValidationError, match="DOCS_ENABLED"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=VALID_SECRET,
            meta_app_secret="s",
            docs_enabled=True,
        )


def test_production_requires_a_meta_app_secret():
    """Without it the webhook cannot verify a signature and answers 503 to every
    delivery - a silent integration outage that reads as Meta's fault."""
    with pytest.raises(ValidationError, match="META_APP_SECRET"):
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=VALID_SECRET,
            docs_enabled=False,
        )


def test_production_accepts_hardened_configuration():
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret=VALID_SECRET,
        docs_enabled=False,
        meta_app_secret="an-app-secret",
    )

    assert settings.is_production
    assert not settings.debug
    assert not settings.docs_enabled


def test_get_settings_is_cached(monkeypatch):
    """About the cache, not about the configuration.

    `get_settings()` reads the real environment, which the autouse fixture above
    strips - so without a usable environment here the settings would be refused
    for a missing signing key and the caching question would never be reached.
    `test` is the environment exempt from that rule, which keeps this test about
    one thing.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

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

    settings = _settings()

    assert settings.credential_encryption_keys == ["first-key", "second-key"]
    assert settings.cors_origins == ["https://app.wasla.test", "https://admin.wasla.test"]


def test_a_json_array_also_survives_the_environment(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", '["first-key"]')

    assert _settings().credential_encryption_keys == ["first-key"]


def test_an_unset_list_is_empty_rather_than_an_error(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEYS", raising=False)

    assert _settings().credential_encryption_keys == []
