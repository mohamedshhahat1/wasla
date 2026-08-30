# Knowledge Base and RAG

**Status: Implemented** — ingestion, tenant-scoped retrieval and the `search_knowledge` tool exist and are exercised against real PostgreSQL with pgvector. PDF extraction arrived with phase 9, and each search is metered as a `rag_query` (phase 12). An approximate vector index is Planned. See [../TASKS.md](../TASKS.md) phase 6. Storage decision: ADR-008. Embedding width: ADR-018. Queue separation: ADR-019.

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

**No approximate index exists yet.** ivfflat and hnsw need to be built against representative data to be worth anything — ivfflat in particular wants its list count chosen from the row count, and one built on an empty table produces a bad plan that survives until someone reindexes. Exact search is correct at every size and fast at the sizes a new workspace has; choosing an index belongs with the Phase 14 performance pass.

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
