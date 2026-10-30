"""The structural evaluation of retrieval quality (RAG remediation part 2).

Same rules as `rag_eval`: fake embeddings, production code above the socket,
the harness's own measuring stick. What it adds is what Part 2 changed:

- **lexical**: answerable questions whose span the exact-word channel alone
  returns in its top 5 (RQ-15);
- **identifiers**: SKU, model-number and Latin-inside-Arabic questions, in a
  workspace indexed by `IdentifierBlindEmbeddings` - the bag of words with every
  token that holds a digit removed, which is how an embedding model treats an
  identifier it has never seen: as noise. Asked at the real 0.75 threshold,
  vector channel alone and hybrid;
- **follow-up / cross-lingual**: `part2/contract.jsonl`, each case asked as the
  customer typed it and as the query contract asks the model to write it - a
  standalone query, and for cross-lingual cases an `alternate_query` in the
  documents' language (RQ-17). The model is scripted: the numbers measure the
  plumbing that carries a good query, not a model's ability to write one;
- **two-chunk answers**: adjacent chunks of one section, asked by the end of
  the first; complete when the end of the second arrives in the same context;
- **delivered vs requested**: sources in the model-facing context against the
  passages the search kept, at the largest `max_results` the tool offers.

Written to run unchanged against the base of this Part (131bfa4), where the
lexical channel, the policy and `alternate_query` do not exist: each is
detected and reported "n/a" or measured without it, which is the "before".
"""

from __future__ import annotations

import inspect
import json
import os
import uuid
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import app.services.retrieval_service as retrieval_module
from app.db.models.knowledge import Document, DocumentChunk
from app.repositories.knowledge_repository import DocumentChunkRepository
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FakeEmbeddings, embed_text
from tests.fakes import as_embeddings
from tests.integration.rag_eval import EVAL, EvalRun, comparable, ingest, load
from tests.integration.rag_harness import Indexing

PART2 = EVAL / "part2"
TOP_K = 5
IDENTIFIER_DOCS = ("csv-0", "csv-1", "en-prices-0", "en-prices-1", "en-prices-2")
# Identifier questions asked in Arabic about English rows (Latin inside Arabic).
LATIN_IN_ARABIC = (
    ("csv-0", "بكام WS-0035؟", 'WS-0035,Sofa 35,"2,615"'),
    ("csv-1", "عايز سعر WS-1010", 'WS-1010,Armchair 10,"10,559"'),
    ("csv-0", "WS-0005 متوفر؟", 'WS-0005,Wardrobe 5,"7,045"'),
)


def _blind(text: str) -> str:
    return " ".join(word for word in text.split() if not any(ch.isdigit() for ch in word))


class IdentifierBlindEmbeddings(FakeEmbeddings):
    """The suite's bag of words, deaf to every token holding a digit."""

    async def embed_batch(self, texts: Any) -> Any:
        return await super().embed_batch([_blind(text) or "." for text in texts])


async def blind_provider(body: dict[str, Any]) -> httpx.Response:
    dimensions = int(body.get("dimensions", 1536))
    data = [
        {
            "object": "embedding",
            "index": index,
            "embedding": embed_text(_blind(item) or ".", dimensions=dimensions),
        }
        for index, item in enumerate(body["input"])
    ]
    return httpx.Response(
        200,
        content=json.dumps(
            {"object": "list", "data": data, "usage": {"prompt_tokens": 3, "total_tokens": 3}}
        ).encode(),
        headers={"content-type": "application/json"},
    )


def _supports(name: str) -> bool:
    return name in inspect.signature(RetrievalService.search).parameters


def _policy(hybrid: bool) -> dict[str, Any]:
    if "policy" not in inspect.signature(RetrievalService.__init__).parameters:
        return {}
    from app.services.retrieval_policy import RetrievalPolicy

    return {"policy": RetrievalPolicy(hybrid=hybrid)}


