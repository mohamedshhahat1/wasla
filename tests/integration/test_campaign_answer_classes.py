"""Every Meta answer class, handled as Meta documents it (CMP-06, CMP-07, CMP-18, CMP-19).

Ported from the audit's P06 matrix: three recipients, several sweeps, the real
`CampaignWorker`, `MessagingService`, `WhatsAppClient` and Meta error
classifier, with Meta replaced at the transport. Each row's target is section
22.4 of the remediation brief; the audit's own column is in the comment.

Error envelopes and codes follow Meta's "Error codes" page and the Cloud API
send response (developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes,
read 2026-10-13; audit section 36, EXT-CMP-02..05).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason
from app.db.models.consent import ContactChannelConsent
from app.db.models.whatsapp_template import TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.integrations.whatsapp.client import WhatsAppClient
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    FakeMeta,
    accepted,
    compose,
    erase,
    ledger,
    meta_error,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

Answer = Callable[[dict[str, Any], int], Awaitable[httpx.Response]]


def always(response: Callable[[dict[str, Any], int], httpx.Response]) -> Answer:
    async def answer(payload: dict[str, Any], n: int) -> httpx.Response:
        return response(payload, n)

    return answer


def raising(error: type[httpx.TransportError]) -> Answer:
    async def answer(payload: dict[str, Any], n: int) -> httpx.Response:
        raise error("harness", request=httpx.Request("POST", "https://graph.facebook.com"))

    return answer


@pytest_asyncio.fixture
async def world(prepared_database: str) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="answers")
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def _sweeps(each: CampaignWorker, count: int) -> None:
    start = datetime.now(UTC)
    for index in range(count):
        await each.run_once(now=start + timedelta(seconds=400 * index))


async def _campaign(database: Database, campaign_id: Any) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real waiting between the client's retries; how long it waits is the client suite's."""

    async def no_wait(
        self: WhatsAppClient, attempt: int, *, retry_after: float | None = None
    ) -> None:
        return None

    monkeypatch.setattr(WhatsAppClient, "_backoff", no_wait)


