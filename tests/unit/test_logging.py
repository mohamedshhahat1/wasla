"""Structured logging tests."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.logging import (
    REDACTED,
    ConsoleFormatter,
    JsonFormatter,
    bind_log_context,
    clear_log_context,
    redact,
)


@pytest.fixture(autouse=True)
def _reset_log_context() -> Iterator[None]:
    clear_log_context()
    yield
    clear_log_context()


def _record(message: str = "hello", **extra: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_core_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record(event="test.event")))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "hello"
    assert payload["event"] == "test.event"
    assert "timestamp" in payload


def test_json_formatter_includes_bound_request_context() -> None:
    bind_log_context(request_id="req-123", tenant_id="tenant-abc")

    payload = json.loads(JsonFormatter().format(_record()))

    assert payload["request_id"] == "req-123"
    assert payload["tenant_id"] == "tenant-abc"
    assert "user_id" not in payload


def test_json_formatter_redacts_sensitive_extras() -> None:
    record = _record(
        api_key="sk-live-secret",
        authorization="Bearer abc",
        meta_signature="sha256=deadbeef",
        tenant_name="ACME",
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["api_key"] == REDACTED
    assert payload["authorization"] == REDACTED
    assert payload["meta_signature"] == REDACTED
    assert payload["tenant_name"] == "ACME"


def test_redact_handles_nested_structures() -> None:
    result = redact(
        {
            "outer": {"access_token": "abc", "keep": 1},
            "items": [{"password": "p"}, {"safe": "value"}],
        }
    )

    assert result == {
        "outer": {"access_token": REDACTED, "keep": 1},
        "items": [{"password": REDACTED}, {"safe": "value"}],
    }


def test_console_formatter_appends_context() -> None:
    bind_log_context(request_id="req-9")

    output = ConsoleFormatter().format(_record())

    assert "request_id=req-9" in output
