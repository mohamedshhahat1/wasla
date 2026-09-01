"""Application configuration.

All settings are environment-driven (Pydantic Settings). Secrets have no
production-ready defaults: the application refuses to start in production
while placeholder values are still in place.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.proxy import parse_trusted_proxies

Environment = Literal["local", "test", "staging", "production"]
LogFormat = Literal["json", "console"]

# Sentinel, not a credential: production configuration rejects this value.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105
MINIMUM_SECRET_LENGTH = 32
# A shape check for the configured sender, not RFC 5322. It exists to catch a
# typo and a pasted display name ("Wasla <no-reply@x.com>"), both of which the
# provider refuses permanently.
_EMAIL_SHAPE: Final = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
# The only schemes an emailed link may be built on. `javascript:` and `data:`
# are the reason this is an allowlist: APP_PUBLIC_URL is prefixed onto every
# reset and invitation link, so whatever it holds is what a recipient clicks.
# The Google redirect URI is checked against the same set, for the same reason.
_PUBLIC_URL_SCHEMES: Final = frozenset({"http", "https"})
# Every OAuth client id Google issues ends with this. Checking it catches the
# two paste errors that actually happen - the secret in the id field, or the
# bare project number - at the price of refusing to boot if Google ever changes
# a suffix that has been stable for over a decade. Worth it: the alternative
# failure is every login broken with the only error message living on Google's
# domain, which is far harder to diagnose than a container that says why.
_GOOGLE_CLIENT_ID_SUFFIX: Final = ".apps.googleusercontent.com"

# The absolute ceiling on an agent's `max_output_tokens`, independent of what a
# deployment configures below. Defined here rather than in the schema because
# `Settings` has to validate against it and configuration cannot import schemas
# without a cycle; the schema imports it from here instead, so the number has
# one home.
MAX_AGENT_OUTPUT_TOKENS: Final = 8_192

# Anything that is a number at all, sign included, so a negative integration id
# is refused as an out-of-range number rather than accepted as a method name.
_SIGNED_INTEGER: Final = re.compile(r"[+-]?\d+")


def _integration_token(raw: str) -> int | str:
    """One `PAYMOB_INTEGRATION_IDS` entry, as the provider will send it.

    A number is an integration id and becomes an integer; anything else is one
    of the method names Paymob documents (`"card"`) and stays a string.
    Deciding here rather than in the provider keeps the parsing in the one
    place that already owns turning environment text into settings.

    A *signed* number is deliberately still parsed as a number, so `-1` becomes
    the integer -1 and is refused by the range check. Treating it as a name
    would let a typo through as a payment method Paymob has never heard of,
    which is a checkout that fails at the customer rather than at boot.
    """
    token = raw.strip()
    return int(token) if _SIGNED_INTEGER.fullmatch(token) else token


def _key_mode(key: str | None, prefix: str) -> str | None:
    """Which Paymob mode a key declares, or None if it does not say.

    Documented shapes are `sk_test_…` / `sk_live_…` and `pk_test_…` /
    `pk_live_…`. Searched rather than matched from position 0 because some
    regions prefix the whole thing; the point is to read the mode, not to
    police the format.
    """
    if not key:
        return None
    for mode in ("test", "live"):
        if f"{prefix}_{mode}_" in key:
            return mode
    return None


def _public_url_problems(value: str) -> list[str]:
    """Whether APP_PUBLIC_URL can safely be the base of an emailed link."""
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in _PUBLIC_URL_SCHEMES:
        return [
            "APP_PUBLIC_URL must be an http or https URL; emailed links are built "
            "by prefixing it, so any other scheme becomes the link a recipient clicks"
        ]
    if not parsed.netloc:
        return ["APP_PUBLIC_URL must include a host, such as https://app.example.com"]
    if parsed.query or parsed.fragment:
        # Templates append their own path and query. A base carrying either
        # produces a malformed link rather than an unsafe one, but a malformed
        # reset link is a reset that cannot be completed.
        return ["APP_PUBLIC_URL must be an origin and optional path, with no query or fragment"]
    return []


def _google_problems(
    *,
    client_id: str | None,
    client_secret: str | None,
    redirect_uri: str | None,
    require_https: bool,
) -> list[str]:
    """Whether Google sign-in is configured well enough to be switched on.

    Checked at startup rather than discovered at the first login, because every
    failure in here is total and invisible from this side: a wrong client id or
    redirect uri means Google refuses every authorization request, and the only
    error message the user ever sees is on Google's domain and says nothing
    about which of our values was wrong.

    Note what is *not* validated, because it is not configuration. The issuer is
    a constant in the verification module - a configurable trust anchor is not a
    feature. The audience is the client id itself, so there is nothing separate
    to check and no way for the two to disagree.
    """
    problems: list[str] = []

    if not client_id:
        problems.append("GOOGLE_CLIENT_ID must be set when GOOGLE_ENABLED is true")
    elif not client_id.strip().endswith(_GOOGLE_CLIENT_ID_SUFFIX):
        problems.append(
            "GOOGLE_CLIENT_ID must be the OAuth client id issued by Google, "
            f"which ends in {_GOOGLE_CLIENT_ID_SUFFIX}"
        )

    if not client_secret:
        problems.append("GOOGLE_CLIENT_SECRET must be set when GOOGLE_ENABLED is true")
    elif client_id and client_secret.strip() == client_id.strip():
        # A paste error worth naming, because the resulting failure is an
        # `invalid_client` from Google that looks like a Google problem.
        problems.append("GOOGLE_CLIENT_SECRET must not be the same value as GOOGLE_CLIENT_ID")

    if not redirect_uri:
        problems.append("GOOGLE_REDIRECT_URI must be set when GOOGLE_ENABLED is true")
        return problems

    parsed = urlparse(redirect_uri.strip())
    scheme = parsed.scheme.lower()
    if scheme not in _PUBLIC_URL_SCHEMES:
        # The allowlist is what refuses `javascript:`, `data:` and a
        # protocol-relative `//host/path`, which urlparse reports with an empty
        # scheme. This value is never request input - no code path reads a
        # redirect target from a caller - so this guards a typo in deployment
        # configuration rather than an attack.
        problems.append(
            "GOOGLE_REDIRECT_URI must be an http or https URL; it is handed to "
            "Google as the address a browser is returned to"
        )
    elif require_https and scheme != "https":
        problems.append(
            "GOOGLE_REDIRECT_URI must be https in production: a single-use "
            "authorization code travels back to it in the query string"
        )

    if not parsed.netloc:
        problems.append(
            "GOOGLE_REDIRECT_URI must include a host, such as "
            "https://app.example.com/auth/google/callback"
        )

    if parsed.fragment:
        problems.append(
            "GOOGLE_REDIRECT_URI must not contain a fragment: RFC 6749 forbids "
            "one and Google refuses to register it"
        )

    return problems


# The only algorithms this application can be configured with. HMAC only:
# the signing key is a shared secret, so an asymmetric algorithm could only
# ever be configured wrongly here, and `none` is not an algorithm.
ALLOWED_JWT_ALGORITHMS: Final = frozenset({"HS256", "HS384", "HS512"})
VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


class Settings(BaseSettings):
    """Typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Wasla"
    environment: Environment = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True

    # Observability
    log_level: str = "INFO"
    log_format: LogFormat = "json"
    health_check_timeout_seconds: float = Field(default=2.0, gt=0)

    # HTTP
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    request_id_header: str = "X-Request-ID"

    # Security
    jwt_secret: str = PLACEHOLDER_SECRET
    # Constrained to symmetric HMAC, and validated below. The setting exists so
    # a deployment can move to a longer digest, not so it can choose a family:
    # `jwt_secret` is a shared secret, and handing an asymmetric algorithm a
    # shared secret is how key-confusion bugs are built.
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = Field(default=900, gt=0)
    refresh_token_ttl_seconds: int = Field(default=1_209_600, gt=0)

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://wasla:wasla@localhost:5432/wasla"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout: float = Field(default=30.0, gt=0)
    database_pool_recycle_seconds: int = Field(default=1800, gt=0)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=20, ge=1)
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0)

    # OpenAI: configuration only until the AI phase lands
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    # Separate from the chat model on purpose. Describing every photograph a
    # business receives is a recurring cost with its own budget, and a workspace
    # may reasonably want a cheaper model for it than the one that answers.
    openai_vision_model: str = "gpt-4.1-mini"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    # Runs on every customer message, so it is the model whose cost scales with
    # traffic rather than with usage of one feature. Separate from the chat
    # model so a workspace can classify cheaply and answer well.
    openai_sentiment_model: str = "gpt-4.1-mini"
    openai_timeout_seconds: float = Field(default=60.0, gt=0)
    # Which models a workspace may point an agent at, and how much output it may
    # buy per call. Both are cost controls, and both belong here rather than in
    # the schema: the schema cannot see configuration, and a deployment paying
    # the provider bill is the only party that can say what it is willing to
    # fund. Empty means "no restriction", which is the right default for a
    # single-tenant or development deployment and the wrong one for a SaaS -
    # `.env.example` sets an explicit list for that reason.
    #
    # `openai_model` above is always permitted whatever this says: it is the
    # fallback every agent gets when it names none, so a list that excluded it
    # would make the default agent unbuildable.
    openai_allowed_models: list[str] = Field(default_factory=list)
    # The ceiling on `agents.max_output_tokens`, and the default when an agent
    # names none. A null there previously meant "whatever the provider's own
    # default is", which is an unbounded per-call spend nobody chose.
    openai_max_output_tokens: int = Field(default=2_048, ge=1, le=MAX_AGENT_OUTPUT_TOKENS)

    # Media
    # Where downloaded attachments are written. A path on the local filesystem,
    # which requires the API and worker containers to share a volume; object
    # storage replaces this implementation without changing the setting
    # (ADR-023).
    media_storage_path: str = "/var/lib/wasla/media"
    # Larger than most of what WhatsApp permits, so the cap bites on video and
    # on nothing else. A file over it is recorded as skipped rather than
    # downloaded: the point is not to pay to move ninety megabytes in order to
    # discover there was nothing to read.
    media_max_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    # Credential encryption at rest (ADR-034). A key ring: the first key
    # encrypts, every key decrypts, so rotation is prepending one. Each is 32
    # random bytes, base64. Empty means a workspace cannot store its own Meta
    # token at all - the platform token is used instead, and an attempt to
    # supply one is refused rather than stored in the clear.
    # `NoDecode` for the same reason `cors_origins` has it: without it
    # pydantic-settings tries to JSON-decode a list field straight from the
    # environment and raises before any validator runs, so a plain
    # comma-separated value - the only thing a container environment can
    # comfortably express - fails at start-up.
    credential_encryption_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Request limits, enforced by the application rather than only by nginx.
    # nginx is one deployment topology, not a property of the software: run the
    # container directly and every limit configured there disappears.
    # Comfortably above the media cap, so an attachment upload is bounded by the
    # rule that understands attachments rather than by this blunt one.
    max_request_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    # The webhook's own cap. Far smaller because a WhatsApp delivery is a few
    # kilobytes of JSON, and it is the one endpoint an unauthenticated caller
    # can reach - the 32 MB above exists for media uploads by signed-in
    # colleagues, which is not what arrives here.
    webhook_max_request_bytes: int = Field(default=1024 * 1024, gt=0)
    # How long a handler may take. Bounds a pooled database connection being
    # held, not the client's patience. The WhatsApp webhook is exempt.
    request_timeout_seconds: float = Field(default=60.0, gt=0)

    # Which immediate peers are allowed to speak for the client behind them.
    # Empty is the safe default and means "there is no proxy": forwarding
    # headers are ignored entirely and the socket address is the client.
    #
    # This is not a convenience toggle. `X-Forwarded-For` is attacker-supplied
    # unless a proxy we control appended to it, and the shipped nginx uses
    # `$proxy_add_x_forwarded_for`, which *appends* - so the first entry is
    # whatever the caller sent. Trusting it lets anyone forge the identity that
    # authentication rate limiting counts by. Only when the connection actually
    # comes from a listed address is a forwarding header believed.
    #
    # `NoDecode` for the reason `cors_origins` has it: a comma-separated value
    # is the only thing a container environment expresses comfortably.
    trusted_proxy_ips: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Rate limiting
    # Off by default in tests and on everywhere else, because a limiter that is
    # on in the suite makes every test order-dependent: the twentieth login in a
    # file would fail for a reason the test never mentions.
    rate_limit_enabled: bool = True
    # Authentication: per client address, since the caller has no identity yet.
    # Ten attempts a minute is generous for a person and hostile to a script.
    rate_limit_auth_per_minute: int = Field(default=10, gt=0)
    # Authentication, counted per *account* rather than per address. This is the
    # limit that survives a botnet: an address-based one counts the attacker's
    # machines, and they have more machines than we have patience. Deliberately
    # tighter than the address limit, because one person signing in does not
    # need many attempts a minute and a password sprayer needs exactly this.
    rate_limit_login_per_account_per_minute: int = Field(default=5, gt=0)
    # Everything else a workspace does, counted per workspace rather than per
    # user: the limit protects the platform's shared resources, and a workspace
    # with fifty colleagues is fifty times the load of one with one.
    rate_limit_workspace_per_minute: int = Field(default=300, gt=0)
    # Broadcasts and template syncs, which are expensive per request and rare
    # per person. Deliberately much lower than the general workspace limit.
    rate_limit_campaign_per_minute: int = Field(default=30, gt=0)

    # Billing
    # The plan a workspace is entitled to when it has no subscription of its
    # own: every workspace that predates billing, and any created before one is
    # chosen. Named by code rather than by id so it is the same string in every
    # environment. A code that matches no plan leaves limits unenforced and logs
    # it, which is a better failure than taking a working deployment offline
    # over a missing catalogue row.
    default_plan_code: str = "starter"

    # Which processor collects money, if any (ADR-044). `manual` is the
    # existing behaviour and stays the default: it records what is owed and
    # waits for a human to confirm a bank transfer, which is how this product
    # has always billed and how every local deployment and test still does.
    billing_provider: Literal["manual", "paymob"] = "manual"

    # Paymob credentials. All three are separate secrets with separate jobs and
    # none is interchangeable with another:
    #   - the secret key authenticates *us to them* when creating an intention
    #   - the public key is not secret at all and goes in the checkout URL the
    #     customer's browser follows
    #   - the HMAC secret authenticates *them to us* on the callback, and is
    #     the only thing standing between a stranger and a forged payment
    # None has a default. A deployment that sets `BILLING_PROVIDER=paymob`
    # without them refuses to boot rather than failing at the first customer.
    paymob_secret_key: str | None = None
    paymob_public_key: str | None = None
    paymob_hmac_secret: str | None = None
    # Which payment integrations a checkout may use. Passed to Paymob's Create
    # Intention API as `payment_methods`, documented as "the Integration ID(s)
    # used to process the payment. Values can be provided as integers (e.g.,
    # 1256) or as names enclosed in quotes (e.g., "card")" - so both forms are
    # accepted here and neither is interpreted.
    #
    # **Nothing in this application knows what an entry means.** There is no
    # table mapping a number to card or wallet, because there is nothing this
    # code could do with one: the list is quoted to Paymob and Paymob decides
    # which methods to offer the customer. Switching from card to wallet, or
    # offering both, is this variable changing and no code changing.
    #
    # Test and live ids are different values and must match the mode of the
    # secret key used with them.
    paymob_integration_ids: Annotated[list[int | str], NoDecode] = Field(default_factory=list)
    # The Moto integration id, which is what Paymob gates merchant-initiated
    # charges on: both the MIT documentation and the Subscriptions Module
    # require one, and it is a distinct integration type that Paymob issues per
    # merchant rather than one the dashboard lets you create.
    #
    # None is the ordinary state and is not a misconfiguration. Without it,
    # renewals are collected by invoicing the customer and asking them to pay,
    # which is how this product billed before saved cards existed. With it,
    # `RecurringService` can debit a saved card when a renewal falls due.
    paymob_moto_integration_id: int | None = None
    # Chooses the API host *and* the checkout host, which are different hosts.
    # There is deliberately no sandbox setting: Paymob's documentation states
    # that test and live share a regional base URL and the keys decide the
    # mode, so a `PAYMOB_SANDBOX` flag would be a knob that controls nothing.
    paymob_region: Literal["egypt", "uae", "oman", "saudi"] = "egypt"

    # Meta / WhatsApp: configuration only until the WhatsApp phase lands
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_access_token: str | None = None
    meta_api_version: str = "v21.0"

    # Email (ADR-042). Off by default: a deployment that has not configured a
    # sender is a deployment that sends nothing, and every enqueue is a no-op
    # rather than a row that waits forever. Delivery is asynchronous through
    # the outbox and the email worker; the API process never sends, which is
    # why RESEND_API_KEY is validated by the sending process at startup
    # (integrations.email.build_email_provider) rather than here - requiring
    # it globally would force a credential into a container that never uses it.
    email_enabled: bool = False
    email_provider: Literal["resend", "fake"] = "resend"
    resend_api_key: str | None = None
    # Verifies Resend webhook signatures (Svix scheme). Absent means the
    # webhook endpoint refuses every delivery rather than trusting any.
    resend_webhook_secret: str | None = None
    email_from: str | None = None
    email_reply_to: str | None = None
    # The public origin emailed links point at. Configured, never derived
    # from a request: a reset link built from a Host header is a reset link
    # an attacker can aim at their own origin.
    app_public_url: str | None = None
    email_max_attempts: int = Field(default=8, gt=0)
    email_worker_poll_seconds: float = Field(default=10.0, gt=0)

    # Email verification (docs/EMAIL_VERIFICATION.md). Both bounds are enforced
    # by the field rather than by the production validator below, and that is
    # the stronger choice: an unusable lifetime is unsafe in staging and in a
    # developer's container too, so the value is refused wherever it is set.
    #
    # Refused rather than clamped, for the reason the service gives: silently
    # correcting configuration is how an operator comes to believe a code lives
    # for a day when it lives for an hour.
    #
    # The floor is a minute because anything shorter expires while somebody
    # switches to their mail client. The ceiling is an hour because the entire
    # security argument for six digits is that the window is small - a code
    # valid for a day is a password with twenty bits of entropy.
    email_verification_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    # Wrong answers one challenge tolerates before it is dead even for the
    # right code. Capped at ten: the code space is a million values, so a
    # ceiling an operator could raise to a thousand would turn the challenge
    # into something worth guessing. One is permitted because a deployment that
    # wants no second chances is making a defensible choice.
    email_verification_max_attempts: int = Field(default=5, ge=1, le=10)

    # Google sign-in (ADR-047, docs/GOOGLE_OAUTH.md). Off by default, and off
    # means the endpoints answer 404: a feature nobody configured does not
    # exist in this deployment, which is a different statement from 503's "it
    # is temporarily unwell, try again".
    #
    # Only four settings. The issuer is a constant in the verification module,
    # because a configurable trust anchor is not a feature anyone wants, and
    # the audience is the client id below rather than a fifth setting that
    # could disagree with it.
    google_enabled: bool = False
    google_client_id: str | None = None
    # Read by the API process and used in exactly one place: the direct
    # server-to-server token exchange. Never a build argument, never in an
    # image layer, never in a frontend bundle, never in a response body
    # including /health, and never logged.
    google_client_secret: str | None = None
    # Where Google returns the browser. Configuration, never request input:
    # nothing in this application reads a redirect target from a caller, which
    # is what makes open redirection structurally impossible here rather than
    # something a validator has to keep catching.
    google_redirect_uri: str | None = None

    @field_validator("paymob_integration_ids", mode="before")
    @classmethod
    def _parse_integration_ids(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list.

        Comma-separated because that is what a container environment can
        express, following `credential_encryption_keys`.

        Numeric entries become integers and everything else stays a string,
        because Paymob's Create Intention API documents `payment_methods` as
        taking "the Integration ID(s) used to process the payment. Values can
        be provided as integers (e.g., 1256) or as names enclosed in quotes
        (e.g., "card")". Both forms are passed through untouched, so which
        payment methods a deployment offers stays a configuration question -
        which is the whole point of this setting.

        Nothing here decides what a method *is*. An id is an opaque token to
        this application: it is quoted to Paymob and Paymob decides what it
        means, so there is no table here mapping numbers to card or wallet and
        no way for one to fall out of date.
        """
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [_integration_token(item) for item in raw.split(",") if item.strip()]
        if isinstance(value, list):
            return [_integration_token(item) if isinstance(item, str) else item for item in value]
        return value

    @field_validator("paymob_integration_ids", mode="after")
    @classmethod
    def _check_integration_ids(cls, value: list[int | str]) -> list[int | str]:
        """Refuse entries that cannot be real, and refuse the same one twice.

        A duplicate is not harmless: the list is sent to the provider as the
        payment methods to offer, and a repeated entry offers the same method
        twice on the checkout page. A non-positive id is a parsing accident -
        `0` is what an empty field becomes if the split above is ever loosened
        - and it would silently disable a payment method rather than fail.

        Refused rather than de-duplicated, following the rule this file already
        follows for a lifetime out of range: silently correcting configuration
        is how an operator comes to believe something is set that is not.
        """
        for item in value:
            if isinstance(item, bool) or (isinstance(item, int) and item <= 0):
                raise ValueError("PAYMOB_INTEGRATION_IDS must all be positive integers or names")
            if isinstance(item, str) and not item.strip():
                raise ValueError("PAYMOB_INTEGRATION_IDS must not contain an empty entry")
        if len({str(item) for item in value}) != len(value):
            raise ValueError("PAYMOB_INTEGRATION_IDS must not repeat an entry")
        return value

    @field_validator("credential_encryption_keys", mode="before")
    @classmethod
    def _parse_encryption_keys(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list.

        Comma-separated is what a container environment can actually express,
        and the order matters: the first key is the one that encrypts.
        """
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list."""
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def _parse_trusted_proxy_ips(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a list."""
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxy_ips", mode="after")
    @classmethod
    def _check_trusted_proxy_ips(cls, value: list[str]) -> list[str]:
        """Refuse an entry that is not an address or a network.

        Fail-fast, in every environment, and the failure it prevents is one
        that produced no error at all. `docker-compose.prod.yml` shipped
        `TRUSTED_PROXY_IPS=nginx` - a Docker service name - and the comparison
        was against `request.client.host`, which is an IP. Nothing ever
        matched, so forwarding headers were ignored, every client on the
        internet shared one authentication rate-limit bucket, and HSTS was
        never emitted. All of that looked exactly like a correctly configured
        deployment (ADR-060).

        A hostname is refused rather than resolved. Resolving one would put the
        trust anchor for forwarding headers under whatever answers DNS, and
        this list exists precisely because that decision must not be
        influenceable from outside.
        """
        parse_trusted_proxies(value)
        return value

    @field_validator("jwt_algorithm", mode="before")
    @classmethod
    def _validate_jwt_algorithm(cls, value: Any) -> Any:
        """Refuse anything but the HMAC family.

        Two failures this closes, both configuration-only and both silent.

        **`none`.** PyJWT registers the null algorithm, and a deployment that
        set `JWT_ALGORITHM=none` would be listing it in the `algorithms=`
        allowlist that is otherwise the defence against algorithm confusion.
        The library happens to refuse a non-empty key for it today; relying on
        that is relying on somebody else's implementation detail to protect the
        thing that decides who a request is.

        **The asymmetric families.** `jwt_secret` is a shared secret. Naming
        `RS256` here would have the application verify with that string as a
        public key, which is the classic confusion: anyone who learns the
        "public" key can sign. There is no key-pair configuration in this
        application, so an asymmetric algorithm cannot be configured correctly -
        only incorrectly.

        Note that Google ID tokens *are* RS256, and are verified somewhere else
        entirely: `app/integrations/google/` names its own algorithm as a
        literal and never reads this setting. The two token families are
        deliberately not interchangeable - different verifier, different key
        material, different issuer, different required claims.

        Refused at startup, so a misconfiguration is a container that will not
        boot rather than an authentication system that quietly stops meaning
        anything.
        """
        if not isinstance(value, str):
            return value
        algorithm = value.strip().upper()
        if algorithm not in ALLOWED_JWT_ALGORITHMS:
            allowed = ", ".join(sorted(ALLOWED_JWT_ALGORITHMS))
            raise ValueError(f"jwt_algorithm must be one of: {allowed}")
        return algorithm

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        level = value.strip().upper()
        if level not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(f"log_level must be one of: {allowed}")
        return level

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_testing(self) -> bool:
        return self.environment == "test"

    @model_validator(mode="after")
    def _validate_hardening(self) -> Settings:
        """Fail fast rather than serve traffic with unsafe defaults.

        The signing-key rule deliberately applies to **every environment except
        `test`**, and that is a change from an earlier version which only checked
        it when `environment` was literally `production`.

        The old shape was unsafe in two ways that were easy to miss. `staging` is
        a first-class tier here - reachable from the internet, because Meta has
        to deliver webhooks to it - and it skipped the check entirely, so it ran
        on whatever `.env.example` shipped. And `environment` itself defaults to
        `local`, so a container started without `ENVIRONMENT` set skipped the
        check too: the guard protecting the key was opt-in through a field that
        defaults to the unprotected side.

        `jwt_secret` is not one credential among several. It is the only thing
        standing between a stranger and a token that names any user - including
        one carrying a platform role - because `get_current_user` trusts a valid
        signature completely. A key that ships in the repository is not a secret,
        it is a checksum, and no downstream check can compensate for it.

        `test` is exempt so the suite can build settings without ceremony. It is
        the one environment that never listens on a network.
        """
        problems: list[str] = []

        if not self.is_testing and (
            self.jwt_secret == PLACEHOLDER_SECRET or len(self.jwt_secret) < MINIMUM_SECRET_LENGTH
        ):
            problems.append(
                "JWT_SECRET must be a random value of at least "
                f"{MINIMUM_SECRET_LENGTH} characters; generate one with "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )

        if self.is_production:
            if self.debug:
                problems.append("DEBUG must be disabled")
            if self.docs_enabled:
                problems.append(
                    "DOCS_ENABLED must be false: the interactive reference publishes "
                    "every route and schema, including platform administration"
                )
            if not self.meta_app_secret:
                # Without it the webhook cannot verify a Meta signature, and the
                # endpoint answers 503 to every delivery - a silent integration
                # outage that looks like Meta's fault.
                problems.append("META_APP_SECRET must be set so webhook signatures can be verified")
            if "*" in self.cors_origins:
                # The middleware is configured with `allow_credentials=True`, and
                # Starlette answers a wildcard-plus-credentials configuration by
                # echoing whatever `Origin` arrives - so every site on the
                # internet becomes an allowed origin. This API authenticates with
                # a bearer token rather than a cookie, which limits the damage,
                # but "the other control saves us" is not a reason to ship the
                # combination. Name the origins.
                problems.append(
                    "CORS_ORIGINS must name each allowed origin explicitly; "
                    "'*' is not permitted with credentialed requests"
                )

        if self.email_enabled and not self.is_testing:
            # Fail closed where email is *required*: a deployment that turned
            # it on and half-configured it should refuse to boot, not enqueue
            # rows that render broken links or send from nobody (ADR-042).
            if not self.email_from:
                problems.append("EMAIL_FROM must be set when EMAIL_ENABLED is true")
            elif not _EMAIL_SHAPE.match(self.email_from.strip()):
                # Checked here rather than discovered by the worker. A sender
                # the provider rejects is a *permanent* failure on every row,
                # so a typo in this one value silently discards every email
                # the deployment ever queues.
                problems.append(
                    "EMAIL_FROM must be a bare email address such as " "no-reply@example.com"
                )

            if not self.app_public_url:
                problems.append(
                    "APP_PUBLIC_URL must be set when EMAIL_ENABLED is true: emailed "
                    "links need a configured origin, never one derived from a request"
                )
            else:
                problems.extend(_public_url_problems(self.app_public_url))

            if self.is_production:
                if self.email_provider != "resend":
                    problems.append(
                        "EMAIL_PROVIDER must be a real provider in production; "
                        "the fake delivers nothing and says it succeeded"
                    )
                if self.app_public_url and not self.app_public_url.startswith("https://"):
                    problems.append(
                        "APP_PUBLIC_URL must be https in production: reset and "
                        "invitation tokens travel in these links"
                    )
                if not self.resend_webhook_secret:
                    # Without it the delivery-event endpoint answers 503 to
                    # every call, so bounces and complaints are never recorded
                    # and the platform keeps writing to dead mailboxes until
                    # the sending domain is the thing that fails. The same
                    # reasoning META_APP_SECRET is required on.
                    problems.append(
                        "RESEND_WEBHOOK_SECRET must be set so delivery events can be "
                        "verified; without it bounces and complaints are never recorded"
                    )

        if self.billing_provider == "paymob" and not self.is_testing:
            # Fail closed where money is involved, and in every environment
            # rather than only production: a staging deployment configured to
            # take payments and missing its HMAC secret would answer 503 to
            # every callback, so real transactions would complete at Paymob and
            # never be recorded here. That is the worst failure this subsystem
            # has - the customer is charged and gets nothing.
            if not self.paymob_secret_key:
                problems.append("PAYMOB_SECRET_KEY must be set when BILLING_PROVIDER is paymob")
            if not self.paymob_public_key:
                problems.append("PAYMOB_PUBLIC_KEY must be set when BILLING_PROVIDER is paymob")
            if not self.paymob_hmac_secret:
                # The one that is not merely configuration. Without it a
                # callback cannot be authenticated, and an endpoint that cannot
                # authenticate a payment notification must refuse every one.
                problems.append(
                    "PAYMOB_HMAC_SECRET must be set when BILLING_PROVIDER is paymob: "
                    "callbacks cannot be verified without it"
                )
            if not self.paymob_integration_ids:
                problems.append(
                    "PAYMOB_INTEGRATION_IDS must name at least one integration id; "
                    "an intention with no payment method is refused by Paymob"
                )
            if not self.app_public_url:
                # The callback URL is built from it, and a callback URL derived
                # from a request Host header is a callback an attacker can aim.
                problems.append(
                    "APP_PUBLIC_URL must be set when BILLING_PROVIDER is paymob: "
                    "the callback URL is built from it"
                )
            elif self.is_production and not self.app_public_url.startswith("https://"):
                problems.append(
                    "APP_PUBLIC_URL must be https in production: payment callbacks travel to it"
                )

            problems.extend(self._paymob_key_problems())

        if self.google_enabled and not self.is_testing:
            # Fail closed the same way email does, and for a sharper reason:
            # half-configured Google sign-in is a button that always fails, and
            # the only error message lives on Google's domain.
            problems.extend(
                _google_problems(
                    client_id=self.google_client_id,
                    client_secret=self.google_client_secret,
                    redirect_uri=self.google_redirect_uri,
                    require_https=self.is_production,
                )
            )

        if problems:
            raise ValueError(f"invalid {self.environment} configuration: " + "; ".join(problems))
        return self

    def _paymob_key_problems(self) -> list[str]:
        """Catch the two credential mistakes that produce a working-looking mess.

        Paymob issues the secret and public keys per *mode*, prefixed `sk_test_`
        / `sk_live_` and `pk_test_` / `pk_live_`, and documents that the keys
        decide whether a transaction is real - there is no sandbox host to point
        at, which is why this file has no sandbox flag.

        That makes a mismatched pair the dangerous case. A live secret key with
        a test public key creates a real intention and sends the customer to a
        test payment page, so nothing is ever collected and every callback is
        for money that does not exist. Nothing else in the system can notice:
        both halves look perfectly valid on their own.

        The second is a deployment that believes it is live and is not - test
        keys in production means every payment is pretend and every customer
        gets the product free.

        Matched by substring rather than prefix on purpose. Some regions issue
        keys with a country prefix ahead of the `sk_`, and refusing to boot over
        a shape this integration has not seen would be worse than the mistake
        being guarded against. A key with no recognisable mode at all is left
        alone for the same reason - it is reported only when its partner *does*
        declare one, because that is when they can be shown to disagree.
        """
        modes = {
            "PAYMOB_SECRET_KEY": _key_mode(self.paymob_secret_key, "sk"),
            "PAYMOB_PUBLIC_KEY": _key_mode(self.paymob_public_key, "pk"),
        }
        declared = {name: mode for name, mode in modes.items() if mode is not None}
        if len(set(declared.values())) > 1:
            names = ", ".join(sorted(declared))
            return [
                f"{names} are for different Paymob modes; a live secret key with a "
                "test public key creates real intentions behind a test payment page"
            ]
        if self.is_production and "test" in declared.values():
            return [
                "Paymob test keys must not be used in production: every payment "
                "would be pretend and every customer would get the product free"
            ]
        return []


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
