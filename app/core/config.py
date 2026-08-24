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
_PUBLIC_URL_SCHEMES: Final = frozenset({"http", "https"})


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

        if problems:
            raise ValueError(f"invalid {self.environment} configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
