"""Handing a test double to code that is typed for the real thing.

The suite is full of stand-ins: a Redis that keeps its lists in a dictionary, a
transport that answers from a table, a session that records what it was asked
to do. Each exists because the alternative is a test that proves nothing — a
fake `lrem` that always answers `1` cannot tell dead-letter deduplication
working from dead-letter deduplication broken.

None of them *is* the class the production code declares, and none of the
libraries involved publishes a Protocol for "the part of this that Wasla uses".
So somewhere the claim "this stands in for that" has to be made. The choice is
where:

- At each of the hundred-odd call sites, as `cast(Any, ...)`. That spreads an
  unexplained assertion across the suite and makes every one of them look like
  a workaround rather than a decision.
- Once per stand-in family, here, as a named function whose docstring says what
  is being claimed and why it holds.

This is the second. `as_redis(fake)` reads at the call site as "hand this to
code typed for redis-py", which is exactly what is happening, and the reasoning
for whether that is safe lives in one place instead of being re-derived by
whoever reads the next test.

**What these do not do.** They do not check anything at runtime and they are
not a substitute for the fake being faithful. `tests/fake_queue_redis.py`
implements redis-py's semantics deliberately — `lrem` returns how many it
removed, `blmove` returns `None` on an empty list, a pipeline queues
synchronously and applies on `execute` — and that faithfulness is what makes
the tests meaningful. These functions only stop the type checker from having an
opinion about a substitution the suite has already decided is correct.

`as_table` is the exception and is a real narrowing rather than an assertion:
SQLAlchemy declares `__table__` as `FromClause` on the declarative base, and a
mapped class always has a `Table`. The `isinstance` proves it rather than
claiming it.
"""

from __future__ import annotations

import uuid
from typing import cast

from fastapi.security import HTTPAuthorizationCredentials
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import FromClause
from starlette.requests import Request

from app.core.config import Settings
from app.core.oauth_flow import OAuthFlowStore
from app.core.redis import RedisClient
from app.core.storage import MediaStorage, build_key
from app.db.session import Database
from app.integrations.billing.checkout import RecurringProvider
from app.integrations.google.client import GoogleOAuthClient
from app.integrations.google.oidc import GoogleIdTokenVerifier
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.embeddings import EmbeddingsClient
from app.integrations.whatsapp.client import WhatsAppClient
from app.services.media_reader import MediaReader
from app.services.messaging_service import MessagingService
from app.services.sentiment_reader import SentimentAnalyzer
from app.services.sentiment_service import SentimentService
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentQueue


def as_redis(fake: object) -> Redis:
    """A queue stand-in, for code typed against redis-py's client.

    `ReliableQueue` issues about fifteen commands and redis-py publishes no
    Protocol for them, so the concrete class is what the constructor can be
    annotated with. `FakeQueueRedis` implements those commands with redis-py's
    own return semantics, which is the property the queue's correctness tests
    actually depend on.
    """
    return cast("Redis", fake)


def as_redis_client(fake: object) -> RedisClient:
    """A stand-in for Wasla's own Redis wrapper — the `.client` and `.check` pair."""
    return cast("RedisClient", fake)


def as_http_client(fake: object) -> AsyncClient:
    """A transport stand-in, for a provider client typed against httpx.

    Most of these are real `httpx.AsyncClient` objects over a
    `MockTransport`; the ones that reach here are the hand-written doubles that
    record calls instead of routing them.
    """
    return cast("AsyncClient", fake)


def as_session(fake: object) -> AsyncSession:
    """A recording session, for a service typed against SQLAlchemy's.

    Used where the test is about *what was asked of the database* rather than
    about what the database did — an ordering, a flush that must happen before
    a commit — and a real session would answer the question with a query rather
    than with a record of the calls.
    """
    return cast("AsyncSession", fake)


def as_credentials(fake: object) -> HTTPAuthorizationCredentials:
    """A bearer credential stand-in, for `get_current_user`.

    Used by the tests that call the dependency directly rather than through
    a request, where FastAPI would have built one.
    """
    return cast("HTTPAuthorizationCredentials", fake)


def as_database(fake: object) -> Database:
    """A session-handing stand-in, for a worker typed against `Database`.

    The workers take a `Database` and use one thing from it: `session()`. These
    hand back the suite's transaction-scoped session instead, so a worker's
    writes roll back with everything else the test did.
    """
    return cast("Database", fake)


def as_whatsapp(fake: object) -> WhatsAppClient:
    """An outbound stand-in, for code that would otherwise message a customer."""
    return cast("WhatsAppClient", fake)


def as_responses(fake: object) -> ResponsesClient:
    """An inference stand-in, for code typed against the Responses client."""
    return cast("ResponsesClient", fake)


