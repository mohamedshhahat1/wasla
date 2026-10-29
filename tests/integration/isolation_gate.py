"""The isolation gate every new way of reading knowledge passes (FX-1.12, IMP-23, G1 .. G3).

Two workspaces hold **byte-identical** documents - the same titles, text,
hashes and filenames - so nothing but the tenant predicate can tell their rows
apart, and the foreign one also holds a marker document the viewer has no copy
of. A path is isolated when, read as the viewer, nothing in its result names a
foreign document or chunk id, the marker's text, or (for a refusal) the fact
that the other workspace holds the same text.

    twins = await build_twins(indexing)
    result = await some_new_path(viewer=twins.viewer, ...)
    twins.assert_isolated(result)

`assert_isolated` serialises whatever it is given - rows, dataclasses, JSON,
an HTTP body, an exception message, a cache key - so one helper serves every
path, and a path's test is one line on top of building the twins.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models.knowledge import DocumentChunk
from app.services.knowledge_service import KnowledgeService
from tests.integration.rag_harness import Indexing

SHARED = (
    ("Price list", "pricing.txt", "Premium finishing costs 7900 EGP per square metre."),
    ("Refund policy", "refunds.txt", "Items may be returned within fourteen days for a refund."),
)
MARKER = "Foreign marker zephyr quokka: the other workspace's private supplier rate is 1234 EGP."


@dataclass
class Twins:
    """Two workspaces, identical content, and what the viewer must never see."""

    viewer: uuid.UUID
    foreign: uuid.UUID
    viewer_documents: dict[str, uuid.UUID] = field(default_factory=dict)
    foreign_documents: dict[str, uuid.UUID] = field(default_factory=dict)
    foreign_chunks: set[uuid.UUID] = field(default_factory=set)
    foreign_knowledge_bases: set[uuid.UUID] = field(default_factory=set)

    def foreign_ids(self) -> set[str]:
        ids = {
            *self.foreign_documents.values(),
            *self.foreign_chunks,
            *self.foreign_knowledge_bases,
        }
        return {str(value) for value in ids} | {str(self.foreign)}

    def assert_isolated(self, result: Any) -> None:
        text = _serialise(result)
        leaked = sorted(value for value in self.foreign_ids() if value in text)
        assert not leaked, f"foreign ids in the viewer's result: {leaked}"
        assert "zephyr quokka" not in text, "the foreign workspace's marker text leaked"

    def assert_refusal_reveals_nothing(self, message: str) -> None:
        """A refusal names only the viewer's own documents (G3)."""
        self.assert_isolated(message)
        assert "another workspace" not in message.lower()
        assert "other workspace" not in message.lower()


def _serialise(result: Any) -> str:
    def default(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
        if hasattr(value, "__table__"):
            return {c.name: getattr(value, c.key, None) for c in value.__table__.columns}
        if isinstance(value, set | frozenset | tuple):
            return list(value)
        return str(value)

    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=default, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(result)


async def build_twins(indexing: Indexing) -> Twins:
    """Index the shared documents into two workspaces, and the marker into the foreign one."""
    twins = Twins(viewer=await indexing.workspace(), foreign=await indexing.workspace())
    for tenant_id, documents in (
        (twins.viewer, twins.viewer_documents),
        (twins.foreign, twins.foreign_documents),
    ):
        async with indexing.database.session() as session:
            knowledge = KnowledgeService(
                session=session, tenant_id=tenant_id, queue=indexing.queue()
            )
            base = await knowledge.ensure_default_knowledge_base()
            if tenant_id == twins.foreign:
                twins.foreign_knowledge_bases.add(base.id)
            entries = list(SHARED)
            if tenant_id == twins.foreign:
                entries.append(("Supplier rates", "suppliers.txt", MARKER))
            for title, filename, body in entries:
                view, _ = await knowledge.submit(
                    knowledge_base_id=base.id, title=title, raw=body, filename=filename
                )
                documents[title] = view.document.id
    await indexing.drain()
    async with indexing.database.session() as session:
        twins.foreign_chunks = set(
            await session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.tenant_id == twins.foreign)
            )
        )
    assert twins.foreign_chunks, "the foreign workspace indexed nothing; the gate would be vacuous"
    return twins
