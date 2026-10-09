"""When an automated reply must disclose, and how it fits the channel (OMNI-041)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.agents.disclosure import (
    DEFAULT_DISCLOSURES,
    SEPARATOR,
    compose,
    disclosure_due,
    disclosure_for,
)
from app.channels.policy import TextUnit, text_length
from app.integrations.whatsapp.policy import WHATSAPP_CAPABILITIES
from app.schemas.workspace import WorkspaceUpdateRequest
from tests.channel_fakes import TAGGED_CAPABILITIES

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
GAP = timedelta(hours=24)


@pytest.mark.parametrize(
    ("disclosed", "resumed", "due"),
    [
        (None, None, True),
        (NOW - timedelta(hours=1), None, False),
        (NOW - timedelta(hours=25), None, True),
        (NOW - timedelta(hours=1), NOW - timedelta(minutes=5), True),
        (NOW - timedelta(hours=1), NOW - timedelta(hours=2), False),
    ],
    ids=["never", "recently", "after_the_gap", "after_a_hand_back", "hand_back_before_it"],
)
def test_when_a_disclosure_is_due(
    disclosed: datetime | None, resumed: datetime | None, due: bool
) -> None:
    assert disclosure_due(disclosed_at=disclosed, resumed_at=resumed, now=NOW, gap=GAP) is due


def test_the_language_follows_the_reply_and_the_workspace_may_reword_it() -> None:
    assert disclosure_for("We open at noon.") == DEFAULT_DISCLOSURES["en"]
    assert disclosure_for("نفتح ظهرا") == DEFAULT_DISCLOSURES["ar"]
    assert disclosure_for("We open at noon.", {"en": "Bot here."}) == "Bot here."
    # A wording for the other language only: Wasla's for this one.
    assert disclosure_for("We open at noon.", {"ar": "بوت"}) == DEFAULT_DISCLOSURES["en"]


@pytest.mark.parametrize("reply", ["short", "x" * 5_000, "مرحبا بك " * 400, "😀" * 600])
def test_the_disclosure_and_the_reply_never_pass_the_channels_limit(reply: str) -> None:
    for capabilities in (TAGGED_CAPABILITIES, WHATSAPP_CAPABILITIES):
        disclosure = disclosure_for(reply)
        sent = compose(reply, disclosure, capabilities)
        assert sent.startswith(disclosure + SEPARATOR)
        assert text_length(sent, capabilities.text_unit) <= capabilities.text_limit


def test_a_tight_budget_still_leaves_the_reply_room() -> None:
    tight = replace(TAGGED_CAPABILITIES, text_limit=300, reply_budget=250)
    sent = compose("A" * 1_000, "Automated.", tight)

    assert text_length(sent, TextUnit.UTF8_BYTES) <= 300
    assert len(sent) > len("Automated." + SEPARATOR)


@pytest.mark.parametrize(
    "wording",
    [{"en": ""}, {"fr": "Bonjour"}, {"en": "x" * 201}],
    ids=["empty", "unknown_language", "too_long"],
)
def test_a_workspace_cannot_blank_out_the_obligation(wording: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        WorkspaceUpdateRequest.model_validate({"automation_disclosure": wording})


def test_a_workspace_may_set_its_wording_or_restore_wasla_s() -> None:
    both = WorkspaceUpdateRequest.model_validate(
        {"automation_disclosure": {"en": "Bot here.", "ar": "بوت"}}
    ).automation_disclosure
    restored = WorkspaceUpdateRequest.model_validate({"automation_disclosure": {}})
    assert both is not None
    assert both.model_dump(exclude_none=True) == {"en": "Bot here.", "ar": "بوت"}
    assert restored.automation_disclosure is not None
    assert restored.automation_disclosure.model_dump(exclude_none=True) == {}
