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
    # worker is what actually sends. Both, therefore - and the fields only one
    # of them may hold are called out in EXPECTED_ABSENT below, which is
    # enforced in both directions: missing where needed *and* present where it
    # is not.
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
    # Both, and for different reasons: the API serves the exposition, the
    # worker writes the cross-process counters into Redis for the API to render
    # (ADR-069). A deployment that set it on one would silently publish half
    # the signals - which is the failure this whole guard exists to stop.
    "metrics_enabled": ("api", "worker"),
    # Both, and this one is not merely symmetric - it is the join. The trace a
    # worker continues is the one the API started, so a deployment that traced
    # one process and not the other, or sampled them differently, or pointed
    # them at different collectors, would produce traces with their middle
    # missing (ADR-083).
    "tracing_enabled": ("api", "worker"),
    "otel_": ("api", "worker"),
    # The worker holds reservations and renews them; the API publishes how
    # many have expired. A deployment where the two disagree would have one
    # process reporting a queue stuck while the other believed it fine
    # (ADR-074).
    "queue_visibility_timeout_seconds": ("api", "worker"),
    # Only the API reads it, and only to publish how stale the backup is. The
    # worker takes no backups and is deliberately not told where the status
    # file is - see EXPECTED_ABSENT below (ADR-075).
    "backup_status_path": ("api",),
    # Both, and this pair is the one where disagreement is silent rather than
    # loud: the worker writes an attachment and the API serves it back, so a
    # deployment where one is on `s3` and the other on `local` accepts files
    # into one store and reads from another, and nothing anywhere says so
    # (ADR-077).
    "media_storage_backend": ("api", "worker"),
    "media_storage_path": ("api", "worker"),
    "media_max_bytes": ("api", "worker"),
    "media_s3_": ("api", "worker"),
    # The retention sweep runs in the worker; the API is given the same values
    # so a mismatch is a configuration error rather than a behaviour nobody
    # notices (ADR-078).
    "media_retention_": ("api", "worker"),
}

# Fields a mapped prefix picks up that a given service deliberately does not
# carry, with the reason. Anything here is a decision; anything missing without
# being here is drift.
EXPECTED_ABSENT: dict[tuple[str, str], str] = {
    ("api", "RESEND_API_KEY"): (
        "the API never sends - `build_email_provider` validates this in the "
        "process that uses it, so the credential lives on the worker alone"
    ),
    ("worker", "RESEND_WEBHOOK_SECRET"): (
        "the worker never verifies a delivery event - "
        "`require_delivery_verification` validates this in the API, which is "
        "the only process that serves the webhook (ADR-063)"
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

# Values the *backup* process holds and no application process may. Asserted
# rather than assumed, because the whole point of giving backups their own
# container is that taking over an application container does not hand somebody
# the ability to read or delete the backups (ADR-075).
BACKUP_ONLY: tuple[str, ...] = (
    "BACKUP_DESTINATION",
    "BACKUP_S3_BUCKET",
    "BACKUP_S3_ENDPOINT_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


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


@pytest.mark.parametrize("key", sorted(EXPECTED_ABSENT))
def test_a_setting_a_process_does_not_need_is_not_injected_into_it(
    key: tuple[str, str],
) -> None:
    """The other half of the drift guard, and the half that was missing.

    Everything above asks whether a process is given what it reads. Nothing
    asked the reverse, so adding `RESEND_API_KEY` to the API - or putting
    `GOOGLE_CLIENT_SECRET` on the worker "to keep the two blocks the same" -
    would have passed every check in this file while widening the blast radius
    of whichever container was taken over first.

    `EXPECTED_ABSENT` is therefore read as a decision rather than an excuse:
    each entry says a process must *not* carry that value, and this is what
    holds the file to it.
    """
    service, name = key
    declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), service)

    assert name not in declared, (
        f"docker-compose.prod.yml passes {name} to {service!r}, which does not "
        f"need it: {EXPECTED_ABSENT[key]}. Remove it, or delete the "
        "EXPECTED_ABSENT entry if the process genuinely started reading it."
    )


def test_the_two_halves_of_the_resend_configuration_never_meet() -> None:
    """Neither container can both send mail and authenticate a delivery event.

    Stated as its own property because it is the concrete thing the split buys.
    A credential that can send as the platform's domain and a secret that
    decides which delivery reports are believed are separately damaging, and
    the point of putting them in different containers is that taking one does
    not hand over the other.
    """
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    for service in ("api", "worker"):
        declared = _service_environment(text, service)
        held = declared & {"RESEND_API_KEY", "RESEND_WEBHOOK_SECRET"}
        assert len(held) <= 1, f"{service} holds both halves of the Resend configuration: {held}"


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
        "MEDIA_S3_SECRET_ACCESS_KEY",
    )
    for name in secretish:
        for line in re.findall(rf"^\s*{name}:.*$", text, re.M):
            value = line.split(":", 1)[1].strip()
            assert value.startswith("${"), f"{name} is not interpolated: {line.strip()}"


