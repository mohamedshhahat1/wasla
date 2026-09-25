"""The pure rules behind custom plans and top-ups (ADR-113), without a database."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core import telemetry
from app.db.models.billing import TOPUP_LIMITS, LimitKey, Plan, PlanScope
from app.db.models.topup import (
    ACTIVE_TOPUP_STATUSES,
    TOPUP_TRANSITIONS,
    TopupEntitlement,
    TopupStatus,
    topup_may_move,
)
from app.schemas.custom_plan import CUSTOM_PLAN_KEYS, CustomPlanCreate
from app.schemas.topup import TopupGrantCreate, TopupProductCreate
from app.services.entitlement_service import Entitlement

PRODUCT = {
    "code": "ai-10k",
    "name": "AI Turns +10K",
    "entitlement_key": "period_ai_turns",
    "quantity": 10_000,
    "price": "200.00",
    "currency": "EGP",
    "reason": "Catalogue.",
}


def test_the_topup_keys_are_exactly_the_seven() -> None:
    assert {member.value for member in TopupEntitlement} == {key.value for key in TOPUP_LIMITS}
    assert set(CUSTOM_PLAN_KEYS) == set(TOPUP_LIMITS)
    assert LimitKey.AGENTS not in TOPUP_LIMITS
    assert LimitKey.OWNED_WORKSPACES not in TOPUP_LIMITS


def test_a_granted_topup_can_only_expire_or_go_to_review() -> None:
    assert TOPUP_TRANSITIONS[TopupStatus.GRANTED] == {
        TopupStatus.EXPIRED,
        TopupStatus.REFUND_REVIEW,
    }
    assert not topup_may_move(TopupStatus.GRANTED, TopupStatus.GRANTED)
    assert not topup_may_move(TopupStatus.EXPIRED, TopupStatus.GRANTED)
    assert not topup_may_move(TopupStatus.CANCELLED, TopupStatus.GRANTED)
    assert topup_may_move(TopupStatus.PENDING, TopupStatus.GRANTED)
    assert topup_may_move(TopupStatus.REFUND_REVIEW, TopupStatus.CANCELLED)
    # Only a live grant, or one under review, counts toward a limit.
    assert {TopupStatus.GRANTED, TopupStatus.REFUND_REVIEW} == set(ACTIVE_TOPUP_STATUSES)


@pytest.mark.parametrize(
    "overrides",
    [
        {"entitlement_key": "agents"},
        {"entitlement_key": "nonsense"},
        {"quantity": 0},
        {"quantity": -1},
        {"price": "-0.01"},
        {"currency": "USD"},
        {"scope": "tenant"},
        {"scope": "global", "tenant_id": str(uuid.uuid4())},
        {"surprise": 1},
    ],
)
def test_an_invalid_product_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TopupProductCreate.model_validate({**PRODUCT, **overrides})


def test_a_grant_is_bounded_and_needs_a_reason() -> None:
    base = {
        "entitlement_key": "team_members",
        "quantity": 1,
        "reason": "Help.",
        "expected_subscription_revision": 1,
    }
    assert TopupGrantCreate.model_validate(base).valid_until == "current_period_end"
    for bad in ({"quantity": 0}, {"reason": "no"}, {"valid_until": "forever"}):
        with pytest.raises(ValidationError):
            TopupGrantCreate.model_validate({**base, **bad})


def test_every_custom_plan_limit_must_be_stated() -> None:
    body = {
        "code": "abc",
        "name": "ABC",
        "price": "1500",
        "currency": "EGP",
        "billing_interval": "monthly",
        "reason": "Terms.",
        **{key.value: 1 for key in CUSTOM_PLAN_KEYS},
    }
    assert CustomPlanCreate.model_validate(body).limits()["team_members"] == 1
    for key in CUSTOM_PLAN_KEYS:
        missing = {name: value for name, value in body.items() if name != key.value}
        with pytest.raises(ValidationError):
            CustomPlanCreate.model_validate(missing)


def test_the_effective_limit_reports_over_limit_without_a_negative_remainder() -> None:
    over = Entitlement(key=LimitKey.WHATSAPP_NUMBERS, limit=3, used=5, base_limit=3)
    assert (over.over_limit, over.remaining) == (True, 0)
    at = Entitlement(key=LimitKey.WHATSAPP_NUMBERS, limit=3, used=3, base_limit=1, topup_limit=2)
    assert (at.over_limit, at.remaining) == (False, 0)
    unlimited = Entitlement(key=LimitKey.PERIOD_AI_TURNS, limit=None, used=10**9)
    assert (unlimited.over_limit, unlimited.remaining) == (False, None)


def _scope(plan: Plan) -> PlanScope:
    return plan.scope


def test_scope_follows_visibility_until_a_plan_is_custom() -> None:
    private = Plan(code="ent", name="Ent", is_public=False)
    assert _scope(private) is PlanScope.PRIVATE
    private.is_public = True
    assert _scope(private) is PlanScope.PUBLIC
    tenant = uuid.uuid4()
    custom = Plan(code="c", name="C", scope=PlanScope.TENANT, tenant_id=tenant, is_public=False)
    custom.is_public = True  # refused later by the scope_visibility constraint
    assert custom.scope is PlanScope.TENANT
    assert custom.available_to(tenant)
    assert not custom.available_to(uuid.uuid4())
    assert private.available_to(uuid.uuid4())


def test_topup_metric_labels_are_closed() -> None:
    clamp = telemetry._closed_labels
    assert clamp(
        "wasla_billing_topup_grant_total",
        {"entitlement": "period_ai_turns", "source": "purchase", "outcome": "granted"},
    ) == {"entitlement": "period_ai_turns", "source": "purchase", "outcome": "granted"}
    assert clamp(
        "wasla_billing_topup_grant_total",
        {"entitlement": str(uuid.uuid4()), "source": "a@b.c", "outcome": "12.00"},
    ) == {"entitlement": "other", "source": "other", "outcome": "other"}
    for metric, labels in telemetry.BILLING_LABEL_DOMAINS.items():
        assert telemetry.REDIS_COUNTERS[metric][1] == tuple(labels), metric
    for kind in (
        "topup_paid_but_not_granted",
        "topup_duplicate_payment",
        "custom_plan_scope_mismatch",
    ):
        assert kind in telemetry.BILLING_OUTCOMES["wasla_billing_incidents_total"]
