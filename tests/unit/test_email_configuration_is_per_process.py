"""Each half of the Resend configuration is demanded by one process only.

Two credentials, two jobs, two containers:

- `RESEND_API_KEY` can send mail as the platform's domain. Only the worker
  sends, so `build_email_provider` asks for it and only the worker carries it.
- `RESEND_WEBHOOK_SECRET` decides which delivery reports are believed. Only the
  API serves that webhook, so `require_delivery_verification` asks for it and
  only the API carries it.

The second half of that symmetry did not exist before this file. The webhook
secret was required by `Settings`, which every process builds - so the worker
had to be given a secret it never reads in order to boot at all, and the
production Compose file said as much in a comment (ADR-063).

**These tests are about who fails, not about whether something fails.** A test
that only asserted "a production deployment without the webhook secret is
refused" passed before the change and passes after it, and would not have
noticed the difference that matters.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.integrations.email import build_email_provider, require_delivery_verification
from app.main import create_app

VALID_SECRET = "a" * 64


def _production(**overrides: object) -> Settings:
    """A production deployment with email on, and one thing at a time missing."""
    fields: dict[str, object] = {
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
        "resend_api_key": "re_abc",
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


# ------------------------------------------- the worker owns the sending key


def test_the_sending_process_refuses_to_start_without_the_api_key() -> None:
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        build_email_provider(_production(resend_api_key=None))


def test_the_sending_process_does_not_ask_for_the_webhook_secret() -> None:
    """It never verifies a delivery event, so it has no use for one."""
    provider = build_email_provider(_production(resend_webhook_secret=None))

    assert provider is not None


# ------------------------------------------ the API owns the webhook secret


def test_the_api_refuses_to_start_without_the_webhook_secret() -> None:
    """The guarantee `Settings` used to make, made by the process that needs it.

    Without it the delivery-event endpoint answers 503 to every call, so
    bounces and complaints are never recorded and the platform keeps writing to
    dead mailboxes until the sending domain is the thing that fails.
    """
    with pytest.raises(ValueError, match="RESEND_WEBHOOK_SECRET"):
        create_app(_production(resend_webhook_secret=None))


def test_the_api_does_not_ask_for_the_sending_key() -> None:
    """The point of the split: building the API needs no credential that sends."""
    app = create_app(_production(resend_api_key=None))

    assert app.state.settings.resend_api_key is None


# ------------------------------------------------------- neither over-reaches


def test_a_deployment_that_sends_nothing_is_asked_for_neither() -> None:
    """Email off is a complete configuration, not a half-finished one."""
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret=VALID_SECRET,
        docs_enabled=False,
        meta_app_secret="an-app-secret",
        email_enabled=False,
    )

    require_delivery_verification(settings)
    assert create_app(settings) is not None


def test_a_local_deployment_needs_no_webhook_secret() -> None:
    """Nothing local has a delivery event to verify; the fake reports none."""
    require_delivery_verification(
        _production(environment="local", email_provider="fake", resend_webhook_secret=None)
    )


def test_the_worker_entrypoint_does_not_build_the_application() -> None:
    """What makes the split hold at runtime, rather than only on paper.

    `require_delivery_verification` runs in `create_app`. The worker is safe
    from it because `python -m app.workers.runner` never constructs the
    application - and if some module under `app.workers` ever imported
    `app.main`, the worker container would start demanding a secret it is
    deliberately no longer given, and would fail to boot with email enabled.

    Checked in a subprocess because the rest of this file imports `app.main`
    itself, so an in-process `sys.modules` check would already be poisoned.
    """
    import subprocess
    import sys

    # S603: the whole command is a literal and `sys.executable` is this very
    # interpreter. Suppressed here rather than for `tests/**`, so the rule keeps
    # watching every other call site.
    probe = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; import app.workers.runner; "
            "sys.exit(1 if 'app.main' in sys.modules else 0)",
        ],
        capture_output=True,
    )

    assert probe.returncode == 0, (
        "app.workers.runner imports app.main, so the worker would run the "
        f"API's startup checks: {probe.stderr.decode(errors='replace')}"
    )
