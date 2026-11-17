"""Put an e2e document into a state the API cannot reach in a few calls (FE-6, EC6-04).

    python -m tests.e2e_stack.knowledge_states processing <document-id>
        the document says it is being indexed, and stays so (nothing re-queues it)
    python -m tests.e2e_stack.knowledge_states failed <document-id>
        the document failed, with an explanation a person can read
    python -m tests.e2e_stack.knowledge_states warnings <document-id>
        a ready document whose extractor counted re-ordered lines
    python -m tests.e2e_stack.knowledge_states needs-reindex <document-id>
        a ready document served by an embedding model the deployment no longer queries with
    python -m tests.e2e_stack.knowledge_states reindexing <document-id>
        a ready document with a re-index attempt claimed and in flight

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that for the package). The journeys
run it with ``docker exec wasla-fe-e2e-api``. Everything else FE-6's journeys need
is made through the API and the fake pipeline - a document going pending ->
processing -> ready by upload, a duplicate by a second upload, an empty base, an
agent in each status, a disabled default. The gap report's "on" state is not
reachable here: ``RAG_GAP_REPORT_ENABLED`` is the profile's, read at start-up,
so it is proved by a unit test and a story instead.

Rows are written through the application's models. Each command prints one JSON
line naming what it made, read back from the database.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

COMMANDS: Final = ("processing", "failed", "warnings", "needs-reindex", "reindexing")
USAGE: Final = (
    "usage: python -m tests.e2e_stack.knowledge_states "
    "(processing | failed | warnings | needs-reindex | reindexing) <document-id>"
)
FAILED_ERROR: Final = "The file could not be read: it has no text to index. (e2e)"
WARNINGS: Final = {"digits_glued_to_arabic": 3}
#: An embedding model nobody queries with, so the served generation is stale.
STALE_MODEL: Final = "e2e-retired-embedding-model"


def parse(argv: list[str]) -> tuple[str, uuid.UUID]:
    """(command, document id) or the usage, before any connection is opened."""
    if len(argv) == 2 and argv[0] in COMMANDS:
        try:
            return argv[0], uuid.UUID(argv[1])
        except ValueError:
            pass
    raise SystemExit(USAGE)


async def _document(session: AsyncSession, document_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from app.db.models.knowledge import Document

    document = await session.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        raise SystemExit(f"no document {document_id}")
    return document


async def _active(session: AsyncSession, document_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from app.db.models.knowledge import DocumentIndexGeneration, GenerationState

    generation = await session.scalar(
        select(DocumentIndexGeneration).where(
            DocumentIndexGeneration.document_id == document_id,
            DocumentIndexGeneration.state == GenerationState.ACTIVE,
        )
    )
    if generation is None:
        raise SystemExit(f"document {document_id} has no serving generation: wait for ready")
    return generation


async def apply(session: AsyncSession, command: str, document_id: uuid.UUID) -> dict[str, object]:
    from app.db.models.knowledge import (
        DocumentIndexGeneration,
        DocumentStatus,
        GenerationState,
    )

    document = await _document(session, document_id)
    made: dict[str, object] = {"document_id": str(document.id), "state": command}
    if command == "processing":
        document.status = DocumentStatus.PROCESSING
    elif command == "failed":
        document.status = DocumentStatus.FAILED
        document.error = FAILED_ERROR
        made["error"] = FAILED_ERROR
    elif command == "warnings":
        document.extraction_warnings = dict(WARNINGS)
        made["extraction_warnings"] = WARNINGS
    elif command == "needs-reindex":
        active = await _active(session, document.id)
        active.embedding_model = STALE_MODEL
        made["embedding_model"] = STALE_MODEL
    else:
        active = await _active(session, document.id)
        latest = await session.scalar(
            select(func.max(DocumentIndexGeneration.number)).where(
                DocumentIndexGeneration.document_id == document.id
            )
        )
        number = int(latest or active.number) + 1
        now = datetime.now(UTC)
        session.add(
            DocumentIndexGeneration(
                tenant_id=document.tenant_id,
                document_id=document.id,
                number=number,
                state=GenerationState.PROCESSING,
                trigger="reindex",
                attempts=1,
                claim_token=uuid.uuid4(),
                claimed_at=now,
                content=active.content,
                content_hash=active.content_hash,
            )
        )
        made["generation"] = number
    await session.flush()
    made["status"] = document.status.value
    return made


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, target = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            made = await apply(session, command, target)
            await session.commit()
        return made
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    print(json.dumps(asyncio.run(run(argv))))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])