# (answer, requests, recipients, campaign status, status_reason)
MATRIX: dict[str, tuple[Answer, int, dict[str, int], CampaignStatus, str | None]] = {
    # audit: 3, sent x3, completed
    "200_id": (always(accepted), 3, {"sent/attempts=0": 3}, CampaignStatus.COMPLETED, None),
    # audit: 3, failed/1 x3 - uncertain, never resent
    "200_no_messages": (
        always(lambda p, n: httpx.Response(200, json={"messages": []})),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "200_malformed": (
        always(lambda p, n: httpx.Response(200, content=b"{not json")),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "500": (
        always(lambda p, n: meta_error(500, None)),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "503": (
        always(lambda p, n: meta_error(503, None)),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "read_timeout": (
        raising(httpx.ReadTimeout),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "connection_reset": (
        raising(httpx.RemoteProtocolError),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    # audit: 1 request, the un-asked spent an attempt each (CMP-19)
    "400_132001": (
        always(lambda p, n: meta_error(400, 132001)),
        1,
        {"pending/attempts=1": 1, "pending/attempts=0": 2},
        CampaignStatus.FAILED,
        CampaignStopReason.TEMPLATE_WITHDRAWN,
    ),
    # audit: 9 requests, failed/3 x3, completed, no reason (CMP-05)
    "400_132000": (
        always(lambda p, n: meta_error(400, 132000)),
        1,
        {"pending/attempts=1": 1, "pending/attempts=0": 2},
        CampaignStatus.FAILED,
        CampaignStopReason.TEMPLATE_PARAMETERS_REFUSED,
    ),
    # audit: 9 requests - retried within minutes where Meta says wait 24 h (CMP-06)
    "400_131049": (
        always(lambda p, n: meta_error(400, 131049)),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    "400_131026": (
        always(lambda p, n: meta_error(400, 131026)),
        3,
        {"failed/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    # audit: 3, skipped x3, consent x3
    "400_131050": (
        always(lambda p, n: meta_error(400, 131050)),
        3,
        {"skipped/attempts=1": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
    # audit: 1 request, FAILED
    "401_no_code": (
        always(lambda p, n: meta_error(401, None)),
        1,
        {"pending/attempts=0": 3},
        CampaignStatus.FAILED,
        CampaignStopReason.CREDENTIAL_REFUSED,
    ),
    "400_190": (
        always(lambda p, n: meta_error(400, 190)),
        1,
        {"pending/attempts=0": 3},
        CampaignStatus.FAILED,
        CampaignStopReason.CREDENTIAL_REFUSED,
    ),
    "400_368": (
        always(lambda p, n: meta_error(400, 368)),
        1,
        {"pending/attempts=0": 3},
        CampaignStatus.FAILED,
        CampaignStopReason.CONNECTION_REFUSED,
    ),
    # A bare 403 is per message by the classifier (OMNI-035, documented):
    # retried until the attempts run out.
    "403_no_code": (
        always(lambda p, n: meta_error(403, None)),
        9,
        {"failed/attempts=3": 3},
        CampaignStatus.COMPLETED,
        None,
    ),
}


@pytest.mark.parametrize("name", list(MATRIX))
async def test_each_answer_class(
    world: tuple[FakeMeta, CampaignWorker, Database], name: str
) -> None:
    answer, requests, recipients, status, reason = MATRIX[name]
    meta, each, database = world
    meta.behaviour = answer
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)

    await _sweeps(each, 5)

    assert len(meta.requests) == requests, meta.per_recipient()
    if name != "403_no_code":
        assert meta.received_twice() == 0
    row = await ledger(database, campaign_id)
    assert dict(row.recipients) == recipients
    assert row.status is status
    assert (await _campaign(database, campaign_id)).status_reason == reason
    async with database.session_factory() as session:
        template = await session.get(WhatsAppTemplate, workspace.template_id)
        assert template is not None
        assert template.status is (
            TemplateStatus.PAUSED if name == "400_132001" else TemplateStatus.APPROVED
        )
        if name == "400_131050":
            consents = await session.scalar(
                select(func.count())
                .select_from(ContactChannelConsent)
                .where(ContactChannelConsent.tenant_id == workspace.tenant_id)
            )
            assert consents == 3


@pytest.mark.parametrize(
    "answer",
    [
        always(lambda p, n: meta_error(429, None, {"Retry-After": "1"})),
        always(lambda p, n: meta_error(400, 130429)),
    ],
    ids=["429_retry_after", "400_130429"],
)
async def test_a_throttle_waits_without_spending_attempts(
    world: tuple[FakeMeta, CampaignWorker, Database], answer: Answer
) -> None:
    """429 / 130429: the client's retries, then the campaign waits; nobody's attempts spent."""
    meta, each, database = world
    meta.behaviour = answer
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)

    await _sweeps(each, 3)

    # Three tries in the client per sweep, all on the first person.
    assert len(meta.requests) == 9
    row = await ledger(database, campaign_id)
    assert dict(row.recipients) == {"pending/attempts=0": 3}
    assert row.status is CampaignStatus.RUNNING


async def test_a_pair_limit_defers_that_person_and_the_rest_go_on(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """131056 (CMP-18): audit - the whole campaign waited on the same first person every window."""
    meta, each, database = world

    async def first_once(payload: dict[str, Any], n: int) -> httpx.Response:
        return meta_error(400, 131056) if n == 1 else accepted(payload, n)

    meta.behaviour = first_once
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)

    start = datetime.now(UTC)
    await each.run_once(now=start)
    after_first = await ledger(database, campaign_id)
    # The other two were sent in the same batch; the first waits, attempts kept.
    assert dict(after_first.recipients) == {"pending/attempts=0": 1, "sent/attempts=0": 2}
    assert after_first.status is CampaignStatus.RUNNING

    # Inside the deferral, the person is not asked again (M-C23).
    await each.run_once(now=start + timedelta(seconds=30))
    assert len(meta.requests) == 3

    await each.run_once(now=start + timedelta(seconds=400))

    assert len(meta.requests) == 4
    assert meta.received_twice() == 1  # the deferred person, asked once more
    row = await ledger(database, campaign_id)
    assert dict(row.recipients) == {"sent/attempts=0": 3}
    assert row.status is CampaignStatus.COMPLETED


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_an_unreachable_meta_costs_time_not_the_audience(
    world: tuple[FakeMeta, CampaignWorker, Database], error: type[httpx.TransportError]
) -> None:
    """CMP-07: audit - 27 transport attempts, every recipient failed after three.

    Nothing reached Meta: the client's three connection attempts per sweep are
    the only traffic, nobody's attempts are spent, and once the outage is older
    than the bound the campaign is paused for a person to look at.
    """
    meta, each, database = world
    meta.behaviour = raising(error)
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)

    start = datetime.now(UTC)
    await each.run_once(now=start)
    await each.run_once(now=start + timedelta(minutes=2))

    row = await ledger(database, campaign_id)
    assert dict(row.recipients) == {"pending/attempts=0": 3}
    assert row.status is CampaignStatus.RUNNING
    assert len(meta.requests) == 6  # three connection attempts per sweep, one person

    await each.run_once(now=start + timedelta(hours=7))

    paused = await _campaign(database, campaign_id)
    assert paused.status is CampaignStatus.PAUSED
    assert paused.status_reason == CampaignStopReason.PROVIDER_UNREACHABLE
    assert dict((await ledger(database, campaign_id)).recipients) == {"pending/attempts=0": 3}
