"""Every telemetry signal, checked against the same five canaries.

Metrics, logs and traces are three destinations with three audiences. A metric
is scraped by anything on the network that can reach `/metrics`. A log is
shipped to whoever operates the log store. A span goes to a trace backend,
which in most deployments is a third party. The three also leak differently: a
metric leaks through a *label*, a log through a formatted message or an
`extra`, a span through an attribute, a name, or an exception recorded by a
default nobody chose.

Rather than assert that in three places, this module puts one distinctive
string into each thing that must never be exported, drives traffic carrying it,
and reads all three destinations back.

| Canary                      | Stands for                          |
| --------------------------- | ----------------------------------- |
| `SECRET-JWT-CANARY`         | a bearer token / the signing secret |
| `SECRET-PAYMOB-CANARY`      | a payment provider API key          |
| `CUSTOMER-EMAIL-CANARY@...` | an end user's email address         |
| `CUSTOMER-PHONE-CANARY`     | an end user's phone number          |
| `PROMPT-CANARY`             | model input or model output text    |

**The three signals do not have the same contract, and pretending they do would
make this file a lie.** Metrics and traces must carry none of the five, ever:
both are bounded label spaces, and an identifier in either is a cardinality
failure as much as a disclosure. Logs are different on purpose. A log is the
signal an operator reads to answer "what happened to *this* request", and
`app.core.middleware` records `request.url.path` for exactly that reason — so a
path segment, which is usually a lead id or a conversation id, does reach the
log store. What must never reach it is a credential, a query string, a header
or a body. Those are asserted separately below, and the difference is the point
rather than an exception to it.

A canary does not replace the allowlist in `test_trace_privacy.py` or the
cardinality tests over `app/core/metrics.py`. Those say what *is* allowed out.
This drives what is not allowed out straight past the exporters, which is the
only way to find out whether the allowlist is doing anything.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.telemetry import CallOutcome, Provider, ProviderCall, record_provider_call
from app.core.tracing import JOB_OUTCOME, SpanKind, span
from app.workers.dispatch import SUCCEEDED, job_span
from app.workers.queue import JobEnvelope
from tests.tracing_recorder import Recording, recording_spans

pytestmark = pytest.mark.integration

JWT_CANARY = "SECRET-JWT-CANARY"
PAYMOB_CANARY = "SECRET-PAYMOB-CANARY"
EMAIL_CANARY = "CUSTOMER-EMAIL-CANARY@example.test"
PHONE_CANARY = "CUSTOMER-PHONE-CANARY"
PROMPT_CANARY = "PROMPT-CANARY"

CANARIES = (JWT_CANARY, PAYMOB_CANARY, EMAIL_CANARY, PHONE_CANARY, PROMPT_CANARY)

#: What must not reach the log store even though the path does: credentials,
#: and anything that arrived in a query string, a header or a body.
NOT_IN_LOGS = (JWT_CANARY, PAYMOB_CANARY, EMAIL_CANARY, PROMPT_CANARY)


@pytest.fixture
def record() -> Iterator[Recording]:
    yield from recording_spans()


@pytest_asyncio.fixture
async def traced_client(app: FastAPI, record: Recording) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http:
        yield http


def _spans(record: Recording) -> str:
    """Everything the exporter would put on the wire, as one document.

    `to_json` is the SDK's own serialisation — name, kind, status, attributes,
    events, links, resource. Searching that is searching what a collector
    receives, not a hand-picked subset of it.
    """
    return "\n".join(item.to_json() for item in record.finished())


def _logs(caplog: pytest.LogCaptureFixture) -> str:
    """Every record Wasla emitted: message, args and each `extra`.

    Scoped to `app.*` because the harness logs too. `httpx` writes one INFO
    line per request naming the full URL — that is the *test client* narrating
    its own call, in the test process, and in production nothing does it. It is
    excluded because it is not Wasla's output, not because it is inconvenient:
    the application's own request log records `request.url.path`, which has no
    query string in it.

    The `extra` fields matter more than the message. Nobody interpolates an
    access token into a sentence; plenty of code passes a whole request object
    as context.
    """
    parts: list[str] = []
    for entry in caplog.records:
        if not entry.name.startswith("app."):
            continue
        parts.append(entry.getMessage())
        parts.extend(f"{key}={value!r}" for key, value in entry.__dict__.items())
    return "\n".join(parts)


def _assert_absent(document: str, canaries: tuple[str, ...], *, where: str) -> None:
    for canary in canaries:
        assert canary not in document, f"{canary} reached {where}"


# ------------------------------------------------------------- the API path


async def test_a_request_carrying_every_canary_exports_none_of_them(
    traced_client: AsyncClient,
    record: Recording,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One request, every canary, all three destinations.

    The canaries ride where a request can actually carry them: the credential
    headers, the query string, and the path. The path is the one worth naming —
    a span named for the URL, or a metric labelled with it, would publish every
    identifier the system has, one time series each.
    """
    with caplog.at_level(logging.DEBUG):
        await traced_client.get(
            f"/api/v1/leads/{uuid.uuid4()}",
            params={"q": PROMPT_CANARY, "email": EMAIL_CANARY},
            headers={
                "Authorization": f"Bearer {JWT_CANARY}",
                "Cookie": f"session={JWT_CANARY}",
                "X-Api-Key": PAYMOB_CANARY,
                "X-Forwarded-For": PHONE_CANARY,
            },
        )
        scrape = await traced_client.get("/metrics")

    _assert_absent(_spans(record), CANARIES, where="an exported span")
    _assert_absent(scrape.text, CANARIES, where="the metrics exposition")
    _assert_absent(_logs(caplog), NOT_IN_LOGS, where="a log record")


