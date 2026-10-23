"""Two hundred open streams on one API process (RT-06).

The real server of `test_events_stream`, one process, 200 streams of one
workspace: forty members with five streams each (the per-person default), the
per-workspace limit raised to 200 as an operator would size it. Twenty
inbound messages; every stream must receive
every `message.created`. Recorded for the report: the p95 delivery latency
across all 4,000 deliveries, measured from before the ingestion call, and the
Python heap held per open stream (tracemalloc; client and server share this
process, so it is an upper bound on the server's share). The latency is
recorded, not asserted: this test runs beside whatever else the machine is
doing, and a single stream's p95 under 1 s is asserted by
`test_a_new_message_reaches_the_stream_within_a_second`.
"""

from __future__ import annotations

import asyncio
import time
import tracemalloc

import httpx
import pytest

from tests.integration.sequence_harness import conversations
from tests.integration.test_events_stream import (
    Reader,
    _record,
    customer_writes,
    member,
    running,
    ticket,
)

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]

STREAMS = 200
PER_PERSON = 5
MESSAGES = 20
STREAM = "/api/v1/events/stream"


async def test_two_hundred_streams_on_one_process(prepared_database: str) -> None:
    async with running(prepared_database, rt_streams_per_workspace=STREAMS) as live:
        people = [(await member(live.mine, live))[1] for _ in range(STREAMS // PER_PERSON)]
        conversation = (await conversations(live.mine))[0]
        limits = httpx.Limits(max_connections=STREAMS + 10, max_keepalive_connections=20)
        async with httpx.AsyncClient(
            base_url=live.base_url, timeout=httpx.Timeout(30.0, read=None), limits=limits
        ) as client:
            tickets = [
                await ticket(client, headers) for headers in people for _ in range(PER_PERSON)
            ]
            tracemalloc.start()
            before = tracemalloc.get_traced_memory()[0]
            responses = []
            try:
                for token in tickets:
                    request = client.build_request("GET", STREAM, params={"ticket": token})
                    response = await client.send(request, stream=True)
                    assert response.status_code == 200
                    responses.append(response)
                readers = [Reader(response) for response in responses]
                for reader in readers:
                    assert (await reader.next())["retry"] == "3000"
                held = tracemalloc.get_traced_memory()[0] - before
                tracemalloc.stop()

                latencies: list[float] = []

                async def receive(reader: Reader, started: float) -> None:
                    event = await reader.until("message.created", within=60)
                    assert event["data"]["conversation_id"] == str(conversation.id)
                    latencies.append(time.monotonic() - started)

                for index in range(MESSAGES):
                    started = time.monotonic()
                    waiting = [asyncio.create_task(receive(reader, started)) for reader in readers]
                    await customer_writes(live, live.mine, conversation, f"capacity {index}")
                    await asyncio.gather(*waiting)
            finally:
                for response in responses:
                    await response.aclose()

    assert len(latencies) == STREAMS * MESSAGES, "every stream received every message"
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    _record("p95_delivery_200_streams_seconds", p95)
    _record("python_heap_bytes_per_stream", held / STREAMS)