@dataclass
class Part2Run:
    metrics: dict[str, Any] = field(default_factory=dict)

    def table(self, label: str) -> str:
        lines = [f"### {label} (part 2)", "", "| Metric (fake embeddings - structural) | Value |"]
        lines.append("|---|---|")
        lines += [f"| {k} | {v} |" for k, v in self.metrics.items() if not k.startswith("_")]
        return "\n".join(lines) + "\n"


async def _search(
    indexing: Indexing,
    tenant_id: uuid.UUID,
    query: str,
    *,
    lifted: bool,
    embeddings: Any = None,
    hybrid: bool = True,
    top_k: int = TOP_K,
    alternate: str | None = None,
) -> Any:
    with pytest.MonkeyPatch.context() as patch:
        if lifted:
            patch.setattr(retrieval_module, "MAX_DISTANCE", 2.0)
        async with indexing.database.session() as session:
            service = RetrievalService(
                session=session,
                tenant_id=tenant_id,
                embeddings=as_embeddings(embeddings or FakeEmbeddings()),
                **_policy(hybrid),
            )
            extra: dict[str, Any] = {}
            if alternate is not None and _supports("alternate_query"):
                extra["alternate_query"] = alternate
            return await service.search(
                query=query, top_k=top_k, max_distance=2.0 if lifted else 0.75, **extra
            )


def _holds(retrieval: Any, span: str) -> bool:
    wanted = comparable(span)
    return any(wanted in comparable(p.content) for p in retrieval.passages)


