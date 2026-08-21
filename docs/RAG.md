# Knowledge Base and RAG

**Status: Planned** — no ingestion or retrieval code exists yet. See [../TASKS.md](../TASKS.md) phase 6. Storage decision: ADR-008.

Scope: knowledge sources, ingestion, embeddings, and tenant-scoped retrieval.

## Knowledge sources

Per-tenant isolated knowledge bases containing company information, FAQs, products, prices, policies, PDFs, documents, attachments, and plain text.

## Ingestion pipeline

```
Upload -> validate -> extract text -> chunk -> generate embeddings
  -> store chunks + vectors -> associate with tenant -> index
```

Ingestion runs in background workers and is idempotent. Documents track processing state so failures are visible and retryable. Large files are not stored in PostgreSQL; a storage abstraction covers future object storage.

## Storage

PostgreSQL with pgvector. `documents` holds source metadata and status; `document_chunks` holds chunk text, ordering, token counts, embedding vectors, and `tenant_id`. Indexes cover `tenant_id` and vector similarity.

## Retrieval flow

```
Question -> embedding -> tenant-filtered vector search -> top-k chunks
  -> agent context -> Responses API -> answer
```

Every similarity query includes a mandatory `tenant_id` predicate; cross-tenant retrieval is prohibited and explicitly tested. Retrieval is exposed to agents only through the `search_knowledge` tool, and RAG queries are metered as usage events. Embedding model and dimensions are configuration, not literals scattered in code.
