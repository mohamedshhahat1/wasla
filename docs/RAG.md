# Knowledge Base and RAG

**Status: Implemented** — ingestion, tenant-scoped retrieval and the `search_knowledge` tool exist and are exercised against real PostgreSQL with pgvector. PDF extraction arrived with phase 9, and each search is metered as a `rag_query` (phase 12). An approximate vector index (HNSW, migration 0039) exists and is measured. See [../TASKS.md](../TASKS.md) phase 6. Storage decision: ADR-008. Embedding width: ADR-018. Queue separation: ADR-019.

Scope: knowledge sources, ingestion, embeddings, and tenant-scoped retrieval.

## Knowledge sources

Per-tenant isolated knowledge bases. A workspace may keep several — sales material apart from support policies — and point different agents at different sets; a workspace that does not care keeps one and never thinks about it again, since the first upload creates it.

Plain text, Markdown and PDF are ingested. Markdown keeps its punctuation, because headings and lists are structure the chunker uses. A PDF is submitted base64-encoded, since this endpoint takes JSON and a PDF is not text.

A **scanned** PDF — a photograph of a page, with no text layer — is refused with a message saying so rather than stored empty. A document that looks perfectly ingested from the outside and answers every question with nothing is worse than one rejected with a reason, and the two are distinguishable: an empty extraction means a valid PDF with nothing to read, where a raised error means the bytes were not a PDF at all. Extraction is shared with inbound WhatsApp documents ([MEDIA.md](MEDIA.md)); there is no OCR.

## Ingestion pipeline

```
Submit (202)  ->  Document row, PENDING  ->  knowledge:ingestion queue
                                                     |
                                            IngestionWorker
                                                     |
                          extract -> chunk -> embed (batched) -> store
                                                     |
                                            Document READY
```

Nothing is embedded in the request that submitted the document. Extraction, chunking and embedding call a provider and a large document is dozens of requests, so the endpoint answers `202` with a `pending` document and the client polls until `ready`.

**Chunking** splits on paragraph structure first and by character count only when a single paragraph exceeds the budget on its own, falling back to sentence boundaries (including the Arabic full stop) before cutting by length. A cut mid-sentence produces a chunk that embeds as neither of the two ideas it straddles. Chunks overlap by a fixed number of characters so an answer sitting across a boundary is reachable from either side.

**Idempotency** is by SHA-256 of the extracted text, unique per knowledge base. Submitting the same text twice returns the existing document with `created: false` — recognised, not duplicated. Re-ingesting deletes a document's chunks and writes new ones rather than appending, which is what makes a duplicated queue job harmless; a document already `READY` is skipped entirely and costs no embedding calls.

**Failure** is recorded on the document, not swallowed: `status` becomes `failed`, `error` carries the reason for whoever has to fix it, and `chunk_count` is zeroed because a failed run leaves nothing retrievable. `POST /knowledge/documents/{id}/ingest` queues it again once the cause is fixed. A document stranded `pending` by a Redis outage is findable through `DocumentRepository.list_pending`.

Ingestion runs on its own queue and worker, separate from the agent queue (ADR-019), so a bulk upload cannot sit in front of a customer waiting for a reply.

## Storage

PostgreSQL with pgvector. `knowledge_bases` groups documents; `documents` holds source metadata, the extracted text and the ingestion status; `document_chunks` holds chunk text, ordinal, token estimate and the embedding vector.

`tenant_id` is on all three tables, including chunks, even though it could be reached by joining through the document. Similarity search *does* join the document — it needs the title to cite and the status to filter on — so both predicates are applied and either one alone would be sufficient. The duplication is kept deliberately: a filter that depends on a join is a filter someone eventually writes without the join, and the column costs four bytes against a leak that would be silent. Removing either is safe; removing both is a cross-tenant leak, and that is the case the tests fail on.

The embedding column is `vector(1536)`, the width of `text-embedding-3-small`, fixed in the schema rather than configurable (ADR-018). The width is also requested explicitly on every embedding call, so the provider cannot return something the column will not accept.

### The approximate index

`ix_document_chunks_embedding_hnsw` is an HNSW index with `vector_cosine_ops`, built by migration 0039 (ADR-079).

**HNSW rather than IVFFlat.** IVFFlat trains its lists against the rows present when it is built, and a knowledge base is empty when a workspace is created and fills up over months. An index whose recall depends on when it was built would be wrong for most of this table's life, and rebuilding it on a schedule is an operational commitment nobody asked for. HNSW has no training step. Defaults for `m` (16) and `ef_construction` (64) were kept because raising either bought nothing measurable and both cost build time on the table this system writes to most.

**`search` sets two GUCs per query, and the index is worse than useless without them.**

