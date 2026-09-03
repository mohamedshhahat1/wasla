"""What the registry concludes from Meta's answer.

Three things are worth pinning down here, and none of them needs a database.

Meta's vocabulary is larger than ours and grows without warning, so the mapping
has to fail closed: an unrecognised status must land somewhere unsendable rather
than be guessed into `approved`.

The variable count is what a campaign's parameters are checked against, so
miscounting it means either a refused campaign that was fine or an accepted one
that Meta will reject at send time.

And the refusal rule has an asymmetry that is easy to lose in a later edit: a
template nobody has synced is allowed, a template Meta has actually refused is
not.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
    count_placeholders,
)
from app.services.template_service import (
    META_CATEGORIES,
    META_STATUSES,
    refusal_reason_for,
)


def _template(**overrides: Any) -> WhatsAppTemplate:
    values = {
        "name": "order_update",
        "language": "ar_EG",
        "category": TemplateCategory.UTILITY,
        "status": TemplateStatus.APPROVED,
        "variable_count": 0,
    }
    values.update(overrides)
    return WhatsAppTemplate(**values)


# ------------------------------------------------------------------ variables


def test_a_template_with_no_variables_expects_none() -> None:
    assert count_placeholders("Your order has shipped.") == 0
    assert count_placeholders(None) == 0
    assert count_placeholders("") == 0


def test_positional_variables_are_counted() -> None:
    assert count_placeholders("Hello {{1}}, your order {{2}} has shipped.") == 2


def test_named_variables_are_counted() -> None:
    """Meta accepts named parameters as well as numbered ones."""
    assert count_placeholders("Hello {{name}}, see you on {{date}}.") == 2


def test_a_variable_used_twice_is_still_one_variable() -> None:
    """Meta is given one parameter for it, so counting occurrences would lie."""
    assert count_placeholders("{{1}}, we mean it, {{1}}.") == 1


def test_whitespace_inside_the_braces_does_not_hide_a_variable() -> None:
    assert count_placeholders("Hello {{ 1 }}.") == 1


def test_something_that_is_not_a_placeholder_is_not_counted() -> None:
    assert count_placeholders("Save {{{ or use { { 1 } }.") == 0


# --------------------------------------------------------------- the mapping


@pytest.mark.parametrize(
    ("meta_status", "expected"),
    [
        ("APPROVED", TemplateStatus.APPROVED),
        ("PENDING", TemplateStatus.PENDING),
        ("IN_APPEAL", TemplateStatus.PENDING),
        ("REJECTED", TemplateStatus.REJECTED),
        ("PAUSED", TemplateStatus.PAUSED),
        ("LIMIT_EXCEEDED", TemplateStatus.PAUSED),
        ("DISABLED", TemplateStatus.DISABLED),
        ("PENDING_DELETION", TemplateStatus.DISABLED),
        ("DELETED", TemplateStatus.DISABLED),
    ],
)
def test_metas_statuses_map_onto_ours(meta_status: str, expected: TemplateStatus) -> None:
    assert META_STATUSES[meta_status] is expected


def test_a_status_meta_invents_later_is_not_approved() -> None:
    """The whole reason `UNKNOWN` exists: an unrecognised state fails closed."""
    assert META_STATUSES.get("SOMETHING_NEW", TemplateStatus.UNKNOWN) is TemplateStatus.UNKNOWN
    assert _template(status=TemplateStatus.UNKNOWN).is_sendable is False


def test_metas_earlier_category_names_still_resolve() -> None:
    assert META_CATEGORIES["TRANSACTIONAL"] is TemplateCategory.UTILITY
    assert META_CATEGORIES["OTP"] is TemplateCategory.AUTHENTICATION


def test_only_approved_is_sendable() -> None:
    assert _template(status=TemplateStatus.APPROVED).is_sendable is True
    for refused in (
        TemplateStatus.PENDING,
        TemplateStatus.REJECTED,
        TemplateStatus.PAUSED,
        TemplateStatus.DISABLED,
        TemplateStatus.UNKNOWN,
    ):
        assert _template(status=refused).is_sendable is False


def test_only_marketing_templates_are_marketing() -> None:
    assert _template(category=TemplateCategory.MARKETING).is_marketing is True
    assert _template(category=TemplateCategory.UTILITY).is_marketing is False


# ------------------------------------------------------------- the refusal rule


def test_an_approved_template_draws_no_objection() -> None:
    assert refusal_reason_for(_template(status=TemplateStatus.APPROVED)) is None


def test_a_template_the_registry_has_never_seen_is_allowed_through() -> None:
    """A workspace that has not synced must not lose every follow-up it has."""
    assert refusal_reason_for(None) is None


def test_a_paused_template_is_refused_and_says_why() -> None:
    reason = refusal_reason_for(_template(status=TemplateStatus.PAUSED))

    assert reason is not None
    assert "paused" in reason


def test_a_rejected_template_is_refused() -> None:
    assert refusal_reason_for(_template(status=TemplateStatus.REJECTED)) is not None
