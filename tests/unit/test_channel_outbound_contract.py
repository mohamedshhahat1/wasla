"""OutboundAdapterContract: the address is the participant, and outcomes are the core's.

Driven through the WhatsApp adapter's own sender (`WhatsAppSender`) with Meta
answered by an `httpx` `MockTransport`, so what is pinned is the seam the
shared delivery protocol calls (OMNI-004, OMNI-020, ADR-093):

- **The participant identity is the address**, exactly one form of it: a phone
  is sent `to`, a business-scoped id is sent as `recipient` - never both,
  because Meta lets `to` win and the conversation's pin would become a
  suggestion. An identity WhatsApp cannot address is refused before any request.
- **Every provider outcome is one of the core's types** - `ProviderReceipt`,
  `ProviderAuthError`, `SendNotAttemptedError`, `UncertainDeliveryError`,
  `RateLimitedError` - and the WhatsApp module's names for them are the same
  classes, so nothing written against either import can disagree.
- **An uncertain send is never retried by the adapter**: one request, then the
  uncertainty is handed up for ADR-093 to keep.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from app.channels import outcomes
from app.channels.adapter import (
    IdentityNotAddressableError,
    Recipient,
    TemplateContent,
    TextContent,
)
from app.core.exceptions import RateLimitedError
from app.db.models.channel import Channel, ContactIdentity, IdentityKind, IdentityScope
from app.integrations.whatsapp import client as whatsapp_client
from app.integrations.whatsapp.adapter import WhatsAppAdapter, WhatsAppSender
from app.integrations.whatsapp.client import WhatsAppClient

PHONE = "201000000801"
BSUID = "EG.0outbound0contract"
# A fixture value, not a credential.
TOKEN = "outbound-contract-token-fixture"


class Graph:
    """Meta's messages endpoint, answering each request from a script."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self._answers = list(answers)
        self.bodies: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _no_wait(_: float) -> None:
    return None


def _sender(graph: Graph) -> WhatsAppSender:
    client = WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(graph.handle)),
        access_token=TOKEN,
        sleep=_no_wait,
        jitter=lambda: 0.0,
    )
    return WhatsAppSender(client=client, phone_number_id="PN-contract", uploaded=[])


def _accepted(message_id: str = "wamid.contract") -> httpx.Response:
    return httpx.Response(
        200, json={"messaging_product": "whatsapp", "messages": [{"id": message_id}]}
    )


def _meta_error(status: int, code: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "refused", "code": code}})


def _identity(kind: IdentityKind, value: str, channel: Channel = Channel.WHATSAPP) -> Any:
    identity = ContactIdentity(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel=channel,
        kind=kind,
        scope=IdentityScope.WORKSPACE,
        scope_ref="",
        value=value,
    )
    return identity


# ------------------------------------------------------------- addressing


async def test_a_phone_participant_is_sent_to_and_nothing_else() -> None:
    graph = Graph(_accepted())
    recipient = WhatsAppAdapter().address(_identity(IdentityKind.PHONE, PHONE))

    receipt = await _sender(graph).send(recipient, TextContent(body="hello"))

    (body,) = graph.bodies
    assert body["to"] == PHONE
    assert "recipient" not in body
    assert receipt.message_id == "wamid.contract"


async def test_a_business_scoped_participant_is_sent_as_recipient_and_never_to() -> None:
    graph = Graph(_accepted())
    recipient = WhatsAppAdapter().address(_identity(IdentityKind.BSUID, BSUID))

    await _sender(graph).send(recipient, TemplateContent(name="welcome", language="ar"))

    (body,) = graph.bodies
    assert body["recipient"] == BSUID
    assert "to" not in body
    assert body["recipient_type"] == "individual"


@pytest.mark.parametrize("kind", [IdentityKind.PSID, IdentityKind.IGSID])
def test_an_identity_whatsapp_cannot_address_is_refused(kind: IdentityKind) -> None:
    with pytest.raises(IdentityNotAddressableError):
        WhatsAppAdapter().address(_identity(kind, "someone"))