async def measure_part2(indexing: Indexing, tenant_id: uuid.UUID, run: EvalRun) -> Part2Run:
    """Part 2's metrics over the workspace `rag_eval.evaluate` built, plus a blind one."""
    result = Part2Run()
    _, questions = load()
    answerable = [q for q in questions if q["doc"] is not None]

    # Lexical channel alone.
    lexical = "n/a (no lexical channel)"
    if hasattr(DocumentChunkRepository, "lexical_search"):
        from app.services.retrieval_ranking import lexical_tokens, tsquery_text

        found = 0
        async with indexing.database.session() as session:
            chunks = DocumentChunkRepository(session, tenant_id=tenant_id)
            for question in answerable:
                tsquery = tsquery_text(lexical_tokens(question["q"]))
                if tsquery is None:
                    continue
                rows = await chunks.lexical_search(
                    tsquery=tsquery,
                    embedding=embed_text(question["q"]),
                    space=FakeEmbeddings().space,
                    limit=TOP_K,
                )
                found += int(
                    any(comparable(question["span"]) in comparable(r.chunk.content) for r in rows)
                )
        lexical = f"{found} / {len(answerable)}"
    result.metrics["spans retrievable by exact words (lexical channel alone, top 5)"] = lexical

    # Follow-ups and cross-lingual questions.
    cases = [
        json.loads(line)
        for line in (PART2 / "contract.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for kind in ("follow-up", "cross-lingual"):
        typed = contract = 0
        subset = [c for c in cases if c["kind"] == kind]
        for case in subset:
            typed += int(
                _holds(await _search(indexing, tenant_id, case["typed"], lifted=True), case["span"])
            )
            contract += int(
                _holds(
                    await _search(
                        indexing,
                        tenant_id,
                        case["standalone"],
                        lifted=True,
                        alternate=case.get("alternate"),
                    ),
                    case["span"],
                )
            )
        alternate = (
            ""
            if _supports("alternate_query") or kind == "follow-up"
            else " (alternate_query unsupported)"
        )
        result.metrics[f"{kind} questions answered: as typed / query contract (top 5)"] = (
            f"{typed} / {len(subset)} -> {contract} / {len(subset)}{alternate}"
        )

    # Two-chunk answers.
    async with indexing.database.session() as session:
        chunks_by_doc: dict[uuid.UUID, list[DocumentChunk]] = {}
        for chunk in await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.tenant_id == tenant_id)
            .order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
        ):
            chunks_by_doc.setdefault(chunk.document_id, []).append(chunk)
        titles = {
            d.id: d.title
            for d in await session.scalars(select(Document).where(Document.tenant_id == tenant_id))
        }
    pairs = []
    for _document_id, chunk_rows in sorted(
        chunks_by_doc.items(), key=lambda item: titles.get(item[0], "")
    ):
        for first, second in pairwise(chunk_rows):
            if first.heading_path and first.heading_path == second.heading_path:
                pairs.append((first, second))
    complete = 0
    complete_one = 0
    for first, second in pairs:
        head = " ".join(first.content.split()[-12:])
        tail = " ".join(second.content.split()[-8:])
        # At max_results=1 as well: there the two chunks cannot both arrive as
        # separate passages, so only merging and neighbour expansion deliver
        # the continuation (FX-3.11).
        single = await _search(indexing, tenant_id, head, lifted=True, top_k=1)
        single_context = (
            comparable(
                " ".join(
                    source["content"]
                    for source in json.loads(single.as_context())["knowledge_sources"]
                )
            )
            if single.passages
            else ""
        )
        complete_one += int(
            comparable(head) in single_context and comparable(tail) in single_context
        )
        found = await _search(indexing, tenant_id, head, lifted=True)
        # What the model reads: the sources' contents, decoded from the JSON.
        context = (
            comparable(
                " ".join(
                    source["content"]
                    for source in json.loads(found.as_context())["knowledge_sources"]
                )
            )
            if found.passages
            else ""
        )
        whole = comparable(head) in context and comparable(tail) in context
        complete += int(whole)
        if os.environ.get("WASLA_RAG_EVAL_DEBUG") and not whole:
            print(  # noqa: T201
                "TWO-CHUNK MISS",
                titles.get(first.document_id),
                first.ordinal,
                comparable(head) in context,
                comparable(tail) in context,
                [(p.document_title, p.ordinals) for p in found.passages],
            )
    result.metrics["two-chunk answers delivered complete (max_results=5)"] = (
        f"{complete} / {len(pairs)}"
    )
    result.metrics["two-chunk answers delivered complete (max_results=1)"] = (
        f"{complete_one} / {len(pairs)}"
    )

    # Delivered vs requested.
    top = retrieval_module.MAX_TOP_K
    kept = delivered = 0
    for question in answerable:
        found = await _search(indexing, tenant_id, question["q"], lifted=True, top_k=top)
        if not found.passages:
            continue
        kept += min(top, len(found.passages))
        delivered += len(json.loads(found.as_context())["knowledge_sources"])
    result.metrics[f"passages delivered vs kept at max_results={top}"] = f"{delivered} / {kept}"

    # Identifier questions in a workspace an identifier-blind model indexed.
    manifest, _ = load()
    blind_questions = [
        q for q in questions if q["doc"] in IDENTIFIER_DOCS and q["lang"] in ("en", "en-sku")
    ]
    previous = indexing.provider.handler
    indexing.provider.handler = blind_provider
    try:
        blind_tenant = await indexing.workspace()
        blind_run = EvalRun()
        await ingest(indexing, blind_tenant, blind_run, only=set(IDENTIFIER_DOCS))
    finally:
        indexing.provider.handler = previous
    blind = IdentifierBlindEmbeddings()
    asked = [(q["q"], q["span"], q["lang"]) for q in blind_questions] + [
        (q, span, "latin-in-arabic") for _, q, span in LATIN_IN_ARABIC
    ]
    for label, hybrid in (("vector channel only", False), ("hybrid", True)):
        if hybrid and not hasattr(DocumentChunkRepository, "lexical_search"):
            result.metrics[f"exact-identifier questions answered at 0.75 ({label})"] = "n/a"
            continue
        counts: dict[str, list[int]] = {}
        for query, span, lang in asked:
            got = await _search(
                indexing, blind_tenant, query, lifted=False, embeddings=blind, hybrid=hybrid
            )
            counts.setdefault(lang, [0, 0])
            counts[lang][0] += int(_holds(got, span))
            counts[lang][1] += 1
        total = sum(v[0] for v in counts.values())
        detail = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(counts.items()))
        result.metrics[f"exact-identifier questions answered at 0.75 ({label})"] = (
            f"{total} / {len(asked)} ({detail})"
        )
    result.metrics["_raw"] = {"lexical": lexical, "pairs": len(pairs), "complete": complete}
    return result
