"""The structural retrieval evaluation (FX-3.1, IMP-01), on fake embeddings.

What it measures is **structure**, not semantic quality: whether the passage
that answers a question exists intact in some chunk, whether a search on the
question's own words returns it, whether a stale version or a duplicate takes a
slot, whether a number or a word was cut, whether a section kept its heading.
The fake embedding model is a hashed bag of words (`tests/fake_embeddings.py`),
so "found" here means "findable by its words" - the property a real model needs
first and cannot recover once a chunker or an extractor has destroyed it.
Semantic metrics (recall@5, MRR@10, nDCG@5) belong to the real-provider lane.

Everything above the HTTP socket is production code: `KnowledgeService.submit`
(and the replace path once it exists), the committing ingestion worker, the
chunker, the extractor's child process, `RetrievalService.search` and the
repository's SQL. The comparator spans are measured with is the harness's own
and deliberately not the system's normaliser, so the measuring stick does not
move when the code under test does.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import app.services.retrieval_service as retrieval_module
from app.core.exceptions import ConflictError, ValidationError
from app.db.models.knowledge import Document, DocumentChunk, DocumentSource
from app.repositories.knowledge_repository import KnowledgeBaseRepository
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FakeEmbeddings
from tests.integration.rag_harness import Indexing

EVAL = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "eval"
TOP_K = 5

_FOLD = str.maketrans(
    {
        **{chr(0x0660 + n): str(n) for n in range(10)},
        **{chr(0x06F0 + n): str(n) for n in range(10)},
        "\u06cc": "\u064a",  # Persian yeh, which some PDF fonts map Arabic yeh to
        "\u06a9": "\u0643",  # keheh
        "\u06be": "\u0647",  # heh doachashmee
        "\u0640": None,  # tatweel
    }
)
_MARKS = re.compile("[\u064b-\u065f\u0670\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d")
_PRESENTATION = re.compile("[ﭐ-﷿ﹰ-﻿]")


def comparable(text: str) -> str:
    """The harness's own measuring stick: how two strings are judged the same."""
    folded = unicodedata.normalize("NFKC", text).translate(_FOLD)
    return _SPACE.sub(" ", _MARKS.sub("", folded)).strip().casefold()


def _tokens(text: str) -> set[str]:
    return {token.strip(".,:;!?()\"'؟،") for token in comparable(text).split(" ")} - {""}


@dataclass
class EvalRun:
    """One evaluation's raw observations and the metrics drawn from them."""

    ingested: dict[str, uuid.UUID] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def table(self, label: str) -> str:
        lines = [
            f"### {label}",
            "",
            "| Metric (fake embeddings - structural) | Value |",
            "|---|---|",
        ]
        lines += [
            f"| {name} | {value} |"
            for name, value in self.metrics.items()
            if not name.startswith("_")
        ]
        return "\n".join(lines) + "\n"