@pytest.mark.parametrize("channel", [Channel.INSTAGRAM, Channel.MESSENGER])
def test_another_channels_identity_is_refused_whatever_its_kind(channel: Channel) -> None:
    with pytest.raises(IdentityNotAddressableError):
        WhatsAppAdapter().address(_identity(IdentityKind.PHONE, PHONE, channel=channel))


async def test_a_recipient_of_an_unknown_kind_reaches_no_provider() -> None:
    graph = Graph(_accepted())
    stray = Recipient(identity_id=uuid.uuid4(), kind="psid", value="someone")

    with pytest.raises(IdentityNotAddressableError):
        await _sender(graph).send(stray, TextContent(body="hello"))
    assert graph.bodies == []


# --------------------------------------------------------------- outcomes


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (_meta_error(401, 0), outcomes.ProviderAuthError),
        (_meta_error(400, 190), outcomes.ProviderAuthError),
        (_meta_error(400, 131026), outcomes.SendNotAttemptedError),
        (_meta_error(403, 10), outcomes.SendNotAttemptedError),
        (httpx.Response(500), outcomes.UncertainDeliveryError),
        (
            httpx.Response(200, json={"messaging_product": "whatsapp"}),
            outcomes.UncertainDeliveryError,
        ),
        (httpx.ReadTimeout("slow"), outcomes.UncertainDeliveryError),
        (httpx.ConnectError("down"), outcomes.SendNotAttemptedError),
        (httpx.Response(429), RateLimitedError),
    ],
)
async def test_every_provider_answer_is_one_of_the_cores_outcomes(
    answer: httpx.Response | Exception, outcome: type[Exception]
) -> None:
    graph = Graph(answer)
    recipient = WhatsAppAdapter().address(_identity(IdentityKind.PHONE, PHONE))

    with pytest.raises(outcome) as raised:
        await _sender(graph).send(recipient, TextContent(body="hello"))
    if outcome is outcomes.SendNotAttemptedError:
        # Refused before reading, not a credential problem.
        assert not isinstance(raised.value, outcomes.ProviderAuthError)


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(500), httpx.ReadTimeout("slow"), httpx.Response(200, json={})],
)
async def test_an_uncertain_send_is_asked_exactly_once(answer: httpx.Response | Exception) -> None:
    """A second request after an unknown outcome is a second message on a phone."""
    graph = Graph(answer)
    recipient = WhatsAppAdapter().address(_identity(IdentityKind.PHONE, PHONE))

    with pytest.raises(outcomes.UncertainDeliveryError):
        await _sender(graph).send(recipient, TextContent(body="hello"))
    assert len(graph.bodies) == 1


async def test_a_credential_refusal_is_its_own_outcome_not_a_recipients() -> None:
    """Distinct, so a sweep stops once instead of failing every recipient (MSG-18)."""
    graph = Graph(_meta_error(401, 190))
    recipient = WhatsAppAdapter().address(_identity(IdentityKind.PHONE, PHONE))

    with pytest.raises(outcomes.ProviderAuthError) as refused:
        await _sender(graph).send(recipient, TextContent(body="hello"))
    assert isinstance(refused.value, outcomes.SendNotAttemptedError)
    assert TOKEN not in str(refused.value)


def test_the_whatsapp_names_are_the_neutral_classes() -> None:
    """Re-exported, not copied: an `except` against either path catches both."""
    assert whatsapp_client.ProviderAuthError is outcomes.ProviderAuthError
    assert whatsapp_client.SendNotAttemptedError is outcomes.SendNotAttemptedError
    assert whatsapp_client.UncertainDeliveryError is outcomes.UncertainDeliveryError
    assert issubclass(whatsapp_client.TemplateWithdrawnError, outcomes.SendNotAttemptedError)
