"""Configuring when an agent stops replying and hands over.

The distinction that needs proving is the one every other field on `AgentUpdate`
avoids: null is a setting here, not an absence. Sending it switches automatic
handoff off; omitting it must leave whatever was configured alone.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.db.models.agent import DEFAULT_ESCALATION_SENTIMENT
from app.db.models.sentiment import SentimentLabel
from app.schemas.agent import AgentCreate, AgentUpdate


def test_a_new_agent_escalates_on_anger_unless_told_otherwise() -> None:
    payload = AgentCreate(name="Sales", system_prompt="Be helpful.")

    assert payload.escalation_sentiment is DEFAULT_ESCALATION_SENTIMENT
    assert payload.escalation_sentiment is SentimentLabel.ANGRY


def test_a_new_agent_can_be_created_with_handoff_switched_off() -> None:
    payload = AgentCreate(
        name="Sales",
        system_prompt="Be helpful.",
        escalation_sentiment=None,
    )

    assert payload.escalation_sentiment is None


def test_a_new_agent_can_escalate_earlier() -> None:
    payload = AgentCreate(
        name="Support",
        system_prompt="Be helpful.",
        escalation_sentiment=SentimentLabel.NEGATIVE,
    )

    assert payload.escalation_sentiment is SentimentLabel.NEGATIVE


def test_an_update_that_omits_the_threshold_leaves_it_alone() -> None:
    payload = AgentUpdate(name="Renamed")

    assert payload.was_sent("escalation_sentiment") is False


def test_an_update_that_sends_null_means_switch_it_off() -> None:
    """The case a plain `is None` check would silently discard."""
    payload = AgentUpdate.model_validate({"escalation_sentiment": None})

    assert payload.was_sent("escalation_sentiment") is True
    assert payload.escalation_sentiment is None


def test_an_update_that_sends_a_threshold_reports_it() -> None:
    payload = AgentUpdate.model_validate({"escalation_sentiment": "negative"})

    assert payload.was_sent("escalation_sentiment") is True
    assert payload.escalation_sentiment is SentimentLabel.NEGATIVE


def test_a_threshold_outside_the_enum_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentUpdate.model_validate({"escalation_sentiment": "furious"})
