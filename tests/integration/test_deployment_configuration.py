"""The deployment topology has to agree with what the application reads.

This file exists because it did not. Google sign-in, transactional email and
Paymob payments were all implemented, tested and documented, and none of them
could be switched on by a deployment brought up from `docker-compose.prod.yml`:
the file enumerated its environment explicitly - which is the right posture for
production - and the enumeration went stale over five phases while `Settings`
grew. The result was features that existed in source and were unreachable in the
product, with nothing anywhere to say so (ADR-062).

**The guard derives its expectations from `Settings`, not from a second list.**
`FEATURE_SETTINGS` below maps each optional integration to the process that
reads it, and the tests cross-check that mapping against `Settings.model_fields`
in both directions - so a field added to a mapped feature prefix and never wired
into Compose fails here, and a mapping entry naming a field that no longer
exists fails here too. Three hand-maintained copies of the same list would be
the bug again with extra steps.

`docker-compose.yml` is deliberately *not* checked the same way. It forwards a
developer's whole `.env` through `env_file`, so it cannot drift; the assertion
below is that it keeps doing so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"


# Which process reads each optional integration, keyed by the settings prefix
# that identifies it. This is the one place the mapping lives; the tests below
# derive the field names from `Settings` rather than repeating them.
#
# "api" and "worker" are the service names in `docker-compose.prod.yml`.
FEATURE_SETTINGS: dict[str, tuple[str, ...]] = {
    # The API verifies Resend's delivery webhook and writes outbox rows; the
    # worker is what actually sends. Both, therefore - and the one field that
    # is genuinely worker-only is called out in EXPECTED_ABSENT below.
    "email_": ("api", "worker"),
    "resend_": ("api", "worker"),
    # Emailed links and the Paymob callback URL are both built from it.
    "app_public_url": ("api", "worker"),
    # OIDC lives entirely in the API. Keeping the client secret out of the
    # worker is the point, not an oversight.
    "google_": ("api",),
    # The API creates intentions and verifies callbacks; the worker collects
    # renewals from saved cards.
    "billing_provider": ("api", "worker"),
    "paymob_": ("api", "worker"),
    # Dunning thresholds (ADR-061). Acted on by the worker, and present on the
    # API so the ordering rule is validated by whichever process starts first.
    "billing_past_due_days": ("api", "worker"),
    "billing_suspend_after_days": ("api", "worker"),
}

# Fields a mapped prefix picks up that a given service deliberately does not
# carry, with the reason. Anything here is a decision; anything missing without
# being here is drift.
EXPECTED_ABSENT: dict[tuple[str, str], str] = {
    ("api", "RESEND_API_KEY"): (
        "the API never sends - `build_email_provider` validates this in the "
        "process that uses it, so the credential lives on the worker alone"
    ),
    ("worker", "EMAIL_VERIFICATION_TTL_SECONDS"): (
        "verification challenges are issued on the request path"
    ),
    ("worker", "EMAIL_VERIFICATION_MAX_ATTEMPTS"): (
        "verification challenges are issued on the request path"
    ),
    ("api", "EMAIL_MAX_ATTEMPTS"): "delivery retries belong to the email worker",
    ("api", "EMAIL_WORKER_POLL_SECONDS"): "the poll interval belongs to the email worker",
}


def _service_environment(text: str, service: str) -> set[str]:
    """The environment keys one Compose service declares.

    Parsed rather than loaded through a YAML library on purpose: the file is
    full of `${VAR:-}` interpolation, and what this needs to know is which keys
    are *written down*, which is a property of the text.
    """
    block = re.search(
        rf"^  {service}:\n(.*?)(?=^  \w|\Z)",
        text,
        re.M | re.S,
    )
    assert block is not None, f"no service named {service} in the compose file"
    environment = re.search(r"^    environment:\n(.*?)(?=^    \w|\Z)", block.group(1), re.M | re.S)
    assert environment is not None, f"service {service} declares no environment"
    return set(re.findall(r"^      ([A-Z][A-Z0-9_]*):", environment.group(1), re.M))


def _fields_for(prefix: str) -> set[str]:
    """The `Settings` fields a mapping entry covers, as environment names."""
    return {
        name.upper() for name in Settings.model_fields if name == prefix or name.startswith(prefix)
    }


def _expected(service: str) -> set[str]:
    wanted: set[str] = set()
    for prefix, services in FEATURE_SETTINGS.items():
        if service in services:
            wanted |= _fields_for(prefix)
    return {name for name in wanted if (service, name) not in EXPECTED_ABSENT}


# ------------------------------------------------- the mapping is not stale


@pytest.mark.parametrize("prefix", sorted(FEATURE_SETTINGS))
def test_every_mapped_prefix_matches_a_real_setting(prefix: str) -> None:
    """A mapping entry naming a field that no longer exists proves nothing.

    Without this the guard could pass while silently checking an empty set -
    which is exactly how a list-based test rots into a test of itself.
    """
    assert _fields_for(prefix), f"FEATURE_SETTINGS entry {prefix!r} matches no Settings field"


@pytest.mark.parametrize("key", sorted(EXPECTED_ABSENT))
def test_every_documented_absence_names_a_real_setting(key: tuple[str, str]) -> None:
    service, name = key
    assert name.lower() in Settings.model_fields, f"{name} is no longer a setting"
    assert service in {"api", "worker"}


# -------------------------------------------- production Compose is complete


@pytest.mark.parametrize("service", ["api", "worker"])
def test_production_compose_carries_every_setting_its_process_reads(service: str) -> None:
    """The drift guard itself.

    If somebody adds a setting under a mapped feature and forgets to wire it
    into the production topology, this fails - which is the whole point, because
    the alternative is a feature that ships and cannot be turned on.
    """
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    declared = _service_environment(text, service)
    missing = sorted(_expected(service) - declared)

    assert not missing, (
        f"docker-compose.prod.yml service {service!r} does not pass: "
        f"{', '.join(missing)}. Add them as ${{VAR:-}} so the feature stays "
        "optional, or record a deliberate omission in EXPECTED_ABSENT."
    )


@pytest.mark.parametrize("service", ["api", "worker"])
def test_an_optional_integration_is_never_mandatory_at_interpolation(service: str) -> None:
    """`${VAR:?}` on a feature setting would stop the stack for a disabled one.

    The infrastructure a deployment cannot run without - the image, the
    database, the signing key - is deliberately `${VAR:?}` elsewhere in the
    file. A feature nobody enabled is a different thing, and refusing to boot
    over an absent Google client secret would make an optional integration
    compulsory.
    """
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \w|\Z)", text, re.M | re.S)
    assert block is not None
    mandatory = {
        name
        for name, expression in re.findall(
            r"^      ([A-Z][A-Z0-9_]*): (\$\{[^}]*\})", block.group(1), re.M
        )
        if ":?" in expression
    }

    assert not (mandatory & _expected(service)), (
        f"{service}: feature settings must be optional at interpolation time: "
        f"{sorted(mandatory & _expected(service))}"
    )


def test_the_development_compose_forwards_everything_instead() -> None:
    """It cannot drift, because it does not enumerate.

    `env_file` with `required: false` hands a developer's whole `.env` to both
    containers, which is why the production file is the only one this suite
    checks key by key. If that ever changes, this file has to grow a second
    expectation - and this assertion is what says so.
    """
    text = DEV_COMPOSE.read_text(encoding="utf-8")

    assert text.count("env_file:") == 2, "both dev services should forward .env"
    assert "required: false" in text


# ----------------------------------------------- the example file documents it


@pytest.mark.parametrize("service", ["api", "worker"])
def test_env_example_documents_every_setting_a_deployment_must_provide(service: str) -> None:
    """An operator has to be able to discover the variable exists.

    `TRUSTED_PROXY_IPS` was absent from `.env.example` while being set to an
    unusable value in Compose, and the two failures compounded: nobody had a
    prompt to fix what nobody could see was wrong (ADR-060).
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))
    missing = sorted(_expected(service) - documented)

    assert not missing, f".env.example does not mention: {', '.join(missing)}"


def test_no_secret_value_is_written_into_the_shipped_configuration() -> None:
    """Every credential is interpolated, never literal.

    A narrow, mechanical check: the settings this file maps are secrets or
    secret-adjacent, and each must be a `${...}` reference rather than a value
    somebody pasted while debugging.
    """
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    secretish = (
        "PAYMOB_SECRET_KEY",
        "PAYMOB_HMAC_SECRET",
        "GOOGLE_CLIENT_SECRET",
        "RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
        "JWT_SECRET",
    )
    for name in secretish:
        for line in re.findall(rf"^\s*{name}:.*$", text, re.M):
            value = line.split(":", 1)[1].strip()
            assert value.startswith("${"), f"{name} is not interpolated: {line.strip()}"