`hnsw.iterative_scan = strict_order`. pgvector indexes one column, so the tenant, knowledge-base and READY filters are applied *after* the index has picked its candidates. By default the scan visits `ef_search` candidates in global distance order and answers with whichever survive - which for a workspace holding a small share of the corpus is close to none. Measured: a 200-chunk workspace in a 32,000-chunk table got **zero passages out of five**, and the agent was then told the knowledge base had no answer.

`plan_cache_mode = force_custom_plan`. The retrieval statement is prepared once per pooled connection, and after five executions PostgreSQL weighs a generic plan against the custom ones. A generic plan cannot know which workspace is asking, so it estimates the tenant filter from the average workspace and picks a nested loop over every document. Measured through `DocumentChunkRepository.search` on a 45,000-chunk workspace: searches one to five took 7ms and every search afterwards took 250ms - the same query, on the same connection, having simply been run often enough.

### Measured behaviour

Local drill, not CI: PostgreSQL 16.14, pgvector 0.8.6, 77,000 chunks across 36 workspaces (one of 45,000, one of 12,000, a tail down to 200), `shared_buffers` 128MB. Twelve searches per arm through the repository, one process per arm so no pooled connection carries a plan decision between them.

| workspace | chunks | before | after | plan after |
| --- | --- | --- | --- | --- |
| enterprise | 45,000 | 236ms (steady 240ms) | **7.9ms** | `Index Scan using ix_document_chunks_embedding_hnsw` |
| mid | 12,000 | 53ms | 54ms | bitmap on `(tenant_id, knowledge_base_id)` + top-N sort |
| small | 200 | 6.8ms | 7.5ms | nested loop from `documents` |

**The planner chooses per query, and that is the design rather than a limitation.** Its cost crossover sits at roughly 26,000 retrievable chunks in one workspace: below it the exact scan is chosen and is correct and cheap; above it the approximate index is chosen and the exact scan would have kept growing linearly for ever. Forcing the approximate path everywhere was measured and rejected - on the 200-chunk workspace it costs 54ms against 1.5ms, because the scan spends its whole budget discarding other workspaces' vectors.

The band between roughly 3,000 and 26,000 chunks is where PostgreSQL keeps the exact scan and the approximate one would have been faster (2.4ms against 42ms, forced, at 12,000). That is left alone deliberately: the alternative is a size threshold in the repository choosing between two query shapes, and a wrong threshold is a silent retrieval-quality regression rather than a slow query. The 0.7ms the two GUCs cost the small workspace is the price of the correctness they buy.

Recall at 45,000 chunks is 1.000 over 20 queries - the approximate answer is the exact answer. On the deliberately tight 1,000-chunk corpus the suite uses, id overlap is 0.80 while the furthest passage returned is within 0.36% of the exact answer's furthest, which is near-tie shuffling rather than lost recall; the test asserts the distance property for that reason.

The index is 597MB for 77,000 chunks, roughly 8KB per chunk, and takes ~14 minutes to build at that size. Both matter operationally and are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Retrieval flow

```
Question -> embedding -> tenant-filtered vector search -> distance threshold
  -> passages (or an explicit "nothing found") -> agent context -> Responses API -> answer
```

Three filters apply to every search and none is optional: the `tenant_id` predicate, a join to the document restricted to `READY`, and a non-null embedding. Results are ordered by cosine distance and then cut at a distance threshold, so an empty knowledge base returns nothing rather than the least-bad match in it.

Cross-tenant retrieval is prohibited and explicitly tested, including the case where a caller names another workspace's knowledge base id directly.

## What the agent sees

Retrieval reaches agents only through the `search_knowledge` tool, granted per agent — a booking agent need not read the price list. The tool takes the customer's question and an optional result count; the tenant id comes from the tool context, never from an argument, because a tenant id a model could supply is a tenant id a model could change.

When nothing is found the tool returns a sentence, not a blank:

> No information about this was found in the company's knowledge base. Tell the customer you do not have that information rather than guessing, and offer to pass the question to a colleague.

That wording is load-bearing. A model handed an empty string fills the silence from its training data, which is exactly the invented answer grounding exists to prevent. There is a test asserting the empty result reaches the model as an instruction rather than as silence.

If no embedding provider is configured, the tool says so and tells the agent not to guess — a missing configuration is ours, not the model's mistake, and saying so lets it fall back to a handoff instead of retrying a tool that cannot work.

## Testing

Retrieval behaviour is tested against real PostgreSQL and real pgvector. The embedding *model* is faked (`tests/fake_embeddings.py`, a hashed bag of words); the vector *search* is not — chunks go into a real `vector` column and come back ordered by real cosine distance. That is the only way to prove the property that matters most: that one company's question cannot reach another company's documents.
