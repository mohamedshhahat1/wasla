"""Configuration tests."""

from __future__ import annotations

import secrets

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


# ------------------------------------------------------------ signing algorithm


@pytest.mark.parametrize("algorithm", ["none", "None", "RS256", "ES256", "PS256", "HS255", ""])
def test_an_algorithm_outside_the_hmac_family_is_refused(algorithm):
    """Two failures, both configuration-only and both silent without this.

    `none` would be listed in the `algorithms=` allowlist that is otherwise the
    whole defence against algorithm confusion. And an asymmetric family would
    have the application verify with `jwt_secret` as a public key - the classic
    confusion, where anyone who learns the "public" key can sign. There is no
    key-pair configuration here, so an asymmetric algorithm cannot be set up
    correctly, only incorrectly.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", jwt_algorithm=algorithm)


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
def test_the_hmac_family_is_accepted(algorithm):
    """The control. A guard that refused everything would pass the test above."""
    settings = Settings(_env_file=None, environment="test", jwt_algorithm=algorithm)

    assert settings.jwt_algorithm == algorithm


def test_the_algorithm_is_normalised_to_upper_case():
    """`hs256` in an environment file is a typo, not a different algorithm."""
    settings = Settings(_env_file=None, environment="test", jwt_algorithm="hs512")

    assert settings.jwt_algorithm == "HS512"


# --- Email configuration (ADR-042) ---------------------------------------
#
# Email fails closed where it is *required*: a deployment that turned it on
# and half-configured it refuses to boot rather than queueing rows that render
# a broken link, send from nobody, or record no bounces.


def _email_production(**overrides):
    """A production configuration with email on, and one thing at a time wrong."""
    fields = {
        "_env_file": None,
        "environment": "production",
        "jwt_secret": VALID_SECRET,
        "docs_enabled": False,
        "meta_app_secret": "an-app-secret",
        "email_enabled": True,
        "email_provider": "resend",
        "email_from": "no-reply@example.com",
        "app_public_url": "https://app.example.com",
        "resend_webhook_secret": "whsec_abc",
    }
    fields.update(overrides)
    return Settings(**fields)


def test_email_off_needs_no_email_configuration():
    """A deployment that sends nothing must not be forced to configure sending."""
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret=VALID_SECRET,
        docs_enabled=False,
        meta_app_secret="an-app-secret",
        email_enabled=False,
    )

    assert not settings.email_enabled


def test_production_accepts_a_complete_email_configuration():
    settings = _email_production()

    assert settings.email_enabled
    assert settings.email_provider == "resend"


def test_email_on_requires_a_sender():
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        _email_production(email_from=None)


def test_email_on_requires_a_sender_that_is_an_address():
    """A sender the provider rejects fails *every* row, permanently."""
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        _email_production(email_from="not-an-address")


def test_a_display_name_in_the_sender_is_refused():
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        _email_production(email_from="Wasla <no-reply@example.com>")


def test_email_on_requires_a_public_url():
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        _email_production(app_public_url=None)


def test_production_refuses_a_plaintext_public_url():
    """Reset and invitation tokens travel in these links."""
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        _email_production(app_public_url="http://app.example.com")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com",
    ],
)
def test_a_public_url_on_a_dangerous_scheme_is_refused(url):
    """Whatever this holds is prefixed onto every link a recipient clicks."""
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        _email_production(app_public_url=url)


def test_a_public_url_without_a_host_is_refused():
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        _email_production(app_public_url="https://")


def test_a_public_url_carrying_a_query_is_refused():
    """Templates append their own path and query; a base with one is broken."""
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        _email_production(app_public_url="https://app.example.com/?next=/x")


def test_production_refuses_the_fake_provider():
    """It delivers nothing and says it succeeded."""
    with pytest.raises(ValidationError, match="EMAIL_PROVIDER"):
        _email_production(email_provider="fake")


def test_production_requires_a_webhook_secret():
    """Without it no bounce or complaint is ever recorded, and the platform
    keeps writing to dead mailboxes until the sending domain is what fails."""
    with pytest.raises(ValidationError, match="RESEND_WEBHOOK_SECRET"):
        _email_production(resend_webhook_secret=None)


def test_a_non_production_environment_may_use_the_fake_over_plain_http():
    """Local development sends nothing and needs no certificate to do it."""
    settings = Settings(
        _env_file=None,
        environment="local",
        jwt_secret=VALID_SECRET,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="http://localhost:3000",
    )

    assert settings.email_provider == "fake"


def test_a_dangerous_public_url_is_refused_outside_production_too():
    """The scheme allowlist is not a production-only courtesy."""
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        Settings(
            _env_file=None,
            environment="local",
            jwt_secret=VALID_SECRET,
            email_enabled=True,
            email_provider="fake",
            email_from="no-reply@example.com",
            app_public_url="javascript:alert(1)",
        )


# ----------------------------------------------------------- payment provider

PAYMOB = {
    "billing_provider": "paymob",
    "paymob_secret_key": "sk_test_notreal000000",
    "paymob_public_key": "pk_test_notreal000000",
    "paymob_hmac_secret": "a-test-hmac-secret",
    "paymob_integration_ids": [4097558],
    "app_public_url": "https://app.example.com",
}


def _paymob(**overrides):
    """A staging deployment configured to take payments.

    Staging rather than test, because the test environment is exempt from the
    fail-closed rules on purpose - and it is precisely those rules being
    exercised here.
    """
    values = {
        "_env_file": None,
        "environment": "staging",
        "jwt_secret": secrets.token_urlsafe(32),
        **PAYMOB,
    }
    values.update(overrides)
    return Settings(**values)


def test_a_deployment_taking_payments_accepts_a_complete_configuration():
    """The other half of every refusal below: this is what right looks like."""
    settings = _paymob()

    assert settings.billing_provider == "paymob"
    assert settings.paymob_integration_ids == [4097558]


@pytest.mark.parametrize(
    "missing",
    [
        "paymob_secret_key",
        "paymob_public_key",
        "paymob_hmac_secret",
        "app_public_url",
    ],
)
def test_a_half_configured_payment_provider_refuses_to_boot(missing):
    """Fail closed in every environment, not only production.

    A staging deployment taking real payments with no HMAC secret answers 503
    to every callback, so transactions complete at Paymob and are never
    recorded here. That is the worst failure this subsystem has: the customer
    is charged and gets nothing.
    """
    with pytest.raises(ValidationError):
        _paymob(**{missing: None})


def test_no_integration_id_is_refused():
    """An intention with no payment method is refused by Paymob itself.

    Discovered at startup rather than at the first customer.
    """
    with pytest.raises(ValidationError):
        _paymob(paymob_integration_ids=[])


def test_a_repeated_integration_id_is_refused():
    """The list is sent as the payment methods to offer on the page.

    A repeated id offers the same method twice, which is a checkout that looks
    broken to every customer who reaches it.
    """
    with pytest.raises(ValidationError):
        _paymob(paymob_integration_ids=[4097558, 4097558])


@pytest.mark.parametrize("value", [[0], [-1], [4097558, 0]])
def test_an_integration_id_that_cannot_be_real_is_refused(value):
    """Refused rather than dropped: a payment method that silently stops being
    offered is a much harder thing to notice than a deployment that will not
    start."""
    with pytest.raises(ValidationError):
        _paymob(paymob_integration_ids=value)


def test_mismatched_key_modes_are_refused():
    """The dangerous case, because both halves look valid on their own.

    A live secret key with a test public key creates a *real* intention and
    sends the customer to a *test* payment page. Nothing is ever collected,
    every callback is for money that does not exist, and no other check in the
    system can see it.
    """
    with pytest.raises(ValidationError) as caught:
        _paymob(paymob_secret_key="sk_live_realone00000")

    assert "different Paymob modes" in str(caught.value)


def test_matching_live_keys_are_accepted_in_production():
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret=secrets.token_urlsafe(32),
        docs_enabled=False,
        meta_app_secret="meta-secret",
        **{
            **PAYMOB,
            "paymob_secret_key": "sk_live_realone00000",
            "paymob_public_key": "pk_live_realone00000",
        },
    )

    assert settings.billing_provider == "paymob"


def test_test_keys_are_refused_in_production():
    """A deployment that believes it is live and is not.

    Every payment would be pretend and every customer would get the product
    free - and the dashboard would look busy while no money arrived.
    """
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=secrets.token_urlsafe(32),
            docs_enabled=False,
            meta_app_secret="meta-secret",
            **PAYMOB,
        )

    # Asserted on the message, because every other production rule is
    # satisfied above - so this test fails if the mode check is the thing that
    # stops working rather than passing on somebody else's refusal.
    assert "test keys" in str(caught.value)


def test_a_regional_key_prefix_is_not_treated_as_a_mismatch():
    """Some regions prefix the whole key, and the mode is still readable.

    Refusing to boot over a shape this integration has not seen would be worse
    than the mistake being guarded against, so the mode is searched for rather
    than required at position zero.
    """
    settings = _paymob(
        paymob_secret_key="egy_sk_test_notreal000",
        paymob_public_key="egy_pk_test_notreal000",
    )

    assert settings.paymob_secret_key.startswith("egy_")


def test_a_callback_url_must_not_be_plain_http_in_production():
    """Payment callbacks travel to it, and so does a signed transaction."""
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret=secrets.token_urlsafe(32),
            docs_enabled=False,
            meta_app_secret="meta-secret",
            **{
                **PAYMOB,
                "paymob_secret_key": "sk_live_realone00000",
                "paymob_public_key": "pk_live_realone00000",
                "app_public_url": "http://app.example.com",
            },
        )

    assert "https" in str(caught.value)


def test_the_manual_provider_needs_no_credentials():
    """The default, and the state every local run and every test is in.

    A deployment that configures nothing bills exactly as it did before hosted
    checkout existed.
    """
    settings = Settings(
        _env_file=None,
        environment="staging",
        jwt_secret=secrets.token_urlsafe(32),
    )

    assert settings.billing_provider == "manual"
    assert settings.paymob_secret_key is None


# --------------------------------------------------------------- Google sign-in

GOOGLE = {
    "google_enabled": True,
    "google_client_id": "1234567890-testclient.apps.googleusercontent.com",
    "google_client_secret": "a-test-client-secret",
    "google_redirect_uri": "https://app.example.com/auth/google/callback",
}


def _google(**overrides):
    """A staging deployment with Google sign-in switched on.

    Staging rather than test for `_paymob`'s reason: the test environment is
    exempt from the fail-closed rules on purpose, and those rules are what
    these exercise.
    """
    values = {
        "_env_file": None,
        "environment": "staging",
        "jwt_secret": secrets.token_urlsafe(32),
        **GOOGLE,
    }
    values.update(overrides)
    return Settings(**values)


def test_google_off_needs_no_credentials():
    """A feature nobody enabled must not stop a deployment starting.

    This is the property the production Compose file relies on: every
    integration setting is optional at interpolation time, and the validator is
    what refuses a half-configured one (ADR-062).
    """
    settings = Settings(
        _env_file=None,
        environment="staging",
        jwt_secret=secrets.token_urlsafe(32),
    )

    assert settings.google_enabled is False
    assert settings.google_client_id is None
    assert settings.google_client_secret is None


def test_google_on_accepts_a_complete_configuration():
    settings = _google()

    assert settings.google_enabled is True
    assert settings.google_redirect_uri.endswith("/auth/google/callback")


@pytest.mark.parametrize(
    "missing",
    ["google_client_id", "google_client_secret", "google_redirect_uri"],
)
def test_google_on_requires_every_credential(missing):
    """Half-configured Google sign-in is a button that always fails.

    And the only error message a user ever sees lives on Google's domain, which
    is why this refuses at startup rather than at the first login.
    """
    with pytest.raises(ValidationError) as caught:
        _google(**{missing: None})

    assert missing.upper() in str(caught.value)


def test_google_refuses_a_client_id_that_is_not_one():
    """Catches the two paste errors that actually happen.

    The secret in the id field, or the bare project number. Both produce a
    refusal from Google that looks like Google's fault.
    """
    with pytest.raises(ValidationError) as caught:
        _google(google_client_id="1234567890")

    assert "GOOGLE_CLIENT_ID" in str(caught.value)


def test_google_refuses_the_client_id_pasted_as_the_secret():
    with pytest.raises(ValidationError) as caught:
        _google(google_client_secret=GOOGLE["google_client_id"])

    assert "GOOGLE_CLIENT_SECRET" in str(caught.value)


def test_google_requires_https_in_production():
    """A single-use authorization code travels back to this address."""
    with pytest.raises(ValidationError) as caught:
        _google(
            environment="production",
            debug=False,
            docs_enabled=False,
            meta_app_secret="an-app-secret",
            google_redirect_uri="http://app.example.com/auth/google/callback",
        )

    assert "https" in str(caught.value)


@pytest.mark.parametrize(
    "redirect",
    [
        "javascript:alert(1)",
        "//app.example.com/auth/google/callback",
        "https://app.example.com/callback#fragment",
    ],
)
def test_google_refuses_a_redirect_that_is_not_a_plain_url(redirect):
    """Scheme allowlist and no fragment, checked before Google ever sees it."""
    with pytest.raises(ValidationError):
        _google(google_redirect_uri=redirect)


# ------------------------------------------------------------------- dunning


def test_the_suspension_threshold_must_be_later_than_the_past_due_one():
    """Otherwise a workspace is cut off in the sweep that first chases it.

    Checked in every environment, `test` included, because this is an ordering
    rather than a credential - and an ordering that is wrong is wrong
    everywhere (ADR-061).
    """
    for past_due, suspend in ((7, 7), (10, 3), (2, 2)):
        with pytest.raises(ValidationError) as caught:
            Settings(
                _env_file=None,
                environment="test",
                billing_past_due_days=past_due,
                billing_suspend_after_days=suspend,
            )
        assert "BILLING_SUSPEND_AFTER_DAYS" in str(caught.value)


def test_the_dunning_defaults_are_a_week_then_a_month():
    """The numbers a deployment inherits without configuring anything."""
    settings = Settings(_env_file=None, environment="test")

    assert settings.billing_past_due_days == 7
    assert settings.billing_suspend_after_days == 30


def test_the_dunning_thresholds_are_configurable():
    settings = Settings(
        _env_file=None,
        environment="test",
        billing_past_due_days=3,
        billing_suspend_after_days=14,
    )

    assert settings.billing_past_due_days == 3
    assert settings.billing_suspend_after_days == 14
