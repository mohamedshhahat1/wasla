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
import yaml

from app.core.config import Settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
NGINX_CONF = ROOT / "nginx" / "nginx.conf"


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
    # Entitlements (ADR-131). Both: the API reports open AI holds and a
    # reduction's grace, the worker takes holds, sweeps them and disables at
    # the grace's end - two processes disagreeing would count differently.
    "ai_turn_hold_ttl_seconds": ("api", "worker"),
    "channel_capacity_grace_days": ("api", "worker"),
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
    # WhatsApp. Both processes call the Graph API - the API when a colleague
    # replies by hand from the inbox, the worker for agent replies, follow-ups,
    # campaigns and media downloads - so both need the credential and the
    # version. This mapping was absent, which is how the API came to serve three
    # send routes in production with no `META_ACCESS_TOKEN` at all.
    "meta_": ("api", "worker"),
    # The knowledge base. Both: the API's search preview and the worker's agents
    # resolve the same retrieval switches, and a deployment that set them on one
    # would preview a search the agents do not run. The cache, retention and
    # indexing knobs are the worker's; the API is given them so the two never
    # disagree (as `media_retention_`). This mapping was absent, so none of them
    # reached production and the first deploy's `RAG_HYBRID_ENABLED=false` did
    # nothing (RGM-12).
    "rag_": ("api", "worker"),
    # The embedding switch's target: the API embeds the preview's query in it,
    # the worker builds shadows and answers customers in it (RGM-10, RGM-12).
    "openai_embedding_model_next": ("api", "worker"),
    # The signed client-IP forward (FEB-01, ADR-190). Only the API serves the
    # routes the frontend tier signs for; the worker serves no HTTP.
    "session_forward_secret": ("api",),
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
    ("worker", "META_APP_SECRET"): (
        "only the API serves the webhook, so only the API verifies a signature"
    ),
    ("worker", "META_VERIFY_TOKEN"): ("only the API answers Meta's subscription challenge"),
    ("worker", "META_INSTAGRAM_APP_SECRET"): "only the API verifies a webhook signature",
    ("worker", "META_MESSENGER_APP_SECRET"): "only the API verifies a webhook signature",
    ("api", "META_APP_ID"): "declared for documentation; no code reads it",
    ("worker", "META_APP_ID"): "declared for documentation; no code reads it",
}

# Values the *backup* process holds and no application process may. Asserted
# rather than assumed, because the whole point of giving backups their own
# container is that taking over an application container does not hand somebody
# the ability to read or delete the backups (ADR-075).
# Feature settings that *are* mandatory at interpolation, with the reason. The
# test below exists because refusing to boot over an absent Google client secret
# would make an optional integration compulsory; these are the ones where
# refusing is the point.
MANDATORY_BY_DESIGN: dict[str, str] = {
    "META_APP_SECRET": (
        "the webhook's signature check is the whole of its authorization, and "
        "production refuses to serve it without one - a deployment that booted "
        "anyway would accept an unauthenticated POST from anybody"
    ),
}

# Settings an operator is deliberately not shown, with the reason. Anything here
# is a decision; anything missing without being here is drift, and the test
# below is what makes that true.
#
# Empty today, and worth keeping as the mechanism rather than deleting: the
# argument for hiding a setting is always specific - a knob only a test turns, a
# value a deployment cannot usefully change - and it belongs next to the name it
# applies to rather than in a commit message.
UNDOCUMENTED_BY_DESIGN: dict[str, str] = {}

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


def _environment_names() -> set[str]:
    """Every `Settings` field, as the environment variable that supplies it."""
    return {
        str(field.validation_alias or field.alias or name).upper()
        for name, field in Settings.model_fields.items()
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

    offenders = mandatory & _expected(service) - set(MANDATORY_BY_DESIGN)
    assert not offenders, (
        f"{service}: feature settings must be optional at interpolation time: "
        f"{sorted(offenders)}"
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


def test_production_database_owner_secret_reaches_only_migrate() -> None:
    compose = PROD_COMPOSE.read_text(encoding="utf-8")
    assert "MIGRATION_DATABASE_URL" in _service_environment(compose, "migrate")
    for service in ("api", "worker", "backup"):
        environment = _service_environment(compose, service)
        assert "MIGRATION_DATABASE_URL" not in environment
        assert "DATABASE_URL" in environment
    assert "DATABASE_URL" in _service_environment(compose, "migrate")
    assert "python -m scripts.provision_runtime_db_role" in (
        ROOT / "scripts" / "entrypoint.sh"
    ).read_text(encoding="utf-8")


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


def test_env_example_documents_every_setting_that_exists() -> None:
    """The whole of `Settings`, not only what a Compose file forwards.

    The test above answers "can a deployment supply what it must", and it is
    scoped to the prefixes `FEATURE_SETTINGS` maps - which is correct for what
    it asserts and is why it missed two security controls. Both
    `RATE_LIMIT_LOGIN_PER_ACCOUNT_PER_MINUTE` - the login budget counted per
    account, described in `config.py` as the limit that survives a botnet - and
    `WEBHOOK_MAX_REQUEST_BYTES` - the cap on the one endpoint an
    unauthenticated caller can reach - have defaults and appear in no Compose
    file, so they fell outside that assertion entirely and an operator had no
    way to discover either existed.

    This one has no scope. Every field is either documented or listed below
    with a reason, so adding a setting is a decision about whether operators
    should know it exists rather than an omission nobody notices.
    """
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.M))
    missing = sorted(_environment_names() - documented - set(UNDOCUMENTED_BY_DESIGN))

    assert not missing, (
        ".env.example does not mention: "
        f"{', '.join(missing)}. Document it, or add it to UNDOCUMENTED_BY_DESIGN "
        "with the reason it should not be."
    )


