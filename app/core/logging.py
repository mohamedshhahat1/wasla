"""Structured logging.

Logs are single-line JSON in deployed environments and carry the correlation
context (request_id, and where known tenant_id, user_id, conversation_id).
Secret-bearing fields are redacted before serialisation: API keys, access
tokens, passwords and signatures must never reach the logs.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

from app.core.config import Settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)

_CONTEXT_VARS: Final[dict[str, ContextVar[str | None]]] = {
    "request_id": request_id_var,
    "tenant_id": tenant_id_var,
    "user_id": user_id_var,
    "conversation_id": conversation_id_var,
}

REDACTED: Final = "[REDACTED]"
_SENSITIVE_HINTS: Final = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "signature",
)

_RESERVED_RECORD_KEYS: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def bind_log_context(**values: str | None) -> None:
    """Bind correlation values for the current async context."""
    for key, value in values.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            var.set(value)


def clear_log_context() -> None:
    """Reset all correlation values."""
    for var in _CONTEXT_VARS.values():
        var.set(None)


def current_log_context() -> dict[str, str]:
    """Return the correlation values that are currently set."""
    return {key: value for key, var in _CONTEXT_VARS.items() if (value := var.get()) is not None}


def is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SENSITIVE_HINTS)


def redact(value: Any, key: str = "") -> Any:
    """Recursively replace secret-bearing values with a redaction marker."""
    if key and is_sensitive(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def _serialisable(value: Any) -> Any:
    if isinstance(value, str | int | float | bool | dict | list | type(None)):
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_log_context())

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key.startswith("_") or key in payload:
                continue
            payload[key] = _serialisable(redact(value, key))

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local development."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = current_log_context()
        if context:
            base = f"{base} | " + " ".join(f"{key}={value}" for key, value in context.items())
        return base


def configure_logging(settings: Settings) -> None:
    """Install a single stdout handler and align third-party loggers with it."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else ConsoleFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # Access logs are emitted by RequestContextMiddleware in our own format.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