def as_request(fake: object) -> Request:
    """A minimal request object, for a dependency typed against Starlette's."""
    return cast("Request", fake)


def as_recurring(fake: object) -> RecurringProvider:
    """A saved-card stand-in, for code typed against the recurring protocol."""
    return cast("RecurringProvider", fake)


def as_embeddings(fake: object) -> EmbeddingsClient:
    """An embeddings stand-in, for retrieval typed against the real client.

    The RAG tests need vectors that are *comparable in a known way* - the point
    of most of them is which chunk comes back first - and a real provider gives
    numbers nobody can reason about.
    """
    return cast("EmbeddingsClient", fake)


def as_media_reader(fake: object) -> MediaReader:
    """A reader stand-in, for a worker typed against the real one."""
    return cast("MediaReader", fake)


def as_agent_queue(fake: object) -> AgentQueue:
    """A recording queue, for code typed against the agent queue."""
    return cast("AgentQueue", fake)


def as_media_queue(fake: object) -> MediaQueue:
    """A recording queue, for code typed against the media queue."""
    return cast("MediaQueue", fake)


def as_messaging(fake: object) -> MessagingService:
    """An outbound stand-in, for an agent turn typed against the messaging service."""
    return cast("MessagingService", fake)


def as_analyzer(fake: object) -> SentimentAnalyzer:
    """A classifier stand-in, for the sentiment service.

    The tests that use one are about what an escalation *does*, so the reading
    has to be chosen rather than inferred - which is the one thing a real
    classifier cannot offer.
    """
    return cast("SentimentAnalyzer", fake)


def as_flow_store(fake: object) -> OAuthFlowStore:
    """A scripted OAuth flow store, for the Google service."""
    return cast("OAuthFlowStore", fake)


def as_google_client(fake: object) -> GoogleOAuthClient:
    """A scripted code exchange, so no test reaches Google."""
    return cast("GoogleOAuthClient", fake)


def as_id_token_verifier(fake: object) -> GoogleIdTokenVerifier:
    """A scripted verifier, for the claims a test wants to put in front of it."""
    return cast("GoogleIdTokenVerifier", fake)


def as_sentiment(fake: object) -> SentimentService:
    """A sentiment stand-in, for an agent turn typed against the service."""
    return cast("SentimentService", fake)


def as_settings(fake: object) -> Settings:
    """A settings stand-in, for a worker typed against the real `Settings`.

    A handful of tests declare a small object carrying only the fields the code
    under test reads, rather than building a full `Settings` - which validates
    a dozen unrelated rules and would make the test about configuration.
    """
    return cast("Settings", fake)


def as_table(clause: FromClause) -> Table:
    """The `Table` behind a mapped class, narrowed rather than asserted.

    `DeclarativeBase.__table__` is declared as `FromClause` because a mapped
    class *can* be mapped to a subquery. Every model in this application is
    mapped to a table, and the schema tests need `indexes`, `constraints` and
    `columns`, which only `Table` has. The `isinstance` is what makes this a
    narrowing instead of a claim - a model that stopped being a table would
    fail here rather than somewhere less obvious.
    """
    assert isinstance(clause, Table), f"{clause} is not mapped to a table"
    return clause


async def store_object(
    storage: MediaStorage,
    *,
    tenant_id: uuid.UUID,
    data: bytes,
    mime_type: str | None = None,
) -> str:
    """Put one object somewhere a test can read it back, and say where.

    `MediaStorage` deliberately has no method that allocates a key and writes
    in one step: production callers commit the key first, so that an object can
    never exist without a row naming it (ADR-087). A test setting up "there is
    already a file here" is not a production write path and has no row to
    commit, so it does the two halves itself - here, once, rather than in
    twenty test bodies.

    Anything asserting the *protocol* - that an intent precedes a write, that a
    retry reuses a key - must not use this. It exists to arrange a fixture, not
    to stand in for the thing under test.
    """
    key = build_key(tenant_id=tenant_id, mime_type=mime_type)
    await storage.put_at(key=key, data=data, mime_type=mime_type)
    return key


__all__ = [
    "as_agent_queue",
    "as_analyzer",
    "as_credentials",
    "as_database",
    "as_embeddings",
    "as_flow_store",
    "as_google_client",
    "as_http_client",
    "as_id_token_verifier",
    "as_media_queue",
    "as_media_reader",
    "as_messaging",
    "as_recurring",
    "as_redis",
    "as_redis_client",
    "as_request",
    "as_responses",
    "as_sentiment",
    "as_session",
    "as_settings",
    "as_table",
    "as_whatsapp",
    "store_object",
]
