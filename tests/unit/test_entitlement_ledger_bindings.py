"""The entitlement ledger judges against the deployment's own clocks (ADR-131).

`scripts.omnichannel_invariants` restates two figures rather than importing the
worker and the settings it describes - an operator command should not start a
billing worker's imports to count rows. Restated figures drift, so they are
held equal here.
"""

from __future__ import annotations

from app.core.config import Settings
from app.workers.billing_worker import POLL_SECONDS
from scripts.omnichannel_invariants import DEFAULT_HOLD_TTL_SECONDS, SWEEP_SECONDS, Bindings


def test_the_sweep_window_is_the_billing_workers_pause() -> None:
    assert SWEEP_SECONDS == POLL_SECONDS


def test_the_hold_ttl_is_the_deployment_default() -> None:
    default = Settings.model_fields["ai_turn_hold_ttl_seconds"].default
    assert default == DEFAULT_HOLD_TTL_SECONDS


def test_a_hold_outlives_the_sweep_only_past_its_ttl_and_one_pass() -> None:
    values = Bindings(hold_ttl_seconds=900).values()
    assert values["hold_cutoff_seconds"] == 900 + POLL_SECONDS
    assert values["sweep_seconds"] == POLL_SECONDS
