"""A stream's cleanup runs however its response ends (FSR-02).

A client that leaves while the response is sending leaves the stream's
generator suspended at a `yield`; Starlette's `StreamingResponse` does not
close it, so its `finally` - unsubscribing from the hub, uncounting the stream
- waits for garbage collection. `EventStreamResponse` closes it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from app.services.realtime import EventStreamResponse


def _stream(cleaned: list[str]) -> AsyncIterator[str]:
    async def frames() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            while True:
                yield ": keep-alive\n\n"
        finally:
            cleaned.append("unsubscribed")

    return frames()


async def _receive() -> dict[str, Any]:
    # A server with nothing more to say: the request was read, the client has
    # not (yet) been seen to leave. Waits as a real server's receive does.
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


def _scope(spec_version: str) -> dict[str, Any]:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}}


async def _call(response: StreamingResponse, spec_version: str) -> None:
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        # The second body chunk finds the client gone - the generator is
        # suspended at its `yield` while this raises.
        if message["type"] == "http.response.body" and len(sent) >= 2:
            raise OSError("the client went away")
        sent.append(message)

    with pytest.raises((ClientDisconnect, OSError)):
        await response(_scope(spec_version), _receive, send)


@pytest.mark.parametrize("spec_version", ["2.4", "2.3"])
async def test_a_client_gone_mid_send_still_unsubscribes(spec_version: str) -> None:
    cleaned: list[str] = []

    await _call(EventStreamResponse(_stream(cleaned), media_type="text/event-stream"), spec_version)

    assert cleaned == ["unsubscribed"], "the stream's finally ran when the response ended"