def test_every_deliberate_omission_names_a_real_setting() -> None:
    """So the exception list cannot rot into a list of settings that once existed."""
    stale = sorted(set(UNDOCUMENTED_BY_DESIGN) - _environment_names())
    assert not stale, f"UNDOCUMENTED_BY_DESIGN names settings that are gone: {stale}"


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


# --------------------------------------------------------------------- nginx
#
# FEB-35 (NX-01 .. NX-04): the proxy's access log never writes a query string.
# A search term, a filter and the event stream's ticket all travel there, and
# the application's own logs record none of them - a proxy in front that wrote
# the request line would undo that from outside the application.

#: What an access-log format may never contain: the request line, the raw
#: request target, the arguments, the referring page and credentials.
FORBIDDEN_LOG_VARIABLES = (
    "$request ",
    "$request'",
    '$request"',
    "$request_uri",
    "$args",
    "$arg_",
    "$query_string",
    "$is_args",
    "$http_referer",
    "$http_authorization",
    "$http_cookie",
    "$sent_http_set_cookie",
)
#: nginx's own formats: `combined` (built in) and `main` (the image's
#: nginx.conf). Both write the request line and the referrer.
NGINX_OWN_FORMATS = {"combined", "main"}


def _nginx_directives() -> list[str]:
    """The file's directives, comments stripped, one string per `;` or block opener."""
    lines = [line.split("#", 1)[0] for line in NGINX_CONF.read_text(encoding="utf-8").splitlines()]
    return [part.strip() for part in re.split(r"[;{}]", " ".join(lines)) if part.strip()]


def _nginx_server_blocks() -> list[str]:
    """Each `server { ... }` of the file (upstream's `server api:8000` is not one)."""
    text = "\n".join(
        line.split("#", 1)[0] for line in NGINX_CONF.read_text(encoding="utf-8").splitlines()
    )
    blocks: list[str] = []
    for match in re.finditer(r"^server\s*\{", text, flags=re.MULTILINE):
        depth, index = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        blocks.append(text[match.end() : index - 1])
    return blocks


def _log_formats() -> dict[str, str]:
    formats: dict[str, str] = {}
    for directive in _nginx_directives():
        if directive.startswith("log_format "):
            _, name, body = directive.split(None, 2)
            formats[name] = body
    return formats


def test_no_nginx_log_format_can_write_a_query_string() -> None:
    """NX-T1: no format holds the request line, the target, the arguments or the referrer."""
    formats = _log_formats()
    assert formats, "the file declares its own access-log format"
    for name, body in formats.items():
        found = [variable for variable in FORBIDDEN_LOG_VARIABLES if variable in body + " "]
        assert not found, f"log_format {name} writes {found}"
        assert "$uri" in body, f"log_format {name} records the path"


def test_every_nginx_access_log_names_a_declared_format_or_is_off() -> None:
    """NX-T1, NX-T2: never nginx's own `combined` or `main`, and never a format left to default."""
    formats = _log_formats()
    logs = [d.split() for d in _nginx_directives() if d.startswith("access_log ")]
    assert logs
    for words in logs:
        if words[1] == "off":
            continue
        assert len(words) >= 3, f"{' '.join(words)} names no format: nginx would use `combined`"
        assert words[2] not in NGINX_OWN_FORMATS, " ".join(words)
        assert words[2] in formats, " ".join(words)


def test_every_nginx_server_sets_its_own_access_log() -> None:
    """NX-T2: a `server` that set none would inherit the image's `main` format from `http`."""
    blocks = _nginx_server_blocks()
    assert len(blocks) == 2
    formats = _log_formats()
    for block in blocks:
        own = [
            line.split()
            for line in (part.strip() for part in re.split(r"[;{}]", _top_level(block)))
            if line.startswith("access_log ")
        ]
        assert own, "a server block without an access_log of its own"
        assert all(words[1] == "off" or words[2] in formats for words in own)


def _top_level(block: str) -> str:
    """The directives of a block that are not inside a nested block."""
    out, depth = [], 0
    for character in block:
        if character == "{":
            depth += 1
            out.append(";")
        elif character == "}":
            depth -= 1
            out.append(";")
        elif depth == 0:
            out.append(character)
    return "".join(out)


