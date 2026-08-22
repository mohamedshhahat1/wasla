"""Application configuration.

All settings are environment-driven (Pydantic Settings). Secrets have no
production-ready defaults: the application refuses to start in production
while placeholder values are still in place.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
LogFormat = Literal["json", "console"]

# Sentinel, not a credential: production configuration rejects this value.
PLACEHOLDER_SECRET = "change-me"  # noqa: S105
MINIMUM_SECRET_LENGTH = 32
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

    # Meta / WhatsApp: configuration only until the WhatsApp phase lands
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_access_token: str | None = None
    meta_api_version: str = "v21.0"

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
    def _validate_production_hardening(self) -> Settings:
        """Fail fast rather than serve production traffic with unsafe defaults."""
        if not self.is_production:
            return self

        problems: list[str] = []
        if self.jwt_secret == PLACEHOLDER_SECRET or len(self.jwt_secret) < MINIMUM_SECRET_LENGTH:
            problems.append(
                "JWT_SECRET must be a random value of at least "
                f"{MINIMUM_SECRET_LENGTH} characters"
            )
        if self.debug:
            problems.append("DEBUG must be disabled")
        if problems:
            raise ValueError("invalid production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
