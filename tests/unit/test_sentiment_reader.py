"""Reading a customer's mood from one message.

The provider is driven through `httpx.MockTransport`, so the request that would
go to OpenAI is asserted rather than sent. What matters here is the request
shape - a schema the provider enforces, and no sampling - and what happens when
the answer comes back malformed, which is the case that decides whether a bad
reading can escalate a conversation on its own.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.exceptions import ExternalServiceError
from app.db.models.sentiment import MAX_INTENT_LENGTH, SentimentLabel
from app.integrations.openai.client import ResponsesClient
from app.services.sentiment_reader import (
    MAX_ANALYSED_CHARACTERS,
    MAX_ANALYSIS_TOKENS,
    SentimentAnalyzer,
)

MODEL = "gpt-4.1-mini"


def _reply(payload: dict | str) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "id": "resp_1",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42},
    }


def _analyzer(handler, *, model: str = MODEL) -> SentimentAnalyzer:
    client = ResponsesClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_key="test-key",
        max_attempts=1,
    )
    return SentimentAnalyzer(responses=client, model=model)


def _answering(payload: dict | str, *, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json=_reply(payload))

    return handler


@pytest.mark.asyncio
async def test_a_reading_comes_back_decoded():
    analyzer = _analyzer(
        _answering(
            {
                "sentiment": "angry",
                "score": -0.9,
                "intent": "complaint",
                "confidence": 0.85,
            }
        )
    )

    reading = await analyzer.read("This is the third time I have asked.")

    assert reading.label is SentimentLabel.ANGRY
    assert reading.score == pytest.approx(-0.9)
    assert reading.intent == "complaint"
    assert reading.confidence == pytest.approx(0.85)
    assert reading.model == MODEL
    assert reading.usage.total_tokens == 42


@pytest.mark.asyncio
async def test_the_provider_is_made_to_answer_in_a_fixed_shape():
    """Asking for JSON in the prompt and hoping fails on the unusual message."""
    seen: list = []
    analyzer = _analyzer(
        _answering(
            {"sentiment": "neutral", "score": 0, "intent": None, "confidence": 0.5}, seen=seen
        )
    )

    await analyzer.read("hello")

    fmt = seen[0]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"]["additionalProperties"] is False
    assert sorted(fmt["schema"]["required"]) == ["confidence", "intent", "score", "sentiment"]
    assert fmt["schema"]["properties"]["sentiment"]["enum"] == [
        label.value for label in SentimentLabel
    ]


@pytest.mark.asyncio
async def test_classification_does_not_sample():
    """A rule that fires intermittently is worse than one that never fires."""
    seen: list = []
    analyzer = _analyzer(
        _answering(
            {"sentiment": "neutral", "score": 0, "intent": None, "confidence": 0.5}, seen=seen
        )
    )

    await analyzer.read("hello")

    assert seen[0]["temperature"] == 0.0
    assert seen[0]["max_output_tokens"] == MAX_ANALYSIS_TOKENS


@pytest.mark.asyncio
async def test_a_very_long_message_is_bounded_before_it_is_sent():
    seen: list = []
    analyzer = _analyzer(
        _answering(
            {"sentiment": "neutral", "score": 0, "intent": None, "confidence": 0.5}, seen=seen
        )
    )

    await analyzer.read("x" * (MAX_ANALYSED_CHARACTERS * 3))

    assert len(seen[0]["input"][0]["content"]) == MAX_ANALYSED_CHARACTERS


@pytest.mark.asyncio
async def test_an_empty_message_is_never_sent_at_all():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("the provider should not have been called")

    with pytest.raises(ExternalServiceError):
        await _analyzer(handler).read("   \n  ")


@pytest.mark.asyncio
async def test_an_emphatic_score_is_pulled_into_range_rather_than_discarded():
    """A score of 1.4 is a model being emphatic, not a model being broken."""
    analyzer = _analyzer(
        _answering({"sentiment": "positive", "score": 1.4, "intent": "praise", "confidence": 3})
    )

    reading = await analyzer.read("perfect, thank you!")

    assert reading.score == pytest.approx(1.0)
    assert reading.confidence == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_a_missing_confidence_is_read_as_no_confidence():
    """Below every escalation floor. Silence must not be read as certainty."""
    analyzer = _analyzer(_answering({"sentiment": "angry", "score": -1, "intent": "complaint"}))

    reading = await analyzer.read("unacceptable")

    assert reading.confidence == 0.0


@pytest.mark.asyncio
async def test_a_label_outside_the_enum_is_refused():
    """Guessing which mood was meant is how a rule fires on nobody's decision."""
    analyzer = _analyzer(
        _answering({"sentiment": "furious", "score": -1, "intent": None, "confidence": 0.9})
    )

    with pytest.raises(ExternalServiceError):
        await analyzer.read("unacceptable")


@pytest.mark.asyncio
async def test_an_unparseable_answer_is_refused():
    with pytest.raises(ExternalServiceError):
        await _analyzer(_answering("not json at all")).read("hello")


@pytest.mark.asyncio
async def test_an_answer_that_is_not_an_object_is_refused():
    with pytest.raises(ExternalServiceError):
        await _analyzer(_answering("[1, 2, 3]")).read("hello")


@pytest.mark.asyncio
async def test_an_empty_answer_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp_1", "output": []})

    with pytest.raises(ExternalServiceError):
        await _analyzer(handler).read("hello")


@pytest.mark.asyncio
async def test_an_overlong_intent_is_trimmed_to_what_the_column_holds():
    analyzer = _analyzer(
        _answering(
            {
                "sentiment": "neutral",
                "score": 0,
                "intent": "A" * (MAX_INTENT_LENGTH * 2),
                "confidence": 0.4,
            }
        )
    )

    reading = await analyzer.read("hello")

    assert reading.intent is not None
    assert len(reading.intent) == MAX_INTENT_LENGTH
    # Lowercased as well, so the same intent groups with itself in a report.
    assert reading.intent == "a" * MAX_INTENT_LENGTH


@pytest.mark.asyncio
async def test_a_blank_intent_is_stored_as_none():
    analyzer = _analyzer(
        _answering({"sentiment": "neutral", "score": 0, "intent": "   ", "confidence": 0.4})
    )

    assert (await analyzer.read("hi")).intent is None