def test_nginx_keeps_what_it_always_set() -> None:
    """NX-T3: the log lines are all that changed - limits, headers, timeouts and refusals stand."""
    directives = _nginx_directives()
    for expected in (
        "client_max_body_size 10m",
        "client_body_timeout 30s",
        'add_header X-Content-Type-Options "nosniff" always',
        'add_header X-Frame-Options "DENY" always',
        'add_header Referrer-Policy "no-referrer" always',
        "proxy_read_timeout 75s",
        "proxy_read_timeout 1h",
        "proxy_buffering off",
        "return 404",
        "return 301 https://$host$request_uri",
        "server_tokens off",
    ):
        assert expected in directives, expected
    assert directives.count("access_log off") == 2
    assert not [d for d in directives if d.startswith(("log_subrequest", "rewrite_log"))]


# ------------------------------------------------------- nginx's log retention

#: The largest a single log file and the number of files nginx's output may
#: keep on the host (NL-T1). The shipped values sit well under both.
NGINX_LOG_MAX_SIZE_MB = 50
NGINX_LOG_MAX_FILES = 10
DEPLOYMENT_DOC = ROOT / "docs" / "DEPLOYMENT.md"


def _service_block(text: str, service: str) -> str:
    block = re.search(rf"^  {service}:\n(.*?)(?=^  \w|\Z)", text, re.M | re.S)
    assert block is not None, f"no service named {service} in the compose file"
    return block.group(1)


def _size_in_mb(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg])", value.strip().lower())
    assert match is not None, f"max-size {value!r} is not a bounded size"
    number, unit = float(match.group(1)), match.group(2)
    return number * {"k": 1 / 1024, "m": 1.0, "g": 1024.0}[unit]


def test_nginx_output_is_bounded_on_the_host() -> None:
    """NL-T1: nginx's error log (stderr) cannot grow without bound (FEB-35's residual, ADR-200)."""
    block = _service_block(PROD_COMPOSE.read_text(encoding="utf-8"), "nginx")
    logging = re.search(r"^    logging:\n(.*?)(?=^    \w|\Z)", block, re.M | re.S)
    assert logging is not None, "the nginx service has no logging block: Docker keeps it unbounded"
    body = logging.group(1)
    assert re.search(r"^      driver: json-file$", body, re.M), "nginx must log through json-file"
    size = re.search(r'^        max-size: "([^"]+)"$', body, re.M)
    files = re.search(r'^        max-file: "(\d+)"$', body, re.M)
    assert size is not None and files is not None, "both max-size and max-file must be set"
    assert 0 < _size_in_mb(size.group(1)) <= NGINX_LOG_MAX_SIZE_MB
    assert 1 <= int(files.group(1)) <= NGINX_LOG_MAX_FILES


def test_the_deploy_guide_lists_the_log_retention_before_the_first_deploy() -> None:
    """NL-T2: the retention duty is on the operator's first-deploy list."""
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    section = re.search(r"^### Before the first deploy\n(.*?)(?=^#|\Z)", text, re.M | re.S)
    assert section is not None, "docs/DEPLOYMENT.md has no 'Before the first deploy' list"
    assert "- [ ] **nginx's log retention**" in section.group(1)
    assert "- [ ] **nginx's log readers restricted**" in section.group(1)


# The bound every service's own output keeps on the host (FEB-41, ADR-201): the
# one nginx has carried since FE-6, now shared - about 50 MB per service.
SERVICE_LOG_MAX_SIZE_MB = 10
SERVICE_LOG_MAX_FILES = 5


def _rendered_services() -> dict[str, dict[str, object]]:
    rendered = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, dict[str, object]] = rendered["services"]
    return services


def test_every_service_keeps_its_output_bounded() -> None:
    """SL-T1: every service, present or added later, logs through a bounded json-file."""
    unbounded = []
    for name, service in _rendered_services().items():
        logging = service.get("logging")
        if not isinstance(logging, dict) or logging.get("driver") != "json-file":
            unbounded.append(name)
            continue
        options = logging.get("options") or {}
        assert isinstance(options, dict), name
        size, files = options.get("max-size"), options.get("max-file")
        if not isinstance(size, str) or not isinstance(files, str):
            unbounded.append(name)
            continue
        assert 0 < _size_in_mb(size) <= SERVICE_LOG_MAX_SIZE_MB, name
        assert 1 <= int(files) <= SERVICE_LOG_MAX_FILES, name
    assert unbounded == [], f"services whose output Docker keeps without a bound: {unbounded}"


def test_the_shared_bound_is_nginx_s_own() -> None:
    """SL-T2: one bound for every service, the values nginx already carried."""
    rendered = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    shared = rendered["x-logging"]
    assert shared == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}
    assert rendered["services"]["nginx"]["logging"] == shared