@pytest.mark.parametrize("service", ["api", "worker"])
def test_no_application_process_holds_an_object_store_credential(service: str) -> None:
    """The reason backups run in their own container at all.

    An API or worker that could reach the backup bucket would mean a single
    compromised application container could delete the database *and* every
    copy of it, which is the one failure the off-host copy exists to survive.
    """
    declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), service)
    held = declared & set(BACKUP_ONLY)

    assert not held, (
        f"docker-compose.prod.yml gives {service!r} backup-store settings it must "
        f"not have: {sorted(held)}. Only the `backup` service holds these."
    )


def test_the_backup_service_holds_what_it_needs() -> None:
    """The other direction: a backup that cannot reach its destination is none."""
    declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), "backup")

    assert set(BACKUP_ONLY) <= declared
    assert "BACKUP_STATUS_PATH" in declared


def test_the_api_reads_the_backup_status_but_cannot_write_it() -> None:
    """A process that could edit its own backup status could invent one."""
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    block = re.search(r"^  api:\n(.*?)(?=^  \w|\Z)", text, re.M | re.S)
    assert block is not None

    mounts = re.findall(r"^      - (\$\{BACKUP_DIR[^\n]*)$", block.group(1), re.M)
    assert mounts, "the API does not mount the backup status directory"
    assert all(
        mount.endswith(":ro") for mount in mounts
    ), f"the backup directory must be read-only in the API: {mounts}"


# Values the *media* store uses. Named separately from BACKUP_ONLY on purpose:
# these are the credentials the application processes legitimately hold, and the
# whole point is that they are a different bucket under a different key from the
# backups (ADR-077).
MEDIA_STORE_SETTINGS: tuple[str, ...] = (
    "MEDIA_S3_BUCKET",
    "MEDIA_S3_ENDPOINT_URL",
    "MEDIA_S3_ACCESS_KEY_ID",
    "MEDIA_S3_SECRET_ACCESS_KEY",
)


@pytest.mark.parametrize("service", ["api", "worker"])
def test_media_credentials_are_never_the_backup_credentials(service: str) -> None:
    """The two stores must not be reachable with one key.

    An application container that could reach the backup bucket could delete the
    database and every copy of it, which is the one failure the off-host copy
    exists to survive (ADR-075). Media storage gives the API and the worker an
    object-store credential for the first time, so this asserts the thing that
    makes that safe: it is a *different* credential, and the generic AWS pair
    stays out of both processes.
    """
    declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), service)

    assert set(MEDIA_STORE_SETTINGS) <= declared, (
        f"{service} cannot reach the media store: missing "
        f"{sorted(set(MEDIA_STORE_SETTINGS) - declared)}"
    )
    assert not (declared & set(BACKUP_ONLY)), (
        f"{service} holds backup-store settings: {sorted(declared & set(BACKUP_ONLY))}. "
        "Media and backups are different buckets under different credentials."
    )


def test_the_backup_service_holds_no_media_credential() -> None:
    """The other direction, and it matters as much.

    The backup container is the one that may delete objects. Giving it the media
    credential would put every customer attachment inside the blast radius of
    the process whose whole job is deleting old files.
    """
    declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), "backup")
    held = declared & set(MEDIA_STORE_SETTINGS)

    assert not held, f"the backup service holds media-store settings: {sorted(held)}"


def test_the_media_store_is_never_mandatory_at_interpolation() -> None:
    """A deployment on local disk must still come up.

    `${VAR:?}` here would make the object store required by Compose rather than
    by the application, which is the wrong place for the decision: `Settings`
    knows that `s3` needs a bucket and `local` does not, and Compose does not
    (ADR-062).
    """
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    for name in MEDIA_STORE_SETTINGS:
        for line in re.findall(rf"^\s*{name}:.*$", text, re.M):
            assert ":?" not in line, f"{name} is mandatory at interpolation: {line.strip()}"


def test_production_never_allows_a_backup_that_stays_on_this_host() -> None:
    """`BACKUP_ALLOW_LOCAL_ONLY` is for a laptop, and this file is not one.

    Setting it here would make `upload_backup.sh` report success for a dump
    sitting beside the database it came from, which is the exact thing the
    off-host step exists to prevent.
    """
    for service in ("api", "worker", "backup"):
        declared = _service_environment(PROD_COMPOSE.read_text(encoding="utf-8"), service)
        assert "BACKUP_ALLOW_LOCAL_ONLY" not in declared, (
            f"{service} is told it may keep backups on this host. Naming it in a "
            "comment is fine; setting it here is not."
        )