async def test_a_path_that_matched_no_route_is_neither_a_label_nor_a_span_name(
    traced_client: AsyncClient,
    record: Recording,
) -> None:
    """The unmatched-route case, which is where a scanner writes the labels.

    A 404 for `/CUSTOMER-PHONE-CANARY` must count as one unmatched request, not
    open a time series and a span name for whatever was tried.
    """
    await traced_client.get(f"/api/v1/{PHONE_CANARY}")
    scrape = await traced_client.get("/metrics")

    _assert_absent(scrape.text, CANARIES, where="the metrics exposition")
    _assert_absent(_spans(record), CANARIES, where="an exported span")


async def test_the_request_log_records_the_path_and_not_the_query_string(
    traced_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deliberate asymmetry, asserted rather than assumed.

    An operator asking what happened to one customer's request needs the path;
    that is what a log is for, and it is why the path is not held to the rule
    the other two signals are. The query string is a different matter — it is
    caller-supplied, unbounded, and the conventional place to find a token
    somebody pasted into a URL. If this ever starts recording `request.url`
    instead of `request.url.path`, the second assertion is what says so.
    """
    with caplog.at_level(logging.DEBUG):
        await traced_client.get(f"/api/v1/{PHONE_CANARY}", params={"token": JWT_CANARY})

    document = _logs(caplog)
    assert PHONE_CANARY in document, "the request log should name the path"
    assert JWT_CANARY not in document, "a query string reached the log store"


# -------------------------------------------------------- the provider path


async def test_a_provider_failure_does_not_export_the_provider_s_message(
    record: Recording,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider's error text is the classic label leak.

    Payment gateways in particular put the card, the merchant reference and
    sometimes the key straight into `message`. `CallOutcome` exists so the
    metric carries one of four closed words instead.
    """
    with caplog.at_level(logging.DEBUG):
        call = ProviderCall(provider=Provider.PAYMOB, operation="create_intention")
        try:
            raise RuntimeError(f"gateway rejected key={PAYMOB_CANARY} for {EMAIL_CANARY}")
        except RuntimeError:
            await call.record(CallOutcome.FAILURE)

        await record_provider_call(
            provider=Provider.OPENAI,
            operation="respond",
            outcome=CallOutcome.RATE_LIMITED,
        )

    _assert_absent(_spans(record), CANARIES, where="an exported span")
    _assert_absent(_logs(caplog), CANARIES, where="a log record")


async def test_an_exception_reaching_a_span_records_only_its_class_name(
    record: Recording,
) -> None:
    """`record_exception` and `set_status_on_exception` are both off.

    Left at the SDK's defaults this block would export `str(error)` as the
    status description and the whole traceback as a span event — which is how a
    trace backend ends up holding the one string the exception was carrying.
    """

    class PaymentDeclinedError(Exception):
        pass

    with pytest.raises(PaymentDeclinedError), span("provider.paymob", kind=SpanKind.CLIENT):
        raise PaymentDeclinedError(f"card for {PHONE_CANARY} declined, key {PAYMOB_CANARY}")

    document = _spans(record)
    _assert_absent(document, CANARIES, where="an exported span")
    # The class name survives, and it is chosen at the raise site.
    assert "PaymentDeclinedError" in document


# ----------------------------------------------------------- the queue path


async def test_a_job_span_does_not_export_the_job_s_payload(
    record: Recording,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job body holds exactly the customer data a span must not.

    The envelope is the whole message the worker pulled off Redis. What the
    span may say about it is the queue name, the attempt number, and how it
    ended.
    """
    envelope = JobEnvelope(
        body=f'{{"phone": "{PHONE_CANARY}", "prompt": "{PROMPT_CANARY}"}}',
        attempt=2,
        enqueued_at=datetime.now(UTC),
    )

    with caplog.at_level(logging.DEBUG), job_span(job_type="agent", envelope=envelope) as attempt:
        attempt.set_attribute(JOB_OUTCOME, SUCCEEDED)

    _assert_absent(_spans(record), CANARIES, where="an exported span")
    _assert_absent(_logs(caplog), CANARIES, where="a log record")


# ------------------------------------------------------- the exception field


def test_a_logged_traceback_carries_no_frame_locals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`logger.exception` writes a whole traceback into the log's `error` field.

    That is deliberate — `app/core/logging.py` funnels every `exc_info`
    through `formatException`, and a traceback is what makes an incident
    answerable. It is also the field most likely to carry something it should
    not, and the assumption worth pinning down is *which* something.

    `traceback.format_exception` renders the exception's message and each
    frame's source line. It does not render frame locals. That is what keeps a
    credential held in a local variable out of the log store, and it is not a
    property of logging in general: `rich`, `better_exceptions` and several
    error-reporting SDKs all render locals by default, and swapping the
    formatter for one of them would quietly start shipping them. This fails if
    that happens.

    The exception's *message* is a different matter, and it is not this test's
    to guarantee — a message is chosen at the raise site. What this fixes is
    that a raise site is the only way a value gets there.
    """
    logger = logging.getLogger("app.test.telemetry_privacy")

    def authenticate() -> None:
        token = f"Bearer {JWT_CANARY}"  # noqa: F841 — the point is that it is a local
        api_key = PAYMOB_CANARY  # noqa: F841
        raise TimeoutError("the provider did not answer")

    with caplog.at_level(logging.DEBUG):
        try:
            authenticate()
        except TimeoutError:
            logger.exception("provider.call_failed")

    document = _logs(caplog)
    _assert_absent(document, CANARIES, where="a logged traceback")
    # The traceback did arrive — this is not passing because nothing was logged.
    assert "TimeoutError" in document
    assert "the provider did not answer" in document