def load() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = json.loads((EVAL / "manifest.json").read_text(encoding="utf-8"))
    questions = [
        json.loads(line)
        for line in (EVAL / "questions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return manifest, questions


async def ingest(indexing: Indexing, tenant_id: uuid.UUID, run: EvalRun) -> None:
    """Every manifest document, the way a workspace would upload it.

    The second version of the price list goes through the replace path when the
    service has one, and is uploaded as a new document when it does not - which
    is what a workspace did before there was a replace path (RV-02).
    """
    manifest, _ = load()
    for entry in manifest:
        raw_bytes = (EVAL / "corpus" / entry["file"]).resolve().read_bytes()
        source = DocumentSource(entry["source"])
        raw = (
            base64.b64encode(raw_bytes).decode("ascii")
            if source is DocumentSource.PDF
            else raw_bytes.decode("utf-8")
        )
        async with indexing.database.session() as session:
            knowledge = KnowledgeService(
                session=session, tenant_id=tenant_id, queue=indexing.queue()
            )
            base = await KnowledgeBaseRepository(session, tenant_id=tenant_id).get_by_name(
                entry["kb"]
            )
            if base is None:
                base = await knowledge.create_knowledge_base(name=entry["kb"])
            try:
                replace = getattr(knowledge, "replace_content", None)
                if entry["role"] == "version-2" and replace is not None:
                    view, _ = await replace(
                        run.ingested["price-v1"], title=entry["title"], raw=raw, source=source
                    )
                else:
                    view, _ = await knowledge.submit(
                        knowledge_base_id=base.id,
                        title=entry["title"],
                        raw=raw,
                        source=source,
                        filename=Path(entry["file"]).name,
                    )
            except (ConflictError, ValidationError) as error:
                run.refused[entry["id"]] = type(error).__name__
                await session.rollback()
                continue
            run.ingested[entry["id"]] = view.document.id
    await indexing.drain(budget=400)


async def evaluate(indexing: Indexing, tenant_id: uuid.UUID) -> EvalRun:
    """Ingest the corpus into `tenant_id`, ask every question, and measure."""
    run = EvalRun()
    await ingest(indexing, tenant_id, run)
    return await measure(indexing, tenant_id, run)


async def measure(indexing: Indexing, tenant_id: uuid.UUID, ingested: EvalRun) -> EvalRun:
    """Ask every question of what `ingested` put into `tenant_id`, and measure."""
    run = EvalRun(ingested=dict(ingested.ingested), refused=dict(ingested.refused))
    _, questions = load()

    async with indexing.database.session() as session:
        documents = {
            row.id: row
            for row in await session.scalars(
                select(Document).where(Document.tenant_id == tenant_id)
            )
        }
        chunks: dict[uuid.UUID, list[DocumentChunk]] = defaultdict(list)
        for chunk in await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.tenant_id == tenant_id)
            .order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
        ):
            chunks[chunk.document_id].append(chunk)

    async def ask(query: str, *, threshold: bool) -> list[Any]:
        with pytest.MonkeyPatch.context() as patch:
            if not threshold:
                # The server's 0.75 cut-off was set for a real model; a bag of
                # words lands most real matches past it. Lifted for the ranking
                # metric only, measured separately below with it in place.
                patch.setattr(retrieval_module, "MAX_DISTANCE", 2.0)
            async with indexing.database.session() as session:
                found = await RetrievalService(
                    session=session,
                    tenant_id=tenant_id,
                    embeddings=FakeEmbeddings(),  # type: ignore[arg-type]
                ).search(query=query, top_k=TOP_K, max_distance=2.0)
                return list(found.passages)

    counts: dict[str, Any] = defaultdict(int)
    producers: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    duplicate_slots = 0
    searches = 0
    for question in questions:
        doc_key = question["doc"]
        passages = await ask(question["q"], threshold=False)
        searches += 1
        contents = [comparable(p.content) for p in passages]
        duplicate_slots += len(contents) - len(set(contents))
        record: dict[str, Any] = {"id": question["id"], "returned": len(passages)}
        if question.get("stale"):
            counts["stale questions"] += 1
            if any(comparable(question["stale"]) in c for c in contents):
                counts["stale version in top 5"] += 1
        if doc_key is None:
            counts["unanswerable"] += 1
            run.questions.append(record)
            continue
        counts["answerable"] += 1
        span = comparable(question["span"])
        document_id = run.ingested.get(doc_key)
        intact = document_id is not None and any(
            span in comparable(c.content) for c in chunks.get(document_id, [])
        )
        hit = any(span in c for c in contents)
        counts["labelled spans intact in some chunk"] += int(intact)
        counts["span in top 5"] += int(hit)
        # Ties at the best distance share rank 1: the database orders tied rows
        # arbitrarily, and a metric must not.
        best = [comparable(p.content) for p in passages if p.distance == passages[0].distance]
        counts["span at rank 1"] += int(any(span in c for c in best))
        thresholded = await ask(question["q"], threshold=True)
        if not thresholded:
            counts["answerable-but-empty at 0.75"] += 1
        producer = question.get("producer")
        if producer and question["lang"] == "ar-pdf":
            # At the real threshold: a bag of words that shares no code point
            # with the passage cannot reach it, which is RV-01's symptom.
            producers[producer][0] += int(
                any(
                    p.document_id == document_id and span in comparable(p.content)
                    for p in thresholded
                )
            )
            producers[producer][1] += 1
        record.update(intact=intact, hit=hit)
        run.questions.append(record)

    presentation = sum(
        1 for rows in chunks.values() for c in rows if _PRESENTATION.search(c.content)
    )
    numbers_cut = words_cut = 0
    headed_without = 0
    for doc_key, document_id in run.ingested.items():
        document = documents.get(document_id)
        if document is None or not document.content:
            continue
        source_tokens = _tokens(document.content)
        for chunk in chunks.get(document_id, []):
            for token in _tokens(chunk.content) - source_tokens:
                if _NUMBER.search(token):
                    numbers_cut += 1
                else:
                    words_cut += 1
        for question in (q for q in questions if q["doc"] == doc_key and q.get("heading")):
            heading = comparable(question["heading"].lstrip("# "))
            for chunk in chunks.get(document_id, []):
                carried = comparable(
                    chunk.content + " " + (getattr(chunk, "heading_path", None) or "")
                )
                if comparable(question["span"]) in carried and heading not in carried:
                    headed_without += 1

    answerable = counts["answerable"] or 1
    total = counts["answerable"]
    refused = f"{len(run.ingested)} / {len(run.refused)} {sorted(run.refused)}"
    run.metrics = {
        "documents ingested / refused": refused,
        "chunks": sum(len(v) for v in chunks.values()),
        "labelled spans intact in some chunk": (
            f"{counts['labelled spans intact in some chunk']} / {total}"
        ),
        "span in top 5 (threshold lifted)": f"{counts['span in top 5']} / {total}",
        "span at rank 1 (threshold lifted)": f"{counts['span at rank 1']} / {total}",
        "chunks holding Arabic presentation forms": presentation,
        "questions with a stale version in the top results": (
            f"{counts['stale version in top 5']} / {counts['stale questions']}"
        ),
        "duplicate passages per search": round(duplicate_slots / max(searches, 1), 3),
        "answerable-but-empty at 0.75": f"{counts['answerable-but-empty at 0.75']} / {answerable}",
        "Arabic PDF questions answered at 0.75 (per producer)": ", ".join(
            f"{name} {hit}/{total}" for name, (hit, total) in sorted(producers.items())
        ),
        "numbers cut across the corpus": numbers_cut,
        "words cut across the corpus": words_cut,
        "headed-section chunks without their heading": headed_without,
    }
    run.metrics["_raw"] = {
        "intact": counts["labelled spans intact in some chunk"],
        "answerable": counts["answerable"],
        "hit": counts["span in top 5"],
        "rank1": counts["span at rank 1"],
        "presentation": presentation,
        "empty": counts["answerable-but-empty at 0.75"],
        "stale": counts["stale version in top 5"],
        "duplicates": duplicate_slots,
        "numbers_cut": numbers_cut,
        "words_cut": words_cut,
        "headed_without": headed_without,
        "producers": {k: list(v) for k, v in producers.items()},
    }
    return run
