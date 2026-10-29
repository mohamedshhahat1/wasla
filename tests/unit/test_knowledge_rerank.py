"""The optional re-rank: a strict timeout, and the fused order on every failure (RQ-16, M-R37).

The provider is scripted at the HTTP transport; everything above it - request
building, retries, parsing - is the real `ResponsesClient`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.integrations.openai.client import ResponsesClient
from app.services.knowledge_rerank import ERROR, OK, TIMEOUT, LlmReranker
from app.services.retrieval_ranking import VECTOR, Candidate
from tests.integration.ai_harness import text_response

Handler = Callable[[dict[str, Any]], Awaitable[httpx.Response]]


def _candidates(count: int = 3) -> list[Candidate]:
    return [
        Candidate(
            chunk_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            generation_id=uuid.uuid4(),
            ordinal=n,
            content=f"passage {n}",
            title="T",
            heading_path=None,
            distance=0.1 * n,
            channels={VECTOR},
        )
        for n in range(count)
    ]


def _reranker(handler: Handler, seen: list[dict[str, Any]], *, timeout: float = 1.5) -> LlmReranker:
    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return await handler(body)

    client = ResponsesClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handle)), api_key="sk-test-not-real"
    )
    return LlmReranker(client=client, model="gpt-4.1-nano", timeout_seconds=timeout)


def _scores(*pairs: tuple[int, int]) -> Handler:
    async def handle(_body: dict[str, Any]) -> httpx.Response:
        text = json.dumps({"scores": [{"source": s, "score": v} for s, v in pairs]})
        return httpx.Response(200, json=text_response(text))

    return handle


async def test_scores_reorder_the_candidates_and_the_request_is_data_only() -> None:
    seen: list[dict[str, Any]] = []
    candidates = _candidates()

    result = await _reranker(_scores((1, 1), (2, 0), (3, 3)), seen).rerank("q", candidates)

    assert result.outcome == OK
    assert [c.ordinal for c in result.candidates] == [2, 0, 1]
    (body,) = seen
    assert body["store"] is False
    assert body["text"]["format"]["name"] == "passage_scores"
    # The passages travel inside the user turn, as JSON data.
    sent = json.loads(body["input"][0]["content"])
    assert [p["text"] for p in sent["passages"]] == ["passage 0", "passage 1", "passage 2"]


async def test_a_slow_provider_times_out_and_the_fused_order_is_kept() -> None:
    async def slow(_body: dict[str, Any]) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=text_response("{}"))

    candidates = _candidates()
    started = time.monotonic()
    result = await _reranker(slow, [], timeout=0.3).rerank("q", candidates)

    # Non-vacuity: the wall clock really stopped at the timeout.
    assert time.monotonic() - started < 2
    assert result.outcome == TIMEOUT
    assert result.candidates == candidates


async def test_a_refused_request_keeps_the_fused_order() -> None:
    async def refuse(_body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "no", "code": "bad_request"}})

    candidates = _candidates()
    result = await _reranker(refuse, []).rerank("q", candidates)
    assert result.outcome == ERROR
    assert result.candidates == candidates


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"scores": [{"source": 1, "score": 3}]}),  # sources missing
        json.dumps(
            {
                "scores": [
                    {"source": 1, "score": 3},
                    {"source": 1, "score": 2},
                    {"source": 2, "score": 1},
                ]
            }
        ),
        json.dumps(
            {
                "scores": [
                    {"source": 1, "score": 9},
                    {"source": 2, "score": 1},
                    {"source": 3, "score": 1},
                ]
            }
        ),
        json.dumps(
            {
                "scores": [
                    {"source": 4, "score": 1},
                    {"source": 2, "score": 1},
                    {"source": 3, "score": 1},
                ]
            }
        ),
    ],
)
async def test_malformed_scores_keep_the_fused_order(text: str) -> None:
    async def answer(_body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json=text_response(text))

    candidates = _candidates()
    result = await _reranker(answer, []).rerank("q", candidates)
    assert result.outcome == ERROR
    assert result.candidates == candidates
