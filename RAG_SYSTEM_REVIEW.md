# 🧠 Wasla RAG System — Code-Level Review, Bug Report, Fix Plan & Improvements

> [!IMPORTANT]
> This review was written from the **code**, not from the docs. Every claim below cites the file and
> line it comes from, and the most important ones were **reproduced by running the committed code**
> (see [Appendix A](#appendix-a--reproductions-and-their-output)). Where the docs disagree with the
> code, the code wins and the disagreement is listed in [§8](#8-docs-vs-code-where-they-disagree).
> No application code was changed.

| | |
|---|---|
| **Repository / branch** | `mohamedshhahat1/wasla` · `worktree-billing-google-auth` |
| **Commit reviewed** | `7cdf863` (2026-10-25) |
| **Review date** | 2026-10-29 |
| **Scope** | Ingestion (upload → extraction → normalisation → chunking → embeddings → publish), retrieval (agent tool → query → vector search → filters → threshold → context), re-indexing and document updates, workers, migrations, tests; improvement recommendations for accuracy, answer correctness, speed and cost under strict tenant isolation |
| **Method** | Full read of ~25 RAG files + experiments against the real modules, a real PostgreSQL 16 cluster, and a Chromium-generated Arabic PDF |
| **Verdict** | 🟢 **Plumbing is robust** (isolation, failure handling, atomic swaps) · 🔴 **Answer quality and document lifecycle are not**: Arabic PDFs can be indexed as unsearchable glyph forms, updated documents are duplicated instead of replaced, and the chunker corrupts numbers and words |

---

## 📑 Contents

0. [TL;DR](#0-tldr)
1. [How this review was done](#1-how-this-review-was-done)
2. [System map](#2-system-map)
3. [Ingestion: how a document becomes searchable](#3-ingestion-how-a-document-becomes-searchable)
4. [Retrieval: how an answer finds its passages](#4-retrieval-how-an-answer-finds-its-passages)
5. [Re-indexing and updated documents — replaced or duplicated?](#5-re-indexing-and-updated-documents--replaced-or-duplicated)
6. [Root causes of poor answers](#6-root-causes-of-poor-answers)
7. [Bug catalogue](#7-bug-catalogue)
8. [Docs vs code: where they disagree](#8-docs-vs-code-where-they-disagree)
9. [What is done well (keep it)](#9-what-is-done-well-keep-it)
10. [Fix plan](#10-fix-plan)
11. [Improvement recommendations](#11-improvement-recommendations)
- [Appendix A — Reproductions and their output](#appendix-a--reproductions-and-their-output)
- [Appendix B — Every tunable, in one table](#appendix-b--every-tunable-in-one-table)
- [Appendix C — Operator SQL for triage](#appendix-c--operator-sql-for-triage)
- [Appendix D — Glossary](#appendix-d--glossary)

**Severity legend:** 🔴 Critical · 🟠 High · 🟡 Medium · 🔵 Low · ⚪ Info/scale

**Finding types:** `Bug` (code does the wrong thing or contradicts its own intent) · `Gap` (missing capability that degrades answers) · `Risk` (deployment/scale hazard) · `Doc` (docs or comments say something the code does not do)

---

## 0. TL;DR

### The five things that most hurt answer quality

| # | Root cause | Effect a customer sees | Evidence |
|---|---|---|---|
| **RC-1** | 🔴 **Arabic text is never Unicode-normalised.** pypdf can return Arabic PDF text as *presentation-form glyphs* (U+FE70–U+FEFF) — it did for the Chromium-generated PDF tested; how often depends on the PDF producer — and nothing applies NFKC. Mixed Arabic + number lines also came out in scrambled order. | "I don't have that information" for questions an Arabic PDF clearly answers; garbled quotes. | Reproduced: **0 of 8** query words match the indexed text before NFKC, **8 of 8** after ([A.3](#a3-arabic-pdf-extraction)) |
| **RC-2** | 🔴 **There is no "update document" operation.** Uploading a new version creates a *second* document; both are served. "Re-index" only re-processes the text stored at first upload. | Old and new prices/policies returned side by side, at the same distance, with nothing telling the model which is current. | Reproduced on real PostgreSQL: search returned `7900 EGP` **and** `7200 EGP` ([A.5](#a5-update--duplication-demo-on-real-postgresql)) |
| **RC-3** | 🟠 **The chunker cuts words and numbers in half.** Unpunctuated text (lists, tables, much Arabic prose) is hard-cut every 1,000 chars; the 150-char overlap is cut at any character and glued on with a blank line. | Passages like `price 13\n\n,795 EGP`; headings separated from their sections; junk chunks. | Reproduced ([A.1](#a1-chunker), [A.2](#a2-hard-split-and-overlap-glue)) |
| **RC-4** | 🟠 **Retrieval is single-stage vector top-k with an uncalibrated cutoff.** No lexical/hybrid channel, no re-ranker, no dedupe/MMR, no neighbour expansion, and no distance telemetry to calibrate `0.75`. | Exact terms (SKUs, model numbers, names, prices) missed; duplicate passages fill the 4–5 slots that fit. | Code: `retrieval_service.py:291`, `telemetry.py:977` |
| **RC-5** | 🟡 **The query is whatever the model writes "in the customer's own words".** No rewrite, no reference resolution, no cross-lingual help. | Egyptian-dialect or pronoun-only questions ("بكام ده؟", "how much is it?") retrieve poorly, especially against English or MSA documents. | `agents/registry.py:560` |

### Re-index verdict (the specific question asked)

| Scenario | Replaced? | Notes |
|---|:-:|---|
| Re-index the **same** document (`POST /documents/{id}/ingest`) | ✅ **Replaced** | Atomic swap; old generation's chunks deleted in the publish transaction; DB allows one active generation per document. Old version keeps serving until the new one publishes. |
| Upload an **updated version** (new text) | ❌ **Duplicated** | New `content_hash` ⇒ new `Document`; both `ready`, both searched. No update/replace endpoint exists. |
| Same text in **two knowledge bases** | ❌ **Duplicated** | Dedupe key is per knowledge base, retrieval is workspace-global. |
| Re-upload identical text with a **corrected title** | ⚠️ Silently ignored | Old title kept; the model keeps seeing the old title. |
| **Embedding model changed** | ⚠️ Not duplicated, but **hidden** | Every document disappears from retrieval until re-indexed; the batch re-index command cannot page past its first batch. |

### Bug count

| 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low | ⚪ Info | Total |
|:-:|:-:|:-:|:-:|:-:|:-:|
| 2 | 5 | 8 | 10 | 2 | **27** |

### Biggest improvement levers ([§11](#11-improvement-recommendations))

| Goal | Best lever | Why it is the best lever here |
|---|---|---|
| 🎯 Retrieval accuracy | [IMP-03](#imp-03) normalise Arabic, [IMP-04](#imp-04) structure-aware chunks, [IMP-08](#imp-08) hybrid search | They fix what is *in* the index; ranking work on corrupted chunks is wasted. |
| ✅ Fewer wrong / incomplete answers | [IMP-06](#imp-06) replace-in-place, [IMP-13](#imp-13) tool-loop fixes, [IMP-14](#imp-14) number grounding | Stale duplicates, empty turns and unverified prices are the costliest visible errors. |
| ⚡ Response speed | [IMP-17](#imp-17) pre-retrieval, [IMP-18](#imp-18) sentiment off the critical path | Each removes a full model round trip; the database search is already 2–54 ms. |
| 💰 Cost | [IMP-02](#imp-02) cached-token accounting, [IMP-17](#imp-17), [IMP-22](#imp-22) cache-friendly prompts | Spend is dominated by re-sent prompts across rounds, not by embeddings. |
| 🛡️ Isolation | [IMP-23](#imp-23) isolation test gate, [IMP-24](#imp-24) row-level security | Every recommendation adds a query path; each must fail closed. |

---

## 1. How this review was done

**Read** (in full unless noted): `app/db/models/knowledge.py`, `app/repositories/knowledge_repository.py`, `app/services/{knowledge_service,document_indexing,chunking,extraction,pdf_extract_child,knowledge_limits,retrieval_service}.py`, `app/integrations/openai/embeddings.py`, `app/core/{embedding_space,text_safety}.py`, `app/agents/{registry,orchestrator}.py` (RAG paths), `app/workers/{ingestion_worker,ingestion_queue,ingestion_recovery}.py`, the knowledge commands in `app/workers/queues.py`, `app/api/v1/knowledge.py`, `app/schemas/knowledge.py`, and the relevant parts of `app/api/dependencies.py`, `app/services/{entitlement_service,agent_service}.py`, `app/core/{config,telemetry}.py`, `app/db/session.py`, `scripts/db_preflight.py`, migrations `0001` and `0039`, and the RAG test suites (test names and the update/re-index tests in full). The previous audit (`RAG_FINDINGS_REMEDIATION.md`) and `docs/RAG.md` were read only to compare their claims with the code.

**Executed** (scripts kept outside the repo; nothing in the repo modified):

| Check | Result |
|---|---|
| RAG unit suites (`test_chunking`, `test_extraction`, `test_retrieval_authority`, `test_embeddings_*`, `test_knowledge_*`, `test_pdf_extraction_bounds`) | ✅ **182 passed** |
| RAG integration suites on PostgreSQL 16.15 + **pgvector 0.6.0** (`test_knowledge_rag`, `test_rag_integrity`) | 26 passed, **12 failed — every failure is a search, each on `invalid configuration parameter name "hnsw.iterative_scan"`** ([RV-03](#rv-03)) |
| Chunker probes against the committed `app.services.chunking` | junk chunk, orphaned heading, mid-word and mid-number cuts **reproduced** |
| Arabic PDF rendered by Chromium → the project's real `extract_pdf_bounded` | **104 presentation-form characters**, logical text absent until NFKC, bidi-scrambled number line |
| Update / duplicate demo with the real `KnowledgeService` + indexing pipeline + repository search on PostgreSQL | old and new versions **both served** |

> [!NOTE]
> **Limits of the evidence.** There is no OpenAI key here, so retrieval *quality* with the real embedding model was not measured (the project's own tests use a hashed bag-of-words fake). Building pgvector ≥ 0.8 from source was blocked in this environment, so the approximate (HNSW iterative-scan) path was not exercised; the demo ran the committed search query with the two planner settings skipped, and says so. Findings marked *code reading* were not executed.

---

## 2. System map

### 2.1 Components

| Layer | File | Responsibility |
|---|---|---|
| API | `app/api/v1/knowledge.py` | 8 routes under `/api/v1/knowledge` (create/list KB, submit/list/get/re-index/delete document) |
| Contracts | `app/schemas/knowledge.py` | Request/response models; text-safety validators |
| Service | `app/services/knowledge_service.py` | Submit (extract, hash, dedupe, create generation, enqueue), re-index, delete, views |
| Extraction | `app/services/extraction.py`, `pdf_extract_child.py` | PDF → text in a killable child process (pypdf); byte decoding for customer files |
| Limits | `app/services/knowledge_limits.py` | Every size/time bound in one place |
| Chunking | `app/services/chunking.py` | Whitespace normalisation, paragraph/sentence packing, overlap |
| Indexing | `app/services/document_indexing.py` | Claim → embed (no tx) → publish/failure state machine |
| Embeddings | `app/integrations/openai/embeddings.py` | HTTP client to OpenAI `/embeddings`, retries, strict vector validation |
| Space | `app/core/embedding_space.py` | `(provider, model, dimensions, schema_version)` identity of a vector space |
| Data | `app/db/models/knowledge.py` | 4 tables, partial unique indexes, HNSW index |
| Data access | `app/repositories/knowledge_repository.py` | Tenant-scoped repos, the similarity search, recovery/backlog sweeps |
| Workers | `app/workers/ingestion_worker.py`, `ingestion_queue.py`, `ingestion_recovery.py` | Redis-backed queue, consumer, 60-second recovery sweep |
| Ops CLI | `app/workers/queues.py` | `unindexed-documents`, `failed-documents`, `stale-embeddings`, `reindex-*` |
| Retrieval | `app/services/retrieval_service.py` | Query embedding, threshold, context packing, failure containment |
| Agent | `app/agents/registry.py`, `orchestrator.py` | `search_knowledge` tool, tool-calling loop (3 rounds) |

### 2.2 End-to-end flow

```mermaid
flowchart TD
    subgraph Ingestion
        U["Admin uploads<br/>POST /knowledge/bases/:kb/documents"] --> L["DocumentSlotDep<br/>advisory lock + plan count"]
        L --> X{"source"}
        X -->|text / markdown| N["refuse NUL / surrogates<br/>normalise whitespace"]
        X -->|pdf as base64| P["extract_pdf_bounded<br/>child process, pypdf<br/>40 pages, 20 s"]
        P --> N
        N --> H["SHA-256 of text<br/>dedupe per knowledge base"]
        H -->|new hash| D["Document + generation 1 (pending)"]
        H -->|known hash| R["resubmission: retry only if last attempt failed"]
        D --> Q[("Redis ingestion queue")]
        Q --> W["IngestionWorker"]
        S["Recovery sweep, every 60 s"] --> Q
        W --> C1["TX1 claim: token, attempts+1, space"]
        C1 --> E["chunk, then per 96 chunks:<br/>renew, embed, meter"]
        E --> C2["TX2 publish: insert chunks,<br/>supersede old generation, delete its chunks,<br/>activate new"]
        E -. failure .-> C3["TX3 record failure:<br/>retry later or failed"]
    end
    subgraph Retrieval
        M["Customer message"] --> O["Orchestrator, up to 3 model rounds"]
        O -->|model calls search_knowledge| T["RetrievalService.search"]
        T --> QE["embed query (session released)"]
        QE --> SQL[("pgvector cosine top-k<br/>tenant x3, ACTIVE, same space")]
        SQL --> F["distance at most 0.75<br/>context at most 6,000 chars"]
        F --> J["JSON knowledge_sources to the model"]
    end
    C2 --> SQL
```

### 2.3 Data model

```mermaid
erDiagram
    TENANTS ||--o{ KNOWLEDGE_BASES : owns
    KNOWLEDGE_BASES ||--o{ DOCUMENTS : contains
    DOCUMENTS ||--o{ DOCUMENT_INDEX_GENERATIONS : "indexed as"
    DOCUMENT_INDEX_GENERATIONS ||--o{ DOCUMENT_CHUNKS : owns
    KNOWLEDGE_BASES {
        uuid id PK
        uuid tenant_id
        string name "unique per tenant"
    }
    DOCUMENTS {
        uuid id PK
        uuid tenant_id
        uuid knowledge_base_id
        string title
        enum source "text, markdown, pdf"
        enum status "pending, processing, ready, failed"
        string content_hash "unique per tenant and KB"
        text content "extracted text, the only copy"
        int chunk_count
    }
    DOCUMENT_INDEX_GENERATIONS {
        uuid id PK
        uuid document_id
        bigint number
        enum state "pending, processing, active, superseded, failed"
        uuid claim_token "fencing token"
        int attempts
        string embedding_model "space identity"
    }
    DOCUMENT_CHUNKS {
        uuid id PK
        uuid generation_id
        uuid knowledge_base_id
        int ordinal
        text content
        vector embedding "1536, HNSW cosine"
    }
```

**Constraints that matter** (`app/db/models/knowledge.py`):

| Constraint | Line | Meaning |
|---|---|---|
| `uq_documents_tenant_id_knowledge_base_id_content_hash` | 159–165 | Same text twice **in one KB** = one document. Different text, or another KB = new document. |
| `uq_document_index_generations_active` (partial, `state='active'`) | 278–283 | At most one served generation per document. |
| `uq_document_index_generations_in_flight` (partial) | 287–292 | At most one pending/processing attempt per document (coalescing). |
| Composite FKs chunk → document → KB, chunk → generation | 405–428 | A chunk cannot claim a foreign parent. |
| `ix_document_chunks_embedding_hnsw` (`vector_cosine_ops`) | 390–395 | ANN index; filters are applied *after* the index (hence iterative scan). |

There is **no** column for a stable document identity (external id, source key, filename uniqueness), no version number, no effective date and no "supersedes" link. The original file bytes are not stored (`db/models/knowledge.py:153-155`).

---

## 3. Ingestion: how a document becomes searchable

### 3.1 The upload request

`POST /api/v1/knowledge/bases/{kb}/documents` (`app/api/v1/knowledge.py:101-129`), admin only, body `{title, content, source, filename?, media_type?}`:

1. **Entitlement lock first.** `DocumentSlotDep` (`app/api/dependencies.py:906`) runs before the handler and takes `pg_advisory_xact_lock` for the workspace's document limit (`entitlement_service.py:789-791`), held until the request commits.
2. **Validation.** `content` ≤ 400,000 chars; title/filename/media type storable; title visible (`knowledge_service.py:267-275`).
3. **Extraction** (`knowledge_service.py:129-158`) — runs *inside* the request, under the lock from step 1:
   - `text` / `markdown`: refuse NUL and lone surrogates, then `chunking.normalise` (whitespace only). **Markdown is treated exactly like text.**
   - `pdf`: base64-decode → `extract_pdf_bounded` → empty text is refused ("may be a scan") → `chunking.normalise`.
4. **Hash and dedupe** (`knowledge_service.py:286-319`): SHA-256 of the normalised text, looked up **within the same knowledge base**:
   - not found → new `Document` (`status=pending`, `content=text`) + generation 1 (`trigger=submitted`) + enqueue;
   - found → lock it; start a new generation **only if the latest attempt failed**; otherwise nothing changes (not even the title).
5. Audit row + `202 Accepted`. The client polls `GET /documents/{id}`.

### 3.2 PDF extraction

`app/services/extraction.py:96-164` starts `pdf_extract_child.py` as a separate interpreter with the PDF on stdin, an environment with no secrets, `RLIMIT_AS` 768 MiB, a CPU limit, and a 20-second wall clock (killed on timeout). At most 2 extractions run per process.

Inside the child (`pdf_extract_child.py:64-108`): `PdfReader` → decrypt with an empty password if needed → refuse > 40 pages before reading → `page.extract_text()` per page (default *plain* mode) while counting characters → pages joined with `"\n\n"` → NUL removed and surrogates replaced (`_clean`, line 58).

**What it does not do:** Unicode normalisation (NFKC), Arabic shaping/bidi repair, header/footer/page-number removal, layout-aware table handling, OCR. Those gaps are [RV-01](#rv-01), [RV-04](#rv-04) and [RV-22](#rv-22).

### 3.3 The indexing state machine

```mermaid
stateDiagram-v2
    [*] --> pending: submit or reindex
    pending --> processing: claim (new token, attempts + 1, space recorded)
    processing --> active: publish (atomic swap)
    processing --> pending: transient failure, backoff
    processing --> pending: workspace suspended or renewal refused
    processing --> failed: permanent failure
    processing --> failed: transient, budget of 5 spent
    pending --> failed: claimed with budget spent
    active --> superseded: newer generation published
    failed --> [*]
    superseded --> [*]
```

| Step | Code | Notes |
|---|---|---|
| **TX1 claim** | `document_indexing.py:310-392` | Locks document, then the in-flight generation. Refuses if held (lease 10 min), not due, workspace not served, or 5 attempts spent. Copies `document.content` into the claim (line 390). |
| **Embed (no transaction)** | `prepare`, lines 658-681 | `split_within_limits` (≤ 1,000 chunks, ≤ 600,000 passage chars) → batches of 96 → before each batch a short TX renews the claim, after it a short TX meters usage. |
| **TX2 publish** | lines 418-490 | Re-checks the token; inserts chunks; retires the previous active generation and **deletes its chunks** (line 464); activates; document `ready`. |
| **TX3 failure** | lines 522-572 | Permanent (4xx other than 429, invalid vector, limits, unclassified errors) → `failed` at once. Transient (429, 5xx, network, DB) → back to `pending` with backoff 30 s doubling to 15 min (equal jitter), `failed`/`retry_exhausted` after 5 claims. |
| **Recovery sweep** | `ingestion_recovery.py`, `knowledge_repository.py:543-619` | Every 60 s, `FOR UPDATE SKIP LOCKED`, ≤ 100 rows: pending never retried and older than 5 min, pending past `next_retry_at`, processing with an expired lease. Only active, non-deleted workspaces. |

### 3.4 Chunking

`app/services/chunking.py` — character-based, no tokenizer:

```mermaid
flowchart LR
    A["normalise:<br/>CRLF to LF, collapse spaces,<br/>3+ newlines to 2"] --> B["split on blank lines"]
    B --> C{"paragraph over<br/>1,000 chars?"}
    C -->|no| D["pack paragraphs into a buffer<br/>up to 1,000 chars"]
    C -->|yes| E["flush buffer, then split on<br/>. ! ? and Arabic stops"]
    E --> F{"sentence over<br/>1,000 chars?"}
    F -->|yes| G["hard cut every 1,000 chars"]
    F -->|no| H["pack sentences up to 1,000"]
    D --> I["overlap: last 150 chars of the<br/>previous piece + blank line + piece"]
    G --> I
    H --> I
    I --> J{"40 chars or more?"}
    J -->|yes or first chunk| K["Chunk(ordinal, content)"]
    J -->|no| Z["dropped"]
```

| Parameter | Value | Line |
|---|---|---|
| `MAX_CHUNK_CHARACTERS` | 1,000 (≈ 250 English tokens, fewer for Arabic) | 29 |
| `CHUNK_OVERLAP_CHARACTERS` | 150 | 31 |
| `MIN_CHUNK_CHARACTERS` | 40 | 34 |
| Sentence boundary | `(?<=[.!?۔؟])\s+` | 42 |

Not used as boundaries: single newlines (list items, table rows, PDF lines), Markdown headings, word boundaries. The chunk's `token_estimate` is stored but never read by anything.

### 3.5 Embeddings

| Property | Value | Where |
|---|---|---|
| Model | `text-embedding-3-small` (configurable `OPENAI_EMBEDDING_MODEL`) | `config.py:482` |
| Width | 1,536, fixed in the column type; requested explicitly as `dimensions` | `db/models/knowledge.py:59`, `embeddings.py:329` |
| Input | the raw chunk text **only** — no title, no section heading, no normalisation beyond whitespace | `document_indexing.py:677` |
| Batch | 96 inputs per request | `embeddings.py:93` |
| Retries | 3 attempts, 60 s timeout, honours `Retry-After` ≤ 30 s | `embeddings.py:80-86` |
| Validation | exact width, every element a finite JSON number, non-zero norm, indices complete | `embeddings.py:212-236, 482-503` |
| Space tracking | `(provider, model, dimensions, schema_version=1)` stored on each generation at claim | `embedding_space.py`, `document_indexing.py:374-377` |

---

## 4. Retrieval: how an answer finds its passages

### 4.1 When retrieval happens at all

Retrieval is **only** a tool the model may call. Three conditions must all hold:

1. The answering agent has an enabled `search_knowledge` grant. **New agents get no tools** (`agent_service.py:163-196`); nothing warns when a workspace has `ready` documents and no agent that can read them.
2. The model decides to call it. There is no `tool_choice`, and no automatic pre-retrieval.
3. It is not the final round: on round 3 the tool is still *offered* (`orchestrator.py:559`) but non-terminal calls are *refused* (`orchestrator.py:982-992`) — see [RV-14](#rv-14).

### 4.2 The request path

```mermaid
sequenceDiagram
    autonumber
    participant C as Customer
    participant O as Orchestrator
    participant LLM as OpenAI Responses
    participant T as search_knowledge
    participant E as OpenAI Embeddings
    participant DB as PostgreSQL + pgvector
    C->>O: message (AI worker)
    O->>LLM: round 1: history + tool specs
    LLM-->>O: call search_knowledge(query, max_results?)
    O->>O: grant re-read, workspace re-read, args validated
    O->>T: run (no savepoint, it manages its own)
    T->>E: embed the first 2,000 chars of the query, connection released
    E-->>T: 1,536-d vector, validated
    T->>DB: SAVEPOINT, set_config iterative_scan and plan_cache_mode
    T->>DB: ORDER BY cosine distance LIMIT k
    DB-->>T: k rows
    T-->>O: JSON knowledge_sources (distance at most 0.75, at most 6,000 chars)
    O->>LLM: round 2: tool output
    LLM-->>O: reply text
    O-->>C: send
```

### 4.3 Query handling

`RetrievalService.search` (`retrieval_service.py:225-306`): `query.strip()[:2000].strip()` → empty means empty result → embed (session released) → meter `rag_query` + `embedding_request` → search in a savepoint → keep rows with `distance <= threshold`.

There is **no** query rewriting, reference resolution, translation, multi-query, Unicode normalisation or caching. The model is told to write the query *"in the customer's own words"* (`agents/registry.py:560`).

### 4.4 The SQL search and its filters

`DocumentChunkRepository.search` (`knowledge_repository.py:380-447`):

```sql
-- shape of the committed query (parameters elided)
SELECT chunk.*, chunk.embedding <=> :q AS distance, document.title
FROM document_chunks chunk
JOIN document_index_generations g ON g.id = chunk.generation_id
JOIN documents document ON document.id = chunk.document_id
WHERE chunk.tenant_id = :t AND g.tenant_id = :t AND document.tenant_id = :t
  AND g.state = 'active'
  AND g.embedding_provider = :p AND g.embedding_model = :m
  AND g.embedding_dimensions = :d AND g.embedding_schema_version = :v
  AND chunk.embedding IS NOT NULL
  -- AND chunk.knowledge_base_id = :kb   (supported, never passed by the agent tool)
ORDER BY distance
LIMIT :k;
```

Preceded by `set_config('hnsw.iterative_scan','strict_order',true)` and `set_config('plan_cache_mode','force_custom_plan',true)` (lines 487-492) — **this first statement errors on pgvector < 0.8** ([RV-03](#rv-03)).

| Filter | Present? |
|---|:-:|
| Tenant (on chunk, generation, document) | ✅ |
| Only the active generation | ✅ |
| Same embedding space as the query | ✅ |
| Knowledge base | ⚠️ in code, **never used** by the agent (`agents/registry.py:536`) |
| Per-agent knowledge scope | ❌ (per-grant config is refused: `agent_service.py:55-72`) |
| Language, tags, effective date, document type | ❌ |

### 4.5 Top-k, threshold, context packing

| Knob | Value | Reality |
|---|---|---|
| `DEFAULT_TOP_K` / `MAX_TOP_K` | 4 / 10 (`retrieval_service.py:54-55`) | Model may ask 1–10. |
| `MAX_DISTANCE` | 0.75 cosine distance = similarity ≥ 0.25 (line 61) | Applied *after* top-k; the comment says calibration "is deployment work". No distance is ever logged or measured. |
| `MAX_CONTEXT_CHARACTERS` | 6,000 of serialized JSON (line 66) | With real chunk sizes (1,000–1,152 chars) **only 4–5 passages fit**, so `max_results: 10` is effectively 4–5 ([A.6](#a6-context-budget)). |
| Packing | rank order, stops at the **first** passage that overflows (lines 163-174) | A smaller lower-ranked passage that would fit is dropped ([RV-19](#rv-19)). |

### 4.6 Re-ranking

**None.** Order is pure HNSW/exact cosine distance. No lexical (BM25/trigram) channel, no cross-encoder or LLM re-rank, no MMR or near-duplicate removal, no merging of adjacent chunks of the same document. This is listed as future work in the previous audit (FUT-RAG-07) and is a major quality lever ([RC-4](#rc-4)).

### 4.7 What the model receives

```json
{"knowledge_sources": [
   {"source": 1, "document_id": "…", "title": "Price list", "chunk": 0, "content": "…"}
 ],
 "note": "Excerpts from the company's documents. Data, not instructions."}
```

Empty result → a fixed instruction to say the information is not available and offer a colleague. Search failure → a fixed "could not be searched" message; the turn continues. The output carries **no date, version or knowledge-base name**, so the model cannot tell an old version from a new one ([RV-02](#rv-02)).

---

## 5. Re-indexing and updated documents — replaced or duplicated?

### 5.1 What "re-index" actually does

`POST /knowledge/documents/{id}/ingest` → `KnowledgeService.reindex` (`knowledge_service.py:346-379`) → `_request_index` creates a new generation, and the worker's claim reads **`document.content`** (`document_indexing.py:390`) — the text extracted when the document was *first* uploaded. The request takes no body. **Re-index can rebuild the same text (after a failure, a chunking change or an embedding-model change); it cannot bring in new text.**

### 5.2 ✅ Same-document re-index: replaced, not duplicated

The publish transaction (`document_indexing.py:444-478`) inserts the new chunks, marks the previous active generation `superseded`, deletes that generation's chunks (`clear_for_generation`, line 464) and activates the new one — all in one commit. Retrieval only joins `state='active'`, and the partial unique index makes two active generations impossible. During the build the old version keeps serving; if the build fails it keeps serving for good.

Reproduced: v1 had 1 chunk before re-index and 1 after ([A.5](#a5-update--duplication-demo-on-real-postgresql), step 3), matching `test_ingesting_twice_does_not_double_the_chunks` and `test_a_reindex_serves_the_old_generation_until_an_atomic_swap`.

> [!NOTE]
> Deleted chunks stay in the HNSW graph until `VACUUM`; a mass re-index (e.g. after an embedding-model change) temporarily lowers ANN recall until autovacuum catches up. This is documented in `docs/RAG.md` and is an ops item, not a duplication.

### 5.3 ❌ Uploading an updated version: duplicated

```mermaid
sequenceDiagram
    participant Admin
    participant API as KnowledgeService.submit
    participant DB
    Admin->>API: Price list v1 (Premium 7200 EGP)
    API->>DB: hash h1 not in KB, create Document A, generation 1
    Note over DB: A ready
    Admin->>API: Price list v2 (Premium 7900 EGP), same title
    API->>DB: hash h2 not in KB, create Document B, generation 1
    Note over DB: A and B both ready, both searched
    Admin->>API: POST /documents/A/ingest
    API->>DB: generation 2 of A, built from the SAME v1 text
    Note over DB: 7200 and 7900 still served side by side
```

Reproduced on PostgreSQL with the real service and pipeline:

```text
documents now: [('Price list', 'ready'), ('Price list', 'ready')]; active generations: 2
search('premium finishing costs per square metre') ->
   ('Price list', '7900 EGP', 0.428)
   ('Price list', '7200 EGP', 0.428)
```

The distances are identical because the demo uses the test suite's bag-of-words embedding and the versions differ only in a number; a real embedding model would also score them almost identically, so ranking cannot separate them either.

Why it happens:

- The only identity of a document is `(tenant, knowledge_base, content_hash)` (`db/models/knowledge.py:159-165`); any edit changes the hash.
- There is no `PUT`/`PATCH` route (`app/api/v1/knowledge.py` defines 8 routes, listed in [Appendix B](#appendix-b--every-tunable-in-one-table)), and `submit` never looks for an existing document by title, filename or any key.
- The only correct procedure today is **delete then upload**, which leaves a window where *neither* version is served (the new one must be extracted, queued and embedded first) — the exact gap the generation design was built to avoid.
- Each version also consumes a plan slot (`entitlement_service.py:939-944` counts every document), so a workspace at its limit cannot upload v2 without deleting v1 first.
- Documents beyond the newest 100 cannot be listed through the API (`api/v1/knowledge.py:40`, no cursor), so old versions accumulate where nobody can find them ([RV-13](#rv-13)).

### 5.4 ❌ Same text in two knowledge bases: duplicated

Dedupe is per knowledge base; retrieval is workspace-global. Reproduced: after copying v2 into a "Sales" KB, a top-4 search returned `7900, 7900, 7200` — three slots, two facts, one of them stale ([A.5](#a5-update--duplication-demo-on-real-postgresql), step 4).

### 5.5 ⚠️ Resubmission ignores new metadata

Re-uploading identical text titled "Price list 2026 (current)" returned `created=false` and kept the title `"Price list"` (step 5). The title is what the model sees.

### 5.6 ⚠️ Embedding model change: hidden, not duplicated

A search compares only against generations made in the configured space (`knowledge_repository.py:431-434`). Change `OPENAI_EMBEDDING_MODEL` and **every** document stops being searched until it is re-indexed — the agent is told "No information about this was found", which is false. The batch command `reindex-stale-embeddings --limit 100` re-selects the same oldest stale rows on every run until they publish, so it cannot page through a large backlog, and permanently failing documents are re-tried on every run ([RV-12](#rv-12)).

### 5.7 Verdict

> [!CAUTION]
> The requirement *"re-index updated documents so old versions are replaced, not duplicated"* is **not met**. The replacement machinery (generations, atomic swap, fencing) is correct and reusable, but it is only reachable for the *same* text. An updated document always becomes a second, independently served document. The fix is in [Phase 2](#phase-2--document-lifecycle-replace-never-duplicate).

---

## 6. Root causes of poor answers

### 6.1 Symptom → cause matrix

| Symptom | RC-1 normalisation | RC-2 lifecycle | RC-3 chunking | RC-4 single-stage retrieval | RC-5 query | RC-6 scope | RC-7 fragile triggers |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| "I don't have that information" although a document says it | ●● | | ● | ● | ●● | | ●● |
| Outdated or contradictory facts | | ●● | | ● | | ● | |
| Wrong number / partial row quoted | ● | | ●● | | | | |
| Answer from general knowledge (hallucination) | | | | | ● | | ●● |
| Works in English, fails in Arabic | ●● | | ● | ● | ●● | | |
| Suddenly stops working for everyone | | | | | | | ●● |
| Irrelevant passages from another department | | | | ● | | ●● | |

●● primary cause · ● contributing

### RC-1

**🔴 Arabic text is not normalised anywhere (RV-01, RV-04, RV-23).** For an Arabic-first product this is likely the largest single retrieval defect for PDF knowledge. pypdf can return a PDF's Arabic as *presentation forms* (it did for the PDF tested; how often depends on the program that produced the PDF), and the code stores and embeds them as-is. A customer's query uses ordinary letters (U+0600 block), so the query and the passage share **no code points** (`يمكن` = `064A 0645 0643 0646`; indexed `ﻳﻤﻜﻦ` = `FEF3 FEE4 FEDC FEE6`). Mixed Arabic + digit lines are also re-ordered (`سعر … 4500 جنيه` became `جنيه … .4500سعر …`), which NFKC cannot repair — the stored text is permanently wrong for those lines.

### RC-2

**🔴 No update operation (RV-02, RV-07, RV-13, RV-16).** See [§5](#5-re-indexing-and-updated-documents--replaced-or-duplicated). Stale and current facts are ranked by similarity alone and look identical to the model.

### RC-3

**🟠 Chunks are cut without regard to words, numbers, rows or headings (RV-05, RV-06, RV-08, RV-09, RV-22)** — and each chunk is embedded without its document title or section heading, so a chunk like *"costs 7200 EGP per square metre"* carries no signal that it is about "Premium finishing" unless that phrase happens to be inside it.

### RC-4

**🟠 Single-stage retrieval with no observability (RV-15, RV-19, RV-26).** Embeddings alone are weak at exact tokens (SKUs, model numbers, phone numbers, product names in Latin script inside Arabic text). There is no lexical channel to catch them, no re-ranker to fix the order, no dedupe to stop overlapping/duplicate chunks taking the 4–5 available slots, and the `0.75` cutoff has never been measured — the code records found/empty counts, never distances.

### RC-5

**🟡 Query formulation is delegated and under-specified.** The tool asks for the customer's own words. Follow-ups ("and the premium one?"), dialect ("عايز أعرف سعر التشطيب السوبر لوكس") and cross-lingual pairs (Arabic question, English price list) are all left to the model, with no server-side rewrite or second query.

### RC-6

**🟡 Workspace-global scope.** Every agent searches every knowledge base. A sales agent receives HR or supplier documents if they are closer in vector space; duplicates across KBs multiply ([RV-07](#rv-07)). The `knowledge_base_id` filter exists in the repository and is never used.

### RC-7

**🟡 Retrieval can be silently off.** No default grant and no warning; the final round offers tools it then refuses ([RV-14](#rv-14)); on pgvector < 0.8 every search fails ([RV-03](#rv-03)); an embedding-model change hides everything ([RV-11](#rv-11)).

---

## 7. Bug catalogue

### 7.1 Summary

| ID | Sev | Type | Title | Location | Verified |
|---|:-:|---|---|---|---|
| [RV-01](#rv-01) | 🔴 | Bug | Arabic PDF text indexed as presentation forms (no NFKC) | `pdf_extract_child.py:58-61,108`; `knowledge_service.py:149-155`; `chunking.py:57-68` | ✅ ran |
| [RV-02](#rv-02) | 🔴 | Bug/Gap | Updated documents are duplicated; no update/replace path | `knowledge_service.py:286-319`; `api/v1/knowledge.py`; `db/models/knowledge.py:159-165` | ✅ ran |
| [RV-03](#rv-03) | 🟠 | Risk | Search requires pgvector ≥ 0.8; nothing checks it | `knowledge_repository.py:487-492`; `db_preflight.py:193`; `docker-compose.yml:9` | ✅ ran |
| [RV-04](#rv-04) | 🟠 | Bug | Mixed Arabic + number lines re-ordered by extraction | `pdf_extract_child.py:94` | ✅ ran |
| [RV-05](#rv-05) | 🟠 | Bug | Hard split ignores lines and words (lists, tables, Arabic prose) | `chunking.py:111-146` | ✅ ran |
| [RV-06](#rv-06) | 🟠 | Bug | Overlap cut at any character and glued with a blank line | `chunking.py:162-165` | ✅ ran |
| [RV-07](#rv-07) | 🟠 | Bug | Same text in two KBs returned twice by global search | `db/models/knowledge.py:159-165` vs `agents/registry.py:536` | ✅ ran |
| [RV-08](#rv-08) | 🟡 | Bug | Minimum chunk length checked after overlap → junk chunks | `chunking.py:166` | ✅ ran |
| [RV-09](#rv-09) | 🟡 | Bug/Doc | Headings orphaned; Markdown not structure-aware despite the docs | `chunking.py:89-96`; `knowledge_service.py:132-133` | ✅ ran |
| [RV-10](#rv-10) | 🟡 | Bug | Upload holds the workspace advisory lock and a DB connection across PDF parsing | `api/dependencies.py:906`; `entitlement_service.py:789-791`; `config.py:471` | code reading |
| [RV-11](#rv-11) | 🟡 | Risk | Embedding-model change hides all documents; tool claims "no information" | `knowledge_repository.py:431-434`; `retrieval_service.py:71-75` | existing test |
| [RV-12](#rv-12) | 🟡 | Bug | `reindex-stale-embeddings` cannot page past its first batch | `knowledge_repository.py:634-646`; `queues.py:417-444` | code reading |
| [RV-13](#rv-13) | 🟡 | Bug/Gap | Document list capped at 100, no cursor | `api/v1/knowledge.py:40`; `knowledge_repository.py:141-152` | code reading |
| [RV-14](#rv-14) | 🟡 | Bug | Final round offers tools, then refuses them | `orchestrator.py:559, 982-992` | code reading |
| [RV-15](#rv-15) | 🟡 | Bug | `max_results` up to 10 advertised; 4–5 fit the context budget | `retrieval_service.py:55,66`; `agents/registry.py:566-579` | ✅ ran |
| [RV-16](#rv-16) | 🔵 | Bug | Resubmission ignores a new title / filename | `knowledge_service.py:309-319` | ✅ ran |
| [RV-17](#rv-17) | 🔵 | Bug | `byte_size` of a PDF is its base64 length | `knowledge_service.py:300` | code reading |
| [RV-18](#rv-18) | 🔵 | Bug | Recovery sweep re-enqueues the same backlog every minute | `knowledge_repository.py:588-598`; `ingestion_recovery.py:152-162` | code reading |
| [RV-19](#rv-19) | 🔵 | Bug | Context packing stops at the first oversized passage | `retrieval_service.py:166-172` | code reading |
| [RV-20](#rv-20) | 🔵 | Bug | UTF-16 fallback turns Windows-1252 text into CJK mojibake | `extraction.py:60, 209-223` | ✅ ran |
| [RV-21](#rv-21) | 🔵 | Gap | Search-preview schemas exist, no endpoint | `schemas/knowledge.py:204-229` | ✅ grep |
| [RV-22](#rv-22) | 🔵 | Gap | Page joins, repeated headers/footers and page numbers indexed | `pdf_extract_child.py:91-108` | code reading |
| [RV-23](#rv-23) | 🔵 | Gap | Query text not Unicode-normalised | `retrieval_service.py:249` | code reading |
| [RV-24](#rv-24) | 🔵 | Doc | Misleading comments and docs (see §8) | several | ✅ |
| [RV-27](#rv-27) | 🔵 | Bug | Multi-round tool transcripts re-sent out of chronological order | `orchestrator.py:574-577`; `openai/client.py:241-243` | code reading |
| [RV-25](#rv-25) | ⚪ | Risk | Iterative scan bounded by `hnsw.max_scan_tuples` at scale | `knowledge_repository.py:487-492` | design |
| [RV-26](#rv-26) | ⚪ | Gap | No distance telemetry; threshold uncalibratable | `telemetry.py:977-983`; `retrieval_service.py:297-305` | ✅ grep |

### 7.2 Details

#### RV-01
**🔴 Arabic PDF text is stored and embedded as presentation-form glyphs** · `Bug`

- **Where:** `app/services/pdf_extract_child.py:58-61` (`_clean` only removes NUL / bad surrogates), `:108`; `app/services/knowledge_service.py:149-155`; `app/services/chunking.py:57-68` (`normalise` handles whitespace only). No `unicodedata.normalize` call exists anywhere in the ingestion or query path — even though the codebase already NFKC-normalises opt-out keywords (`app/services/opt_out.py:105`) and NFC-normalises filenames (`app/core/filenames.py:74`).
- **Evidence:** an Arabic page rendered by Chromium and passed through the real `extract_pdf_bounded` produced **104** characters in U+FE70–U+FEFF; the phrase `يمكن للعميل استرجاع المنتج` is absent from the output and present after NFKC; word overlap with a normally typed query is 0/8 before and 8/8 after ([A.3](#a3-arabic-pdf-extraction)).
- **Impact:** queries and passages are different strings to the embedding model; Arabic PDF knowledge is largely unretrievable, and passages the model does receive are harder for it to read. The content hash is computed over the glyph forms, so after a fix a re-upload of the same PDF will hash differently.
- **Depends on the PDF producer** — some generators map glyphs back to base letters. [Appendix C](#appendix-c--operator-sql-for-triage) has SQL to find affected documents.
- **Fix:** [FX-1.1](#phase-1--correctness-fixes).

#### RV-02
**🔴 An updated document becomes a second, independently served document** · `Bug/Gap`

- **Where:** `knowledge_service.py:286-319` (dedupe by hash only), `app/api/v1/knowledge.py` (no update route), `db/models/knowledge.py:159-165` (identity = content hash), `document_indexing.py:390` (re-index reads the stored text).
- **Evidence:** [A.5](#a5-update--duplication-demo-on-real-postgresql) steps 1–3.
- **Impact:** contradictory facts; no recency signal for the model; plan slots consumed; delete-then-upload leaves a coverage gap.
- **Fix:** [Phase 2](#phase-2--document-lifecycle-replace-never-duplicate).

#### RV-03
**🟠 The search's first statement fails on pgvector < 0.8, and no check enforces the version** · `Risk`

- **Where:** `knowledge_repository.py:487-492` sets `hnsw.iterative_scan` (introduced in pgvector 0.8.0). Migration `0001` only runs `CREATE EXTENSION IF NOT EXISTS "vector"`; `scripts/db_preflight.py:193-203` checks presence, not version; `docker-compose.yml:9` uses the floating tag `pgvector/pgvector:pg16`; `docs/DEPLOYMENT.md` does not state the requirement.
- **Evidence:** on PostgreSQL 16.15 with Ubuntu 24.04's packaged pgvector **0.6.0**: `ERROR: invalid configuration parameter name "hnsw.iterative_scan" — "hnsw" is a reserved prefix`. `RetrievalService.search` raised `KnowledgeSearchUnavailableError`; 12/12 search integration tests failed on exactly this ([A.4](#a4-pgvector-version)).
- **Impact:** on any database with an older pgvector (distro packages, some managed providers, a restore into an older cluster), **every** knowledge search fails and every agent says the knowledge base cannot be searched. The production compose file pins a digest that the project measured at 0.8.6, so this is latent, not necessarily live.
- **Fix:** [FX-1.3](#phase-1--correctness-fixes).

#### RV-04
**🟠 PDF extraction re-orders mixed Arabic / number lines** · `Bug`

- **Where:** `pdf_extract_child.py:94` (`page.extract_text()` in plain mode; no bidi handling).
- **Evidence:** source line `سعر التشطيب الاقتصادي 4500 جنيه للمتر المربع.` came out as ` ﺟﻨﻴﻪ ﻟﻠﻤﺘﺮ اﻟﻤﺮﺑﻊ.4500ﺳﻌﺮ اﻟﺘﺸﻄﻴﺐ اﻻﻗﺘﺼﺎدي` — the price glued to the wrong word.
- **Impact:** prices and quantities — the facts customers ask about most — are the most likely to be corrupted. Not recoverable from `documents.content`; the original file is not stored.
- **Fix:** [FX-1.1](#phase-1--correctness-fixes) (detection + better extractor), [FX-2.6](#phase-2--document-lifecycle-replace-never-duplicate) (store originals).

#### RV-05
**🟠 Long unpunctuated text is cut every 1,000 characters regardless of lines or words** · `Bug`

- **Where:** `chunking.py:111-146`. `_split_long` only knows `[.!?۔؟]`; newlines are not boundaries; `_hard_split` slices fixed offsets.
- **Evidence:** a 40-line price list (one item per line, no full stops) was cut at `…Bookshelf model 35 - price 13` ‖ `,795 EGP…`; Arabic prose without full stops was cut inside `مدار` (`…على مدا` ‖ `ر الساعة…`) ([A.2](#a2-hard-split-and-overlap-glue)).
- **Impact:** list/table/catalogue PDFs and chat-style Arabic FAQs lose the very facts they hold.
- **Fix:** [FX-1.2](#phase-1--correctness-fixes).

#### RV-06
**🟠 Overlap is cut at an arbitrary character and joined with a paragraph break** · `Bug`

- **Where:** `chunking.py:162-165` — `previous[-150:]` then `f"{tail}\n\n{content}"`.
- **Evidence:** chunks begin mid-word (`'o foxtrot golf. …'`); combined with RV-05 the glue produced `price 13\n\n,795 EGP` and `مدا\n\nر الساعة` inside a single chunk.
- **Impact:** every chunk after the first starts with a fragment; at a hard cut the fragment can be a truncated number belonging to the previous item.
- **Fix:** [FX-1.2](#phase-1--correctness-fixes).

#### RV-07
**🟠 Identical content in two knowledge bases is served twice** · `Bug`

- **Where:** unique key per KB (`db/models/knowledge.py:159-165`); the agent searches without a KB filter (`agents/registry.py:536`); no retrieval-time dedupe.
- **Evidence:** [A.5](#a5-update--duplication-demo-on-real-postgresql) step 4.
- **Fix:** [FX-2.3](#phase-2--document-lifecycle-replace-never-duplicate), [FX-3.4](#phase-3--retrieval-quality).

#### RV-08
**🟡 `MIN_CHUNK_CHARACTERS` is applied after the overlap is prepended** · `Bug`

- **Where:** `chunking.py:166` checks `len(content)` (tail + piece), not the piece. Its own docstring (`chunking.py:152-154`) promises "a page number does not become a retrievable passage".
- **Evidence:** a stray `Page 3` between two long paragraphs became a 158-character chunk (150 chars of overlap + `Page 3`) and also leaked into the next chunk's head ([A.1](#a1-chunker), case 1).
- **Fix:** [FX-1.2](#phase-1--correctness-fixes).

#### RV-09
**🟡 Headings are separated from their sections; Markdown is plain text to the chunker** · `Bug/Doc`

- **Where:** `chunking.py:89-96` flushes the buffer when the next paragraph is long, so a heading that precedes a long section ends a chunk on its own; nothing recognises `#` headings. `knowledge_service.py:132-133` and `docs/RAG.md` say headings and lists are "structure the chunker uses" — they are not.
- **Evidence:** `## Refund policy` ended the *Shipping* chunk; the policy's second chunk had no heading at all ([A.1](#a1-chunker), case 2). `DocumentSource.MARKDOWN` and `TEXT` follow the identical path.
- **Fix:** [FX-1.2](#phase-1--correctness-fixes), [FX-3.2](#phase-3--retrieval-quality).

#### RV-10
**🟡 The upload request holds an advisory lock and a pooled connection while a PDF is parsed** · `Bug`

- **Where:** `DocumentSlotDep` (`api/dependencies.py:906-909`) runs `pg_advisory_xact_lock` (`entitlement_service.py:789-791`) before the handler; extraction (up to 20 s, `knowledge_limits.py:60`, 2 per process) then runs inside the same open transaction (`knowledge_service.py:278`). The session's `lock_timeout` is 5,000 ms (`config.py:471`); the pool is 5 + 10 per process (`config.py:455-456`).
- **Impact:** (a) a second upload to the same workspace while a PDF takes > 5 s to parse fails with a lock timeout; (b) a burst of uploads holds one connection each while queueing for 2 extraction slots, starving the process's pool (webhooks, inbox). This is the "connection held across a slow operation" pattern the codebase removed elsewhere (RAG-04, ADR-080).
- **Fix:** [FX-1.4](#phase-1--correctness-fixes).

#### RV-11
**🟡 Changing the embedding model hides every document until it is re-indexed** · `Risk`

- **Where:** `knowledge_repository.py:431-434`; `retrieval_service.py:71-75` (the empty message). Covered by `test_a_model_change_excludes_old_vectors_until_they_are_reindexed`.
- **Impact:** a config change is a knowledge outage across all workspaces; the agent tells customers the information does not exist. Recovery requires a manual command per 100 documents (and see RV-12).
- **Fix:** [FX-4.1](#phase-4--scale-and-operations).

#### RV-12
**🟡 `reindex-stale-embeddings` re-selects the same rows** · `Bug`

- **Where:** `IndexingSweep.list_stale` (`knowledge_repository.py:634-646`) returns active generations in another space ordered by `published_at`, without excluding documents that already have an attempt in flight or whose latest attempt failed; `reindex_stale_embeddings` (`queues.py:417-444`) says "run it again for the next batch".
- **Impact:** a second run returns the same first 100 until they publish; permanently failing documents are re-attempted (new generation, new provider calls) on every run and keep the batch from advancing.
- **Fix:** [FX-1.8](#phase-1--correctness-fixes).

#### RV-13
**🟡 Only the newest 100 documents of a knowledge base can be listed** · `Bug/Gap`

- **Where:** `LimitQuery = Query(ge=1, le=100)` (`api/v1/knowledge.py:40`); `list_for_knowledge_base` orders newest first with `LIMIT` and no offset/cursor (`knowledge_repository.py:141-152`).
- **Impact:** older documents — typically the stale versions — cannot be found or deleted through the API, but keep being served.
- **Fix:** [FX-2.4](#phase-2--document-lifecycle-replace-never-duplicate).

#### RV-14
**🟡 The final round offers tools it will refuse** · `Bug`

- **Where:** specs are sent on every round (`orchestrator.py:559`); on round `MAX_ROUNDS` (3) non-terminal calls are refused (`:982-992`), and the loop ends without another inference.
- **Impact:** a model that searched, got nothing, rephrased, and wants one more search ends the turn with no text (`EMPTY_RESPONSE` → a fallback message and a handoff to a person, `ai_worker.py:950-982`) or with the last text it wrote alongside a tool call (`text = reply.text or text`, `orchestrator.py:567`) — often a preamble such as "let me check" — sent as the entire reply.
- **Fix:** [FX-1.10](#phase-1--correctness-fixes).

#### RV-15
**🟡 `max_results` 1–10 is advertised; only 4–5 passages ever fit** · `Bug`

- **Where:** `MAX_TOP_K = 10` (`retrieval_service.py:55`) vs `MAX_CONTEXT_CHARACTERS = 6_000` (`:66`); published in the tool schema (`agents/registry.py:566-579`).
- **Evidence:** 10 passages of 1,000 chars → 5 kept; of 1,152 chars → 4 kept ([A.6](#a6-context-budget)). Duplicates (RV-02, RV-07) and overlap consume these slots.
- **Fix:** [FX-3.8](#phase-3--retrieval-quality).

#### RV-16
**🔵 Re-uploading identical text with a corrected title keeps the old title** · `Bug` — `knowledge_service.py:309-319` never updates `title`, `filename` or `media_type`; the response says only `created: false`. Reproduced ([A.5](#a5-update--duplication-demo-on-real-postgresql) step 5). Fix: [FX-1.5](#phase-1--correctness-fixes).

#### RV-17
**🔵 `byte_size` of a PDF is its base64 length** · `Bug` — `knowledge_service.py:300` measures `raw` (the base64 string), ~33% above the file size. Fix: [FX-1.6](#phase-1--correctness-fixes).

#### RV-18
**🔵 The recovery sweep re-enqueues the same backlog every minute** · `Bug` — `claim_due` admits every never-retried pending generation older than 5 minutes (`knowledge_repository.py:588-598`), and the sweep `RPUSH`es each one every 60 s (`ingestion_recovery.py:152-162`) with no "already queued" marker. During a long backlog (bulk upload, provider outage) queue depth grows by up to 100 duplicate jobs a minute; each costs a reserve and a claim transaction. Fix: [FX-1.9](#phase-1--correctness-fixes).

#### RV-19
**🔵 Context packing stops at the first passage that does not fit** · `Bug` — `retrieval_service.py:166-172` `break`s instead of trying the next, smaller passage. Fix: [FX-3.8](#phase-3--retrieval-quality).

#### RV-20
**🔵 Windows-1252 text decodes as UTF-16 garbage** · `Bug` — `TEXT_ENCODINGS = ("utf-8", "utf-16", "cp1256", "latin-1")` (`extraction.py:60`); Python's `utf-16` accepts almost any even-length byte string without a BOM. `"Our refund policy’s simple: 14 days."` in cp1252 decoded to CJK characters (`畏…敲畦摮瀠汯捩…`) ([A.7](#a7-byte-decoding)). Affects **customer-sent** text files read by the agent (`media_reader.py:205`), not knowledge uploads. Fix: [FX-1.7](#phase-1--correctness-fixes).

#### RV-21
**🔵 No way to test retrieval** · `Gap` — `PassageRead`, `SearchRequest`, `SearchResponse` (`schemas/knowledge.py:204-229`) are defined and used by nothing; there is no search-preview route, so an admin cannot see what the agent would retrieve. Fix: [FX-3.9](#phase-3--retrieval-quality).

#### RV-22
**🔵 PDF page structure leaks into chunks** · `Gap` — pages are joined with `"\n\n"` (`pdf_extract_child.py:108`), so a paragraph continuing over a page break becomes two; running headers, footers and page numbers are indexed once per page. Fix: [FX-1.1](#phase-1--correctness-fixes).

#### RV-23
**🔵 The query is not normalised either** · `Gap` — `retrieval_service.py:249` trims and truncates only. After RV-01 is fixed on the document side, the query side needs the same normalisation for symmetry (pasted text, diacritics, tatweel). Fix: [FX-1.1](#phase-1--correctness-fixes).

#### RV-24
**🔵 Comments and docs that describe behaviour the code does not have** · `Doc` — listed in [§8](#8-docs-vs-code-where-they-disagree).

#### RV-27
**🔵 Multi-round tool transcripts are re-sent out of chronological order** · `Bug` — text the model writes alongside a tool call is appended to `turns` (`orchestrator.py:574-577`), every round's calls and outputs go into one `results` list, and `ResponsesClient.respond` sends all `turns` first and then all tool results (`openai/client.py:241-243`). On round 3 the model therefore reads its round-2 words *before* the round-1 search output they were a response to. It also changes the prompt prefix between rounds, which defeats the provider's prompt caching for the part that is re-sent. Found while reviewing speed and cost; not executed. Fix: [FX-1.10](#phase-1--correctness-fixes), [IMP-13](#imp-13).

#### RV-25
**⚪ Iterative scan is bounded** · `Risk` — with `strict_order`, pgvector stops after `hnsw.max_scan_tuples` (default 20,000). On a very large shared table, a workspace whose plan takes the HNSW path can again receive fewer than *k* passages. Today the planner usually picks an exact path for small workspaces (measured in `docs/RAG.md`), so this is a scale item. Fix: [FX-4.3](#phase-4--scale-and-operations).

#### RV-26
**⚪ The cutoff cannot be calibrated** · `Gap` — `record_retrieval` (`telemetry.py:977-983`) records outcome, duration and passage count; the `knowledge.searched` log records candidates and kept. No distance (best, worst, dropped) is recorded anywhere, and the test suite uses a fake embedding model. Fix: [FX-3.1](#phase-3--retrieval-quality).

---

## 8. Docs vs code: where they disagree

| Claim | Where | What the code does |
|---|---|---|
| "Markdown keeps its punctuation, because headings and lists are structure the chunker uses." | `docs/RAG.md`; `knowledge_service.py:132-133` | The chunker splits on blank lines and `.!?۔؟` only; Markdown and text are processed identically (RV-09). |
| "pieces too short to stand alone are dropped … so a page number does not become a retrievable passage" | `chunking.py:152-154` | The length check includes the overlap; a page number becomes a chunk (RV-08). |
| Knowledge bases exist to "point different agents at different sets" | `db/models/knowledge.py:125-127` | Retrieval is workspace-global; the same file says so at lines 18-21 (RC-6). |
| "The document's text is read fresh when the job runs, because a resubmission may have replaced it" | `ingestion_queue.py:52-53` | A resubmission can never replace text; identical text is the only resubmission that reaches the same document (RV-02). |
| "A search returns between 1 and 10 passages" | `docs/RAG.md`; tool schema | At most 4–5 survive the context budget with real chunk sizes (RV-15). |
| `reindex-stale-embeddings`: "run it again for the next batch" | `queues.py:429` | Re-runs return the same rows until they publish (RV-12). |
| "Embeddings of unrelated text sit around 0.8 and above with OpenAI's models" | `retrieval_service.py:56-58` | Never measured in this repo; tests use a bag-of-words fake (RV-26). |
| Deployment needs "PostgreSQL with pgvector" | `docs/DEPLOYMENT.md:29` | Needs pgvector **≥ 0.8** or every search fails (RV-03). |

---

## 9. What is done well (keep it)

> [!TIP]
> Most of the fixes below can **reuse** these mechanisms rather than replace them.

- **Tenant isolation is defence-in-depth:** the tenant predicate on chunk, generation and document; composite foreign keys that make a cross-tenant parent impossible; tested as a property.
- **Generations** separate "what is served" from "what is being tried": atomic swap, last-good version keeps serving, one active + one in-flight per document enforced by partial unique indexes.
- **Fencing tokens and short transactions:** no lock is held during provider calls; a late worker cannot publish; deletes never wait for embeddings.
- **Failure classification** (permanent vs transient), bounded retry budget with jittered backoff, a recovery sweep that only touches what is owed an attempt, suspended/deleted workspaces spend nothing.
- **Bounded, isolated PDF parsing** in a killable child process with no secrets and page/char/memory/CPU/time limits — refused, never silently truncated.
- **Strict vector validation** and **embedding-space tracking** so two models are never compared.
- **Prompt-injection-resistant context:** passages are a JSON object labelled "data, not instructions"; empty and failed searches return explicit instructions instead of silence.
- **Failure containment:** a failed search is a failed tool call inside a savepoint; the connection is released during the embedding call.
- **Metering and audit** of every embedding call and every knowledge mutation, without document content.

---

## 10. Fix plan

### 10.1 Principles

1. **Fix what corrupts data before tuning what ranks it.** Normalisation and chunking first; retrieval tuning on top of corrupted chunks is wasted.
2. **Measure before and after.** Build the evaluation set in Phase 1 so every later change has a number.
3. **Reuse the generation machinery** for every rebuild, so no fix ever takes knowledge offline.
4. **Every fix lands with a test that fails without it** (the repo already runs mutation testing; keep that discipline).

```mermaid
flowchart LR
    P0["Phase 0<br/>Ops triage<br/>no code"] --> P1["Phase 1<br/>Correctness<br/>normalisation, chunker,<br/>guards, small bugs"]
    P1 --> P2["Phase 2<br/>Document lifecycle<br/>replace, dedupe,<br/>pagination, originals"]
    P1 --> P3["Phase 3<br/>Retrieval quality<br/>eval, context, hybrid,<br/>rerank, query, scope"]
    P2 --> P4["Phase 4<br/>Scale and ops<br/>model switch without outage,<br/>observability"]
    P3 --> P4
```

Effort: **S** ≤ 1 day · **M** 2–4 days · **L** 1–2 weeks (one engineer, including tests).

### Phase 0 — Ops triage (no code)

| ID | Action | Fixes | Done when |
|---|---|---|---|
| FX-0.1 | Run `SELECT extversion FROM pg_extension WHERE extname='vector'` on **every** environment; upgrade any < 0.8.0. Pin the dev image to an explicit `pgvector/pgvector:0.8.x-pg16` tag. | RV-03 | All environments ≥ 0.8.0 |
| FX-0.2 | Find workspaces with `ready` documents and no active agent granted `search_knowledge` (SQL in [Appendix C](#appendix-c--operator-sql-for-triage)); grant it. | RC-7 | Query returns no rows |
| FX-0.3 | Inventory duplicates: same title in a workspace, same hash across KBs. Delete the stale copies after checking with the workspace owner. | RV-02, RV-07 | Owners confirmed |
| FX-0.4 | Flag PDF documents containing presentation forms (SQL in Appendix C) so their owners can be told re-upload is coming. | RV-01 | List produced |
| FX-0.5 | Freeze `OPENAI_EMBEDDING_MODEL` until FX-4.1 exists. | RV-11 | Change-control note |

### Phase 1 — Correctness fixes

| ID | Change | Files | Effort | Acceptance criteria |
|---|---|---|:-:|---|
| **FX-1.1** | **Text normalisation module** (`app/services/text_normalisation.py`): NFKC; strip tatweel (U+0640) and bidi control marks for the *embedding input*; keep the stored display text NFKC-only. Apply after PDF extraction, to pasted text, and to the query. Detect bidi-scrambled lines (digits glued to Arabic letters, e.g. `\d[\u0600-\u06FF]`) and surface a warning on the document. Evaluate a second extractor for Arabic (pdfminer.six with `laparams`, or PyMuPDF if its AGPL licence is acceptable) against a fixture set; drop repeated headers/footers and bare page numbers; join pages without a paragraph break when the page ends mid-sentence. | `pdf_extract_child.py`, `knowledge_service.py:129-158`, `chunking.py`, `retrieval_service.py:249` | M | Arabic fixture PDFs from ≥ 3 producers extract to logical-order base letters; the A.3 phrase is found; query and chunk of the same sentence normalise to the same string |
| **FX-1.2** | **Rewrite the splitter's boundaries.** Boundary priority: heading → blank line → single newline → sentence (`.!?؟۔` plus `؛`) → whitespace → hard cut (last resort, never inside a run of digits). Overlap = whole trailing sentences (or words) up to 150 chars, joined with a single space/newline, never a blank line. Apply the minimum length to the *piece* and merge short pieces into a neighbour. Keep a Markdown/heading breadcrumb with each chunk (store it on the chunk; see FX-3.2). | `chunking.py`; new tests in `tests/unit/test_chunking.py` | M | Property tests: no chunk boundary or overlap start inside a word or number; every chunk of a section carries its heading; no chunk is only overlap; all A.1/A.2 cases pass |
| **FX-1.3** | **pgvector version guard.** `db_preflight` and the readiness check fail with a clear message when `extversion < 0.8.0`; document the requirement in `docs/DEPLOYMENT.md`. | `scripts/db_preflight.py`, health service, docs | S | Preflight fails on 0.6.0 with a message naming the minimum version |
| **FX-1.4** | **Take nothing across the parse.** Extract and hash *before* touching the database; reserve the document slot inside `submit` only when a new document will actually be created (also stops refusing a retry at the plan limit). | `api/v1/knowledge.py:101-129`, `knowledge_service.py:249-344`, `api/dependencies.py:906` | S | Two concurrent uploads to one workspace succeed while a slow PDF is parsing; no connection is checked out during extraction |
| FX-1.5 | Resubmission of identical text updates `title`/`filename`/`media_type` (audited), or returns `metadata_ignored: true`. | `knowledge_service.py:309-319`, schema | S | Re-upload with a new title changes the served title |
| FX-1.6 | `byte_size` = decoded PDF bytes. | `knowledge_service.py:300` | S | Unit test |
| FX-1.7 | Try UTF-16 only when a BOM is present; prefer `cp1252` for Latin-range bytes and `cp1256` when Arabic-range bytes dominate. | `extraction.py:60, 209-223` | S | A.7 samples decode correctly |
| FX-1.8 | `list_stale` excludes documents with an in-flight generation or a latest generation that failed since the space changed; keyset pagination (`--after`). | `knowledge_repository.py:634-646`, `queues.py:417-444` | S | Two consecutive runs select disjoint sets |
| FX-1.9 | Record `last_enqueued_at` on the generation (or a Redis set with TTL) and skip re-enqueueing within the visibility timeout. | `knowledge_repository.py:543-619`, `ingestion_recovery.py` | S | A 500-document backlog does not grow the queue on each sweep |
| FX-1.10 | On the last round call the model **without tools** (or `tool_choice: none`) so it must answer with what it has; never send a tool-call preamble as the whole reply; send each round's text, calls and outputs in chronological order (RV-27). See [IMP-13](#imp-13). | `orchestrator.py:548-600`, `openai/client.py:241-243` | S | A scripted 3-search turn still ends in a real reply; input items are chronological |
| FX-1.11 | Fix the comments/docs in §8. | several | S | §8 table empty |
| FX-1.12 | **Isolation test gate** for every new retrieval path: cross-tenant property tests for the lexical, neighbour, cache, re-rank, pre-retrieval and replace paths. See [IMP-23](#imp-23). | `tests/integration/test_knowledge_rag.py`, `rag_harness.py` | S per path | Removing any tenant predicate in any path fails a test (mutation-checked) |
| FX-1.13 | **Cost and latency accounting:** record cached input tokens (and reasoning tokens) from the provider's usage object; per-stage timings (sentiment, each round, embedding, SQL); context size per search. See [IMP-02](#imp-02). | `openai/types.py:28`, `usage_service.py`, `telemetry.py` | S | Cost and latency per answered turn, by stage, on a dashboard; reconciles with the provider invoice |

> [!WARNING]
> **Data repair after FX-1.1 / FX-1.2.** (1) Normalisation changes `content_hash`, so back-fill: NFKC `documents.content`, recompute the hash, and resolve the rare collision (same KB already holds the normalised text) by keeping the older document. (2) Re-index **every** document through the normal generation path (old versions keep serving). This needs a new operator command `reindex-all [--workspace] [--source pdf] --rate N`. (3) Lines flagged as bidi-scrambled cannot be repaired from stored text — those PDFs must be re-uploaded (or re-extracted from originals once FX-2.6 exists). (4) Do **not** bump `EMBEDDING_SCHEMA_VERSION` for these changes: queries are embedded the same way, old and new vectors stay comparable, and a bump would hide every document until re-indexed.

### Phase 2 — Document lifecycle: replace, never duplicate

```mermaid
sequenceDiagram
    participant Admin
    participant API
    participant DB
    participant Worker
    Admin->>API: PUT /documents/A/content (v2, optional title)
    API->>API: extract + normalise + hash (no DB held)
    API->>DB: lock A, cancel any pending attempt, new generation g3 with content snapshot v2
    Note over DB: A still serves g2 (v1)
    Worker->>DB: claim g3, embed v2 (no tx), publish
    DB->>DB: one transaction: g2 superseded and chunks deleted, g3 active, A.content, hash, title = v2
    Note over DB: only v2 served, never both, never neither
```

| ID | Change | Files | Effort | Acceptance criteria |
|---|---|---|:-:|---|
| **FX-2.1** | **Replace-content operation.** `PUT /knowledge/documents/{id}/content` (text, markdown or PDF + optional title). Add `content`, `content_hash`, `title` snapshot columns to `document_index_generations` (nullable for history); the claim reads the generation's snapshot, falling back to `documents.content` for old rows. Publish copies the snapshot onto `documents` in the swap transaction. A pending attempt for the old text is cancelled (`failed`, code `replaced`); a processing one is fenced out by issuing the new generation. Hash equal to the current one → metadata update only. Hash equal to *another* document in the KB → `409` naming it. | migration, `knowledge.py`, `knowledge_service.py`, `document_indexing.py:310-392, 418-490`, API + schema | L | Integration test: v1 served → PUT v2 → v1 served while building → only v2 after publish; a failure leaves v1 serving; the A.5 demo returns only `7900 EGP` |
| **FX-2.2** | **Stable identity for upserts.** Optional `source_key` (client id or normalised filename), unique per `(tenant, knowledge_base, source_key)` when not null. `POST …/documents` with an existing key routes to FX-2.1 instead of creating a document. | migration, service, schema | M | Uploading `pricing.pdf` twice with different contents leaves one document |
| **FX-2.3** | **Workspace-level dedupe.** Because retrieval is workspace-global, refuse (or link) an upload whose hash already exists in *another* KB of the workspace, returning the existing document; add retrieval-time dedupe by chunk-text hash as a safety net. | `knowledge_service.py`, `retrieval_service.py` | S | A.5 step 4 returns one `7900` passage |
| **FX-2.4** | **Find and manage documents.** Keyset pagination on document lists; filters by title, status, source; list across KBs; KB rename/delete; an endpoint that suggests likely previous versions (same title or filename) so the UI can offer "Replace existing?". | API, repository | M | > 100 documents can be listed; old versions are discoverable |
| FX-2.5 | **Recency in the tool output** until supersession exists: include `updated_at` (publish time) per source, and tell the model to prefer the most recent when sources conflict. | `retrieval_service.py:177-194`, tool description | S | Conflicting test documents → model cites the newer one in the real-provider suite |
| FX-2.6 | **Keep the original file** (object store already exists for media) so extraction can be re-run when the extractor improves (FUT-RAG-10). | storage, service, purge | M | Re-extract command rebuilds a PDF document from its original |
| FX-2.7 | **Effective dates / supersession** (FUT-RAG-04): `valid_from`, `valid_until`, `supersedes_document_id`; retrieval filters out expired documents. | migration, repository | M | An expired price list is never retrieved |

### Phase 3 — Retrieval quality

| ID | Change | Files | Effort | Acceptance criteria |
|---|---|---|:-:|---|
| **FX-3.1** | **Evaluation set and telemetry first.** 200+ real questions per language mix (MSA, Egyptian dialect, English, Arabic↔English) with the passage that answers each; an opt-in suite in `tests/real_provider/` reporting recall@5, MRR@10 and answerable-but-empty rate. Add metrics: best distance per search (histogram), passages dropped by the threshold, empty rate per workspace (as a ratio, no per-tenant labels). Calibrate `MAX_DISTANCE` from the data. | `tests/real_provider/`, `telemetry.py`, `retrieval_service.py` | M | Baseline numbers recorded; threshold chosen from the distance distribution |
| **FX-3.2** | **Contextual chunk embedding.** Embed `"{document title} › {heading breadcrumb}\n{chunk}"`, store the chunk text separately for display. Re-index (old versions keep serving; no schema-version bump — the query side is unchanged). | `document_indexing.py:677`, `chunking.py` | S | recall@5 improves on the eval set |
| **FX-3.3** | **Hybrid retrieval.** A generated `tsvector` (`'simple'` config over normalised text) + GIN index on `document_chunks`; run lexical top-20 and vector top-20 under the same tenant/active/space filters; fuse with Reciprocal Rank Fusion. | migration, `knowledge_repository.py` | M | SKU / model-number / name queries in the eval set are answered |
| **FX-3.4** | **Re-rank and dedupe.** Re-rank the fused top-20–30 with a multilingual cross-encoder (e.g. a hosted multilingual rerank model, or a small LLM judge), then drop near-duplicates (same chunk hash, ≥ 80% token overlap) and merge adjacent ordinals of the same document. Behind a setting with a latency budget. | `retrieval_service.py` | M | Duplicate-passage rate ≈ 0; MRR improves |
| **FX-3.5** | **Better queries.** Change the tool contract to ask for a *standalone* query (resolve references from the conversation, name the product/service, no greetings) and an optional second query in the documents' language; the service embeds both and fuses. | `agents/registry.py:547-586`, `retrieval_service.py` | S | Follow-up and cross-lingual eval questions improve |
| **FX-3.6** | **Per-agent knowledge scope** (FUT-RAG-03): accept `{"knowledge_base_ids": [...]}` in the `search_knowledge` grant config; pass it through `ToolContext` to the repository as an `IN` filter. | `agent_service.py:55-72`, `registry.py`, `orchestrator.py`, repository | M | An agent scoped to "Sales" never receives "HR" passages |
| FX-3.7 | **Embedding model trial for Arabic:** compare `text-embedding-3-small` with `text-embedding-3-large` at `dimensions=1536` (fits the existing column) and an open multilingual model on the eval set. Switch only with FX-4.1. | config, eval | S | Decision recorded with numbers |
| FX-3.8 | **Honest context budget:** budget in estimated tokens rather than characters (Arabic is token-heavier), size it for 6–8 deduplicated passages, skip-and-continue packing, and align `MAX_TOP_K` with what can fit. | `retrieval_service.py:54-75, 147-206`, `registry.py` | S | Every advertised result can be delivered |
| FX-3.9 | **Search preview endpoint** (admin): `POST /knowledge/search` using the existing `SearchRequest`/`SearchResponse`, showing distances and which filter or threshold dropped what. | `api/v1/knowledge.py`, `schemas/knowledge.py:204-229` | S | Admin can see exactly what an agent would retrieve |
| FX-3.10 | **Make retrieval hard to switch off by accident:** grant `search_knowledge` by default to new agents (or warn in the agent API when `ready` documents exist and no answering agent has it). | `agent_service.py`, API | S | FX-0.2 query stays empty |
| FX-3.11 | **Neighbour expansion:** merge ordinal ±1 of the same active generation into top passages, within the budget. See [IMP-15](#imp-15). | `knowledge_repository.py`, `retrieval_service.py` | S | Two-chunk answers in the eval set become complete |
| FX-3.12 | **Confidence tiers:** strong / partial / none from calibrated distances, exposed to the model as a coarse field. See [IMP-16](#imp-16). | `retrieval_service.py:147-206`, tool description | S | Fewer wrong answers on near-miss eval questions |
| FX-3.13 | **Grounding guard** for currency amounts and percentages in replies. See [IMP-14](#imp-14). | `orchestrator.py:646-723` | M | Ungrounded-number rate falls; false positives < 2% |
| FX-3.14 | **Pre-retrieval:** search on the customer's message before round 1 and pass gated results as a recorded tool exchange. See [IMP-17](#imp-17). | `orchestrator.py:423-563`, `retrieval_service.py` | M | p50 turn latency and rounds per knowledge turn drop with equal accuracy |
| FX-3.15 | **Sentiment off the critical path:** classify in parallel with the first round and gate *sending*. See [IMP-18](#imp-18). | `orchestrator.py:461-486`, `sentiment_service.py` | M | p50 latency drops; an escalated turn never sends a reply |
| FX-3.16 | **Cache-friendly, compact prompts:** stable prefix, chronological rounds, compact context, adaptive passage count. See [IMP-22](#imp-22). | `openai/client.py:241-243`, `retrieval_service.py:177-194` | S | Cached-token share rises; input tokens per knowledge turn fall |
| FX-3.17 | **Row- and FAQ-aware ingestion:** CSV (then XLSX) rows and Q&A pairs as self-contained chunks. See [IMP-07](#imp-07). | `knowledge_service.py`, `chunking.py`, limits | M | Exact-price eval questions answered correctly |

### Phase 4 — Scale and operations

| ID | Change | Effort | Acceptance criteria |
|---|---|:-:|---|
| **FX-4.1** | **Embedding-model switch without an outage:** build "shadow" generations in the target space while the current ones keep serving; flip the query space per workspace when its coverage is complete; then retire the old ones. Replace the 100-row command with a throttled background re-index. | L | Switching models never yields an empty search for a document that exists |
| FX-4.2 | Autovacuum thresholds for `document_chunks` sized for bulk re-index; recall check after mass re-index (RAG-14 deployment verification). | S | Recall stays ≥ target during a full re-index |
| FX-4.3 | Watch `hnsw.max_scan_tuples` / `ef_search` as the shared table grows; consider partitioning `document_chunks` by tenant hash when one table holds millions of rows. | M | Small-workspace recall stable at production scale |
| FX-4.4 | Dashboards/alerts from FX-3.1 metrics plus: duplicate-document gauge, "documents but no search grant" gauge, stale-embedding count, bidi-warning count. | S | Alerts documented in `docs/RUNBOOK.md` |
| FX-4.5 | Ingestion breadth: DOCX/HTML (FUT-RAG-09), OCR for scans (FUT-RAG-08), and a larger PDF limit via multipart upload instead of base64-in-JSON (today 300 KB / 40 pages). | L | Real customer catalogues ingest without splitting |
| FX-4.6 | **Tenant-scoped caches** for query embeddings and (versioned) results. See [IMP-19](#imp-19). | M | Hit-rate metric; a deleted document is never served from cache |
| FX-4.7 | **Search micro-optimisations** (planner settings, round trips, mid-size band). See [IMP-21](#imp-21). | S | p95 search time down without recall loss |
| FX-4.8 | **Faster ingestion:** bounded-concurrency embedding batches; extraction moved to the worker with originals stored. See [IMP-20](#imp-20). | M | Time-to-ready for a 400,000-char document roughly halves |
| FX-4.9 | **Row-level security** on the four knowledge tables as a fail-closed backstop. See [IMP-24](#imp-24). | L | A query without a tenant predicate returns 0 rows under the runtime role |
| FX-4.10 | **Knowledge-gap report** of empty and low-confidence searches for workspace admins. See [IMP-25](#imp-25). | M | Empty-search share per workspace falls over time |

### 10.2 Suggested order of delivery

1. **Week 1:** Phase 0 · FX-1.3 · FX-1.4 · FX-1.10 · FX-1.12 · FX-1.13 · FX-3.1 (eval baseline).
2. **Week 2:** FX-1.1 · FX-1.2 · data repair + `reindex-all` · re-measure.
3. **Weeks 3–4:** FX-2.1 · FX-2.2 · FX-2.3 · FX-2.4 · FX-2.5 · remaining Phase 1 smalls.
4. **Weeks 5–6:** FX-3.2 · FX-3.3 · FX-3.5 · FX-3.8 · FX-3.9 · FX-3.13 · re-measure.
5. **Weeks 7–8:** FX-3.4 · FX-3.11 · FX-3.12 · FX-3.14 · FX-3.15 · FX-3.16 · re-measure.
6. **Then:** FX-3.6 · FX-3.7 · FX-3.17 · Phase 4 (including FX-4.6 – FX-4.10) · FX-2.6 / FX-2.7.

### 10.3 How success is measured

| Metric | Source | Target (to confirm against the baseline) |
|---|---|---|
| recall@5 on the eval set (Arabic / English / cross-lingual) | FX-3.1 | ≥ 0.85 / 0.90 / 0.75 |
| Answerable-but-empty rate | FX-3.1 | < 5% |
| Duplicate passages per search | FX-3.4 metric | ≈ 0 |
| Documents with more than one served version of the same source | FX-2.2 / FX-4.4 gauge | 0 |
| Searches failing for infrastructure reasons | `wasla_rag_retrievals_total{outcome="failed"}` | < 0.5% |

---

## 11. Improvement recommendations

> [!NOTE]
> Sections 6–10 fix what is broken. This section answers a different question: **what would make answers more accurate, more complete, faster and cheaper — without weakening security or tenant isolation?** Every recommendation is tied to the current code (`file:line`), names the plan item that delivers it, and says how to prove it worked. Numbers marked *measured* come from this review's runs; everything else is an expectation to confirm with [IMP-01](#imp-01).

**Goals:** 🎯 retrieval accuracy · ✅ answer correctness and completeness · ⚡ speed · 💰 cost · 🛡️ security and isolation

**Priority:** 🟥 High · 🟨 Medium · 🟦 Low (squares, so they are not confused with bug severities) · **Effort:** S / M / L as in [§10](#10-fix-plan)

### 11.1 Guardrails every recommendation keeps

These are the security and isolation rules the current code already follows. Each card's **Isolation & security** row says how the change keeps them.

| # | Guardrail | Where it holds today |
|---|---|---|
| G1 | The tenant comes only from server context (the worker's job, `ToolContext`), never from model output or a request that can name another workspace. | `agents/registry.py:514-520` |
| G2 | Every query path applies the tenant predicate on the rows it reads, lives in the repository layer, and has a cross-tenant property test. | `knowledge_repository.py:380-447` (ADR-008) |
| G3 | Nothing is shared across tenants — caches, dedupe lookups, embedding reuse, re-rank batches — even for byte-identical text, so there is no existence or timing side channel. | rule for new code |
| G4 | Deleted or replaced content stops being served at once; any cache is invalidated by a per-tenant knowledge version. | `knowledge_service.py:464-494` (hard delete) |
| G5 | Retrieved text reaches the model only inside the structured tool output, never in `instructions`. | `retrieval_service.py:147-194` (RAG-12) |
| G6 | The server owns top-k, thresholds and context size. | `retrieval_service.py:102-117` (RAG-15) |
| G7 | No customer text in logs or metric labels; any stored query text is tenant-scoped, access-controlled, retention-limited and erased by the workspace purge. | `retrieval_service.py:297-305`, `workspace_purge_service.py:163-168` |
| G8 | Provider data handling stays as strict: `store: false`; a new processor (re-ranker, embedding host) needs zero-retention terms and a DPA, or is self-hosted. | `openai/client.py:31-33, 249` |
| G9 | Every change ships behind a per-workspace flag and is measured with IMP-01 before it is widened. | process |

### 11.2 Where the time and the money go today

One answered knowledge question, on the current code path:

| Stage (in order) | What happens | Latency | Cost driver | Changed by |
|---|---|---|---|---|
| Sentiment classification | a separate small-model call, awaited before the agent starts (`orchestrator.py:461-486`) | one model round trip | short prompt | [IMP-18](#imp-18) |
| Round 1 | the agent model asks for `search_knowledge` | one model round trip | full prompt **P** = instructions + up to 4 tool schemas + history (memory budget 4,000 estimated tokens, `db/models/agent.py:60`) | [IMP-17](#imp-17) removes it for most knowledge turns |
| Query embedding | one embeddings request | one HTTP round trip | negligible (tens of tokens) | [IMP-19](#imp-19) |
| Vector search | planner settings + one SQL statement | 2–54 ms (measured in `docs/RAG.md`) | database | [IMP-21](#imp-21) |
| Round 2 | the agent model answers | one model round trip + generation | **P again** (`store: false`, no `previous_response_id`: `openai/client.py:249`) + the tool exchange, up to 6,000 chars | [IMP-22](#imp-22), [IMP-16](#imp-16) |
| Extra rounds | 0–1 re-searches | one model round trip each | P + growing tool output; one response may request up to 8 searches (`orchestrator.py:89`) | [IMP-09](#imp-09), [IMP-13](#imp-13) |
| Empty turn | fallback message + handoff to a person (`ai_worker.py:950-982`) | — | staff time | [IMP-13](#imp-13) |

So a typical knowledge answer costs roughly **2 × P + context** input tokens and **three sequential model calls** before anything is sent. The embedding and the SQL are rounding errors by comparison — which is why the speed and cost recommendations target model rounds, not the vector index.

```mermaid
flowchart LR
    subgraph Today
        direction LR
        T1["sentiment call"] --> T2["round 1: ask to search"] --> T3["embed + SQL"] --> T4["round 2: answer"] --> T5["send"]
    end
    subgraph Proposed
        direction LR
        P1["embed + SQL on the message"] --> P3["one round: answer with passages"]
        P2["sentiment call, in parallel"] --> P4{"escalate?"}
        P3 --> P4
        P4 -->|no| P5["send"]
        P4 -->|yes| P6["discard reply, hand off"]
    end
```

### 11.3 Summary

● primary goal · ○ secondary goal

| ID | Recommendation | 🎯 | ✅ | ⚡ | 💰 | 🛡️ | Priority | Effort | Plan |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|
| [IMP-01](#imp-01) | Retrieval evaluation set and distance telemetry | ● | ● | | | | 🟥 High | M | FX-3.1 |
| [IMP-02](#imp-02) | Cost and latency accounting | | | ● | ● | | 🟨 Medium | S | FX-1.13 |
| [IMP-03](#imp-03) | Normalise Unicode and Arabic text on both sides | ● | ○ | | | | 🟥 High | S–M | FX-1.1 |
| [IMP-04](#imp-04) | Structure-aware chunking | ● | ● | | | | 🟥 High | M | FX-1.2 |
| [IMP-05](#imp-05) | Embed chunks with document and section context | ● | | | | | 🟥 High | S | FX-3.2 |
| [IMP-06](#imp-06) | Replace-in-place lifecycle and workspace dedupe | ○ | ● | | ○ | ○ | 🟥 High | L | FX-2.1 – FX-2.5 |
| [IMP-07](#imp-07) | Row- and FAQ-aware ingestion | ● | ● | | | | 🟨 Medium | M | FX-3.17 |
| [IMP-08](#imp-08) | Hybrid lexical + vector retrieval | ● | | | | | 🟥 High | M | FX-3.3 |
| [IMP-09](#imp-09) | Standalone and bilingual queries | ● | ○ | | | | 🟥 High | S | FX-3.5 |
| [IMP-10](#imp-10) | Re-ranking and diversity | ● | ○ | | | | 🟨 Medium | M | FX-3.4 |
| [IMP-11](#imp-11) | Per-agent scope and metadata filters | ● | | | | ○ | 🟨 Medium | M | FX-3.6, FX-2.7 |
| [IMP-12](#imp-12) | Stronger embedding model for Arabic | ● | | | | | 🟨 Medium | S | FX-3.7 |
| [IMP-13](#imp-13) | Tool loop: final round and transcript order | | ● | ○ | ○ | | 🟥 High | S | FX-1.10 |
| [IMP-14](#imp-14) | Grounding guard for numbers | | ● | | | | 🟥 High | M | FX-3.13 |
| [IMP-15](#imp-15) | Neighbour expansion | | ● | | | | 🟨 Medium | S | FX-3.11 |
| [IMP-16](#imp-16) | Confidence tiers | | ● | | | | 🟨 Medium | S | FX-3.12 |
| [IMP-17](#imp-17) | Pre-retrieval before the first round | | ○ | ● | ● | | 🟨 Medium | M | FX-3.14 |
| [IMP-18](#imp-18) | Sentiment off the critical path | | | ● | | | 🟨 Medium | M | FX-3.15 |
| [IMP-19](#imp-19) | Tenant-scoped query and result caches | | | ● | ○ | | 🟦 Low | M | FX-4.6 |
| [IMP-20](#imp-20) | Faster time-to-searchable | | | ● | | | 🟦 Low | M | FX-4.8 |
| [IMP-21](#imp-21) | Search micro-optimisations | | | ● | | | 🟦 Low | S | FX-4.7 |
| [IMP-22](#imp-22) | Cache-friendly, compact prompts | | | ○ | ● | | 🟨 Medium | S | FX-3.16 |
| [IMP-23](#imp-23) | Isolation test gate for new paths | | | | | ● | 🟥 High | S per path | FX-1.12 |
| [IMP-24](#imp-24) | Row-level security backstop | | | | | ● | 🟨 Medium | L | FX-4.9 |
| [IMP-25](#imp-25) | Knowledge-gap report | ○ | ● | | | | 🟨 Medium | M | FX-4.10 |
| [IMP-26](#imp-26) | Admin retrieval preview | ○ | ○ | | | | 🟦 Low | S | FX-3.9 |

### 11.4 Measure first

#### IMP-01
**Retrieval evaluation set and distance telemetry** · 🎯 ✅ · 🟥 High · Effort M · Plan [FX-3.1](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Retrieval quality has never been measured. The suite embeds with a hashed bag-of-words fake (`tests/fake_embeddings.py`), the `0.75` cutoff is uncalibrated (`retrieval_service.py:56-61`), and production records only found/empty/failed, duration and passage count — never a distance (`telemetry.py:977-983`). No other recommendation can be proven without this. |
| **Proposed solution** | (1) A golden set of 200–300 anonymised real questions across MSA, Egyptian dialect, English and Arabic↔English, each labelled with the chunk(s) that answer it, plus unanswerable questions. (2) An opt-in harness under `tests/real_provider/` that indexes a fixed corpus with the real embedding model and reports recall@5, MRR@10, nDCG@5 and the answerable-but-empty rate; answer-level checks on a subset with an LLM judge plus human spot checks. (3) Online metrics: best distance per search (histogram), passages dropped by the threshold, context characters returned — no tenant labels, no text. |
| **Rationale** | The metric plumbing (`record_retrieval`), the opt-in real-provider tests and a fixed-corpus fixture (`tests/integration/vector_corpus.py`) already exist; this extends them rather than adding a framework. |
| **Expected impact** | Turns threshold and ranking choices into measured decisions, catches regressions in CI, and gives every other card a before/after number. Indirectly, fewer false "no information" answers once the cutoff is calibrated. |
| **Priority** | 🟥 **High** — the prerequisite for validating everything else, and cheap relative to its value. |
| **Trade-offs** | One to two days of labelling per language mix, refreshed as content changes; each real-provider run costs a little (more with an LLM judge); the golden set must be anonymised and kept under the same controls as customer data. |
| **Validation** | Three repeated runs agree within ±1–2 points; the chosen threshold sits at a documented point on the precision/recall curve; dashboards show the distance histogram per embedding model. |
| **Isolation & security** | Golden data is anonymised; online metrics carry neither tenant labels nor text (G7). |

#### IMP-02
**Cost and latency accounting** · ⚡ 💰 · 🟨 Medium · Effort S · Plan [FX-1.13](#phase-1--correctness-fixes)

| | |
|---|---|
| **Problem** | Usage parsing keeps only `input_tokens`, `output_tokens` and `total_tokens` (`openai/types.py:28`). Cached input tokens (billed at a discount) and reasoning tokens are dropped, and there is no per-stage timing for a turn, so the effect of prompt caching, pre-retrieval or a re-ranker on spend and latency cannot be seen. |
| **Proposed solution** | Parse `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens` into the usage records; add per-stage histograms (sentiment, each round, embedding, SQL, re-rank) and context characters per search; one dashboard of cost and latency per answered turn. |
| **Rationale** | `TokenUsage.from_payload` is already the single validated entry point for usage (AI-11), and `usage_service.py` already records `ai_request` and `embedding_request` events — this adds fields, not a pipeline. |
| **Expected impact** | Makes every 💰 and ⚡ card measurable and shows the real cached-token share, which may already be significant. |
| **Priority** | 🟨 **Medium** — no direct customer-visible gain, but small, and it gates the cost work. |
| **Trade-offs** | Provider-specific fields that must stay optional, as today; a few more metric series. |
| **Validation** | Monthly reconciliation with the provider invoice within a few percent; cached-token share visible per model. |
| **Isolation & security** | Usage rows are already tenant-scoped; new metrics carry no tenant label (G7). |

### 11.5 Better passages in the index

#### IMP-03
**Normalise Unicode and Arabic text on both sides** · 🎯 · 🟥 High · Effort S–M · Plan [FX-1.1](#phase-1--correctness-fixes) · fixes RV-01, RV-04, RV-23

| | |
|---|---|
| **Problem** | Arabic PDF text can be stored as presentation forms that share no code points with a normally typed query (0/8 words before NFKC, 8/8 after — *measured*, [A.3](#a3-arabic-pdf-extraction)); mixed Arabic/number lines are re-ordered; the query is not normalised either. NFKC alone does **not** unify digits: `٤٥٠٠` stays Arabic-Indic (*verified*), so a customer typing `٤٥٠٠` and a document saying `4500` still differ. |
| **Proposed solution** | One shared module with two functions. `for_storage`: NFKC, then strip NUL, zero-width characters and bidi controls (U+200E/200F, U+202A–202E, U+2066–2069). `for_matching`: `for_storage` plus removal of tatweel (U+0640) and Arabic diacritics, and Arabic-Indic/Persian digits mapped to ASCII. Apply `for_storage` to extracted and pasted text (`knowledge_service.py:129-158`) and `for_matching` to both the embedding input and the query (`retrieval_service.py:249`). Keep heavier folding (alef forms, ى/ي, ة/ه) for the lexical index only ([IMP-08](#imp-08)). Flag lines where digits are glued to Arabic letters as possibly re-ordered. |
| **Rationale** | Pure Python at two existing choke points; the codebase already NFKC-normalises opt-out keywords (`opt_out.py:105`), so the approach is established here. |
| **Expected impact** | Arabic PDFs from tools that emit presentation forms become retrievable at all; price questions match whichever digit script the customer types. The mechanism is measured; the recall gain is to be quantified with IMP-01. |
| **Priority** | 🟥 **High** — the largest single accuracy defect for an Arabic-first product, and small to build. |
| **Trade-offs** | Changes `content_hash` (back-fill and collision handling — see the data-repair note in §10); needs a re-index (cheap); re-ordered lines cannot be repaired without the original file ([IMP-20](#imp-20), FX-2.6); aggressive folding can merge distinct words, which is why it stays in the lexical channel. |
| **Validation** | Unit fixtures for presentation forms, digits, diacritics and tatweel; the presentation-form query in Appendix C returns 0 after the back-fill; Arabic-PDF eval subset before/after. |
| **Isolation & security** | A pure function; no data moves. |

#### IMP-04
**Structure-aware chunking** · 🎯 ✅ · 🟥 High · Effort M · Plan [FX-1.2](#phase-1--correctness-fixes) · fixes RV-05, RV-06, RV-08, RV-09, RV-22

| | |
|---|---|
| **Problem** | Unpunctuated text is hard-cut every 1,000 characters and the overlap is cut at any character and glued with a blank line, producing passages like `price 13\n\n,795 EGP` and `مدا\n\nر` ([A.2](#a2-hard-split-and-overlap-glue)); headings end up in the previous chunk and a page number became a chunk ([A.1](#a1-chunker)). |
| **Proposed solution** | Boundary priority: heading → blank line → line → sentence (`. ! ? ؟ ۔ ؛`) → whitespace → hard cut, never inside a run of digits. Overlap by whole sentences or words (≤ 150 chars) joined with a single space or newline. Minimum length applied to the piece, with short pieces merged into a neighbour. List and table lines treated as atomic. Repeated page headers/footers and bare page numbers removed. A heading breadcrumb kept per chunk. Size measured with the existing `estimate_tokens` instead of characters, so chunk size is comparable across scripts. |
| **Rationale** | `chunking.py` is a 172-line pure module with its own unit tests; none of this needs a schema change (the breadcrumb can be used by [IMP-05](#imp-05) at embedding time and stored later). |
| **Expected impact** | No split numbers or words in passages the model quotes (fewer wrong prices); headed sections retrieve with their heading; fewer junk chunks competing for the 4–5 slots. |
| **Priority** | 🟥 **High** — it causes wrong numbers today, and the change is contained. |
| **Trade-offs** | A full re-index (cheap; old generations keep serving); chunk counts shift (check `MAX_CHUNKS_PER_DOCUMENT`); more logic to maintain — keep it table-driven and property-tested. |
| **Validation** | Property tests: no boundary or overlap start inside a word or a number; every number in the source appears intact in some chunk; every chunk of a section carries its heading; the A.1 and A.2 probes pass; list/table eval subset before/after. |
| **Isolation & security** | A pure function. |

#### IMP-05
**Embed chunks with their document and section context** · 🎯 · 🟥 High · Effort S · Plan [FX-3.2](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Only the raw chunk text is embedded (`document_indexing.py:677`). A chunk saying "costs 7200 EGP per square metre" carries no signal that it is about *Premium finishing* in the *Price list* unless those words happen to fall inside it — and today's overlap cuts make that less likely. |
| **Proposed solution** | Embed `"{title} › {heading breadcrumb}\n\n{chunk}"` while storing and showing the chunk alone. Start with the title (available today); add breadcrumbs with [IMP-04](#imp-04). A later, optional upgrade: a one-sentence, LLM-written context per chunk at ingestion, with the whole document prompt-cached. |
| **Rationale** | A one-line change at the embedding call. The query side is unchanged, so old and new vectors stay comparable and `EMBEDDING_SCHEMA_VERSION` need not be bumped. Anthropic's *Contextual Retrieval* write-up (2024) reports about 35% fewer top-20 retrieval failures from contextualised embeddings, about 49% with contextual BM25 added and about 67% with re-ranking on top — on their benchmarks, with LLM-written context; title plus heading is the cheap version of the same idea. |
| **Expected impact** | Better recall for short, context-dependent passages and for queries that name a product or policy rather than quoting it. |
| **Priority** | 🟥 **High** — a very small change for a well-documented kind of gain. |
| **Trade-offs** | 10–30 extra tokens per chunk at ingestion (negligible at embedding prices); a meaningless title ("Untitled", "scan_003.pdf") adds noise — fall back to the filename or skip it; the LLM-written variant adds an ingestion cost per chunk. |
| **Validation** | A/B on the eval set: recall@5 and MRR with and without the prefix, per language. |
| **Isolation & security** | Uses only the document's own metadata. |

#### IMP-06
**Replace-in-place document lifecycle and workspace-wide dedupe** · ✅ 🎯 💰 🛡️ · 🟥 High · Effort L · Plan [FX-2.1 – FX-2.5](#phase-2--document-lifecycle-replace-never-duplicate) · fixes RV-02, RV-07, RV-13, RV-16

| | |
|---|---|
| **Problem** | An updated document becomes a second served document and identical text in two knowledge bases is served twice; old and new prices come back at the same distance with no recency signal (*measured*, [A.5](#a5-update--duplication-demo-on-real-postgresql)). Old copies beyond the newest 100 cannot even be listed. |
| **Proposed solution** | `PUT /knowledge/documents/{id}/content` creating a generation that carries its own content snapshot (the old one keeps serving until the swap); an optional `source_key` for idempotent upserts (`pricing.pdf` replaces itself); refuse or link an upload whose hash already exists in another knowledge base **of the same workspace**; keyset pagination and a "likely previous version" finder; `updated_at` per source in the tool output until supersession dates exist. |
| **Rationale** | The hard part already exists and is well tested — generations, fencing tokens, the atomic swap and coalescing (`document_indexing.py:418-490`). What is missing is a content snapshot per generation and an API to start one. |
| **Expected impact** | Removes the main source of contradictory answers; frees top-k slots and context tokens; fewer documents to embed and store; no coverage gap during an update. |
| **Priority** | 🟥 **High** — your explicit requirement, and a direct cause of wrong answers. |
| **Trade-offs** | A migration (snapshot columns, a `source_key` unique index), new endpoints and UI ("Replace" next to "Upload"); in-flight attempts for the old text must be cancelled cleanly; clients that relied on "new upload = new document" see different behaviour. |
| **Validation** | Integration tests: v1 serves while v2 builds; only v2 after publish; a failed v2 leaves v1 serving; concurrent PUTs coalesce; the A.5 demo returns only `7900 EGP`; the duplicate queries in Appendix C return nothing. |
| **Isolation & security** | Dedupe and `source_key` lookups are scoped by `tenant_id` only — a global hash lookup would reveal that another workspace holds the same document (G3). Snapshots live on the already tenant-keyed generation table with its composite foreign keys. |

#### IMP-07
**Row- and FAQ-aware ingestion for catalogues and Q&A** · 🎯 ✅ · 🟨 Medium · Effort M · Plan [FX-3.17](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Price lists and catalogues are what customers ask about most, yet their rows are chunked as prose and cut mid-number ([A.2](#a2-hard-split-and-overlap-glue)); FAQ documents pack several Q&As into one chunk, which dilutes each match; CSV/XLSX are not accepted at all (FUT-RAG-09). |
| **Proposed solution** | Accept CSV first (standard library), XLSX later (parsed in the sandboxed child process like PDFs). Each row becomes one chunk rendered as `Column: value; Column: value`, with the header and document title repeated. Detect Q&A patterns (`Q:`/`A:`, `س:`/`ج:`, numbered questions) and keep each pair as one chunk. |
| **Rationale** | Fits the existing chunk model (one row = one chunk with an ordinal) and leaves the generation pipeline unchanged; the bounded child-process pattern (`extraction.py:96-164`) covers the new binary format. |
| **Expected impact** | The exact row is retrieved for price and availability questions, and numbers are never split; FAQ questions match their own answer. |
| **Priority** | 🟨 **Medium** — high value for catalogue-heavy workspaces, but it is a new input format with UI work; do it after IMP-03 and IMP-04. |
| **Trade-offs** | New parsing surface needs PDF-style limits (rows, columns, cell size, time); a 5,000-row catalogue exceeds `MAX_CHUNKS_PER_DOCUMENT` (1,000), so it needs its own limit and plan pricing; very wide rows need truncation rules. |
| **Validation** | Exact-price eval subset (the answer names the right product and price); limits tests; ingestion time for a large CSV. |
| **Isolation & security** | Same tenant-scoped pipeline; binary formats parsed only in the secret-free child process. |

### 11.6 Better search

#### IMP-08
**Hybrid retrieval: lexical + vector, fused** · 🎯 · 🟥 High · Effort M · Plan [FX-3.3](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Embeddings are weak at exact tokens — SKUs, model numbers, phone numbers, brand names in Latin script inside Arabic text — and at rare words, and there is no lexical channel to catch them (`knowledge_repository.py:380-447` is vector-only). |
| **Proposed solution** | Store a `search_text` column ([IMP-03](#imp-03)'s matching form plus Arabic light folding) with a generated `tsvector` (`'simple'` configuration) and a GIN index. Run lexical top-20 and vector top-20 with **identical** tenant/active/space predicates and fuse them with Reciprocal Rank Fusion (k ≈ 60); pass the fused list to [IMP-10](#imp-10). |
| **Rationale** | Native PostgreSQL, no new service. The repository is already the one place tenant predicates live (ADR-008), so the lexical query sits next to the vector query; RRF needs no score calibration between the two channels. |
| **Expected impact** | Large gains on exact-term questions and a safety net when embeddings miss dialect or rare words — independent of the embedding model. |
| **Priority** | 🟥 **High** — commerce questions are full of exact identifiers; moderate effort and no new infrastructure. |
| **Trade-offs** | GIN storage and extra write cost at publish; one more query per search (single-digit to low tens of ms); `'simple'` has no Arabic stemming, only the light folding; adding a column to a large table needs a batched back-fill and `CREATE INDEX CONCURRENTLY`, like migration `0039`. |
| **Validation** | Exact-identifier eval subset recall@5; overall MRR; search p95 within +15 ms. |
| **Isolation & security** | The lexical query carries the same triple tenant predicate and joins the active generation; covered by [IMP-23](#imp-23); parameters are bound, never interpolated. |

#### IMP-09
**Standalone and bilingual queries** · 🎯 ✅ · 🟥 High · Effort S · Plan [FX-3.5](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | The tool asks for the query "in the customer's own words" (`agents/registry.py:560`), so follow-ups ("and the premium one?"), dialect, and Arabic questions about English documents are embedded as typed. |
| **Proposed solution** | Rewrite the parameter description to ask for a short, self-contained query (references resolved from the conversation, the product or service named, no greetings); add an optional `alternate_query` "in the language the documents are written in"; embed both in one batched request and fuse the two result lists with RRF. |
| **Rationale** | The agent model already writes the query inside the same tool call, so this adds no model call, and a two-input embedding batch takes the same time as one (the client already batches). |
| **Expected impact** | Better recall on follow-ups and cross-lingual questions; fewer false "no information" answers. |
| **Priority** | 🟥 **High** — a prompt and schema change with clear upside. |
| **Trade-offs** | The model's translation can be poor; the (cheap) search work doubles; the schema change must be checked against the real provider (`tests/real_provider/`). |
| **Validation** | Follow-up and cross-lingual eval subsets; share of calls using `alternate_query` (argument field names are already recorded per call). |
| **Isolation & security** | Queries are model output and already length-bounded; the tenant still comes only from context (G1). |

#### IMP-10
**Re-ranking and diversity** · 🎯 ✅ · 🟨 Medium · Effort M · Plan [FX-3.4](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | The final order is raw cosine distance; overlapping chunks, duplicates and near-identical passages take slots among the 4–5 that fit the context (*measured*, [A.6](#a6-context-budget)). |
| **Proposed solution** | Retrieve 20–30 fused candidates ([IMP-08](#imp-08), [IMP-09](#imp-09)), re-rank with a cross-encoder, then drop near-duplicates (same text hash, high token overlap) and cap passages per document. Options, in order of fit: (a) LLM re-rank with the provider already in use and a small model, one call scoring all candidates; (b) a self-hosted multilingual cross-encoder (for example `bge-reranker-v2-m3`, which covers Arabic); (c) a hosted re-rank API. Always fall back to the fused order on timeout or error. |
| **Rationale** | `RetrievalService.search` is the single choke point and already has failure containment (`KnowledgeSearchUnavailableError`, a savepoint), so a re-ranker with a strict timeout and a fallback can be added without touching the agent loop. |
| **Expected impact** | Higher precision in the passages the model actually reads; fewer answers built on a nearby-but-wrong passage; duplicate slots near zero. |
| **Priority** | 🟨 **Medium** — real gains, but it adds latency and cost per search and needs IMP-01 to tune; do it after IMP-03 – IMP-08. |
| **Trade-offs** | +100–400 ms per search (hosted or LLM) or a new service to run (self-hosted); a per-search cost; option (c) adds a new data processor that needs zero-retention terms and a DPA. |
| **Validation** | nDCG@5 and MRR deltas on the eval set; search p95; cost per 1,000 searches; duplicate-passage rate. |
| **Isolation & security** | Only already tenant-filtered candidates are sent; candidates from different tenants are never batched in one request; no provider-side retention (G3, G8). |

#### IMP-11
**Per-agent knowledge scope and metadata filters** · 🎯 🛡️ · 🟨 Medium · Effort M · Plan [FX-3.6](#phase-3--retrieval-quality), [FX-2.7](#phase-2--document-lifecycle-replace-never-duplicate)

| | |
|---|---|
| **Problem** | Every agent searches every knowledge base in its workspace (`agents/registry.py:536` passes no knowledge base), and expired documents cannot be excluded. |
| **Proposed solution** | Accept `{"knowledge_base_ids": [...]}` in the `search_knowledge` grant config (refused today at `agent_service.py:55-72`), pass it through `ToolContext` to an `IN` filter in the repository, and later add `valid_from`/`valid_until` and language as SQL predicates. |
| **Rationale** | The repository already supports a knowledge-base filter, and `agent_tools.config` was designed for exactly this kind of per-grant setting. |
| **Expected impact** | Higher precision for multi-department workspaces; least-privilege knowledge access per agent. |
| **Priority** | 🟨 **Medium** — significant for workspaces with several knowledge bases; many have only "General". |
| **Trade-offs** | Configuration UX and validation (an agent scoped to an empty knowledge base always answers "no information" — warn about it); one more predicate on the ANN path (the iterative scan handles it). |
| **Validation** | Integration test: a scoped agent never receives another knowledge base's passages; precision on a multi-KB eval workspace. |
| **Isolation & security** | Tightens access *inside* a workspace; knowledge-base ids are validated against the tenant when the grant is saved, so a config can never point at another workspace's data. |

#### IMP-12
**Evaluate a stronger embedding model for Arabic** · 🎯 · 🟨 Medium · Effort S (trial) · Plan [FX-3.7](#phase-3--retrieval-quality), [FX-4.1](#phase-4--scale-and-operations)

| | |
|---|---|
| **Problem** | `text-embedding-3-small` (`config.py:482`) is OpenAI's most economical embedding model, but it was not chosen for Arabic, dialect or cross-lingual retrieval, and nobody has measured it on this product's content. |
| **Proposed solution** | Benchmark on the IMP-01 set: `text-embedding-3-small`, `text-embedding-3-large` with `dimensions=1536`, and an open multilingual model (for example `bge-m3` or `multilingual-e5-large`) if self-hosting is acceptable. Switch only through the zero-downtime migration (FX-4.1). |
| **Rationale** | The `vector(1536)` column already fits `-large` shortened to 1,536 dimensions, and embedding-space tracking (`embedding_space.py`) already prevents mixing spaces. |
| **Expected impact** | Possibly notable Arabic and cross-lingual recall gains — to be measured. Embeddings are a small share of total spend, so a pricier model barely moves cost. |
| **Priority** | 🟨 **Medium** — only meaningful after IMP-03 and IMP-04; before them you would be measuring corrupted text. |
| **Trade-offs** | A full re-index; without FX-4.1 a switch hides every document until it is re-indexed (RV-11); self-hosting adds CPU/GPU capacity and operations. |
| **Validation** | Per-language recall@5 and MRR; embedding cost per 1,000 documents; a staging rehearsal of the switch. |
| **Isolation & security** | A self-hosted model keeps document text in-house; a new hosted provider needs a DPA (G8). |

### 11.7 Fewer wrong or incomplete answers

#### IMP-13
**Fix the tool loop: final round and transcript order** · ✅ ⚡ 💰 · 🟥 High · Effort S · Plan [FX-1.10](#phase-1--correctness-fixes) · fixes RV-14, RV-27

| | |
|---|---|
| **Problem** | On round 3 tools are offered (`orchestrator.py:559`) but refused (`:982-992`), so the turn ends empty (fallback message plus a handoff to a person) or sends a tool-call preamble such as "let me check" as the whole reply (`:567`). Multi-round transcripts are also re-sent out of order (RV-27), which confuses the model and breaks prompt-cache prefixes. A single response may also request up to 8 searches (`orchestrator.py:89`), each adding up to 6,000 characters to the next prompt. |
| **Proposed solution** | On the last round send no tools (or `tool_choice: "none"`) with an instruction to answer from what it has; treat text that came with a tool call as a preamble, never as the final reply; build the request input as a chronological list of per-round items (text, calls, outputs); cap `search_knowledge` at two calls per turn. |
| **Rationale** | Local changes in the round loop and in `ResponsesClient.respond`'s input assembly (`openai/client.py:241-243`); the loop is heavily tested and mutation-checked, so regressions show up. |
| **Expected impact** | Fewer empty turns and handoffs, no "let me check"-only replies, cheaper multi-round turns (stable cache prefix, bounded tool output). |
| **Priority** | 🟥 **High** — small, and it removes customer-visible incomplete answers and needless staff work. |
| **Trade-offs** | Touches a delicate loop with many invariants (PD-TOOLS-02/05, CRM-02); a terminal handoff must still be allowed on the last round. |
| **Validation** | Scripted-provider unit tests: three searches still end in an answer, and input order is chronological; in production, `EMPTY_RESPONSE` outcomes and `agent.round_limit_reached` logs fall. |
| **Isolation & security** | No change to the authorisation gates. |

#### IMP-14
**Grounding guard for numbers in replies** · ✅ · 🟥 High · Effort M · Plan [FX-3.13](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Nothing checks that a price, fee or percentage in a reply appears in the retrieved passages; grounding is prompt-only (PD-RAG-8, no grounded-only mode). A wrong number is the most damaging answer a sales or support agent can give. |
| **Proposed solution** | Before sending, extract currency amounts and percentages (after digit normalisation) from the reply and check each against this turn's retrieved passages and the customer's own messages. On a miss: one corrective round ("X is not in the sources — correct it or say you don't have it"), or, for agents in an opt-in strict mode, a safe "I'll confirm with a colleague" plus a handoff. Count the outcomes. |
| **Rationale** | The orchestrator already has a post-composition gate before sending (`orchestrator.py:646-723`, the take-over check) and holds the turn's tool outputs, so the check runs where the data already is — deterministically, with no extra model call in the common case. |
| **Expected impact** | Directly reduces invented or stale prices and fees reaching customers. |
| **Priority** | 🟥 **High** — it targets the costliest error class and is cheap per turn. |
| **Trade-offs** | False positives on derived numbers (totals, conversions, dates) — limit it to currency amounts and percentages and allow arithmetic on grounded numbers; a corrective round adds latency and tokens on the affected turns. |
| **Validation** | Replay eval conversations: ungrounded-number rate before/after; false-positive rate on correct answers (target under 2%); corrective-round frequency in production. |
| **Isolation & security** | Runs in-process on the turn's own data. |

#### IMP-15
**Neighbour expansion for complete answers** · ✅ · 🟨 Medium · Effort S · Plan [FX-3.11](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Answers that span chunks — a price table that continues, a policy and its exception — come back half-complete; the 150-character overlap rarely covers them. |
| **Proposed solution** | For the top 2–3 passages, fetch ordinal ±1 from the same active generation and merge contiguous chunks into one passage (dropping the overlap), only while the context budget allows, preferring expansion over adding lower-ranked unrelated passages. |
| **Rationale** | Chunks already carry `generation_id` and `ordinal`, and the unique constraint `uq_document_chunks_tenant_id_generation_id_ordinal` (`db/models/knowledge.py:362-367`) provides the index for an exact, cheap lookup. |
| **Expected impact** | Fewer incomplete answers on tables, lists and conditional policies. |
| **Priority** | 🟨 **Medium** — clear benefit and a small change; best after IMP-04 (clean boundaries) and with the token budget (FX-3.8). |
| **Trade-offs** | More context tokens per answer; it can dilute focus if applied blindly — expand only top passages, and only within the same section. |
| **Validation** | Eval subset whose answers span two chunks: completeness by LLM judge plus human spot checks; context tokens per turn. |
| **Isolation & security** | The neighbour query carries the tenant predicate and joins the active generation (G2). |

#### IMP-16
**Confidence tiers instead of one cutoff** · ✅ · 🟨 Medium · Effort S · Plan [FX-3.12](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | A passage is either kept (distance ≤ 0.75) or dropped (`retrieval_service.py:291`); the model cannot tell a strong match from a marginal one, so marginal matches are quoted as confidently as strong ones. |
| **Proposed solution** | After calibration (IMP-01), classify each search as strong, partial or none, add a coarse `match` field to the tool output, and instruct: strong → answer; partial → answer cautiously or ask one clarifying question; none → say it is not available and offer a colleague. Thresholds are per embedding model and stay server-side. |
| **Rationale** | The tool output is already a structured object (`retrieval_service.py:177-194`) and the server already owns thresholds (RAG-15), so this adds one field and keeps the boundary. |
| **Expected impact** | Fewer confidently wrong answers from weak matches, and fewer needless "I don't know" replies when a partial match is genuinely useful. |
| **Priority** | 🟨 **Medium** — it needs calibration data first. |
| **Trade-offs** | Thresholds must be recalibrated after any model or normalisation change; the model may over-hedge. |
| **Validation** | Near-miss eval subset: wrong-answer rate; handoff and clarifying-question rates in an A/B. |
| **Isolation & security** | No change. |

### 11.8 Faster responses

#### IMP-17
**Pre-retrieval: search before the first model round** · ⚡ 💰 ✅ · 🟨 Medium · Effort M · Plan [FX-3.14](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | A knowledge answer needs at least two agent rounds — the first only to ask for the search — and each round re-sends the full prompt (`store: false`, no `previous_response_id`: `openai/client.py:249`). |
| **Proposed solution** | Before round 1, embed the latest customer message (plus the previous one if it is very short) and search. If the best match is strong or partial ([IMP-16](#imp-16)), include the passages in round 1 as a recorded `search_knowledge` call-and-output pair — the same structured boundary the tool uses — and keep offering the tool for refinement. Skip greetings, very short and media-only messages, and agents without the grant. Send the embedding request concurrently with the sentiment call (both are HTTP; database work stays sequential on the session). |
| **Rationale** | `ToolResult.to_input` already produces call/output pairs (`openai/types.py:157-176`) and `RetrievalService` can be called directly; the executor's grant and serving-state checks can be reused, so the pre-search is authorised and recorded like any other tool call. |
| **Expected impact** | One fewer model round trip on most knowledge questions (typically a second or more) and one fewer full prompt (partly offset where prompt caching already applies), with the embedding hidden behind the sentiment call. |
| **Priority** | 🟨 **Medium** — the biggest speed lever, but it needs calibration (IMP-01, IMP-16) so weak context is not injected, and IMP-13 first. |
| **Trade-offs** | Searches run on turns that did not need them (cheap) and add tokens if the gate is loose; the latest message alone can be a poor query for follow-ups (the tool stays available); the synthesised call must be written to `tool_executions` so the audit trail stays honest; more orchestrator complexity. |
| **Validation** | Per-workspace A/B: p50/p95 turn latency, rounds per knowledge turn, input tokens per turn, accuracy on eval conversations unchanged or better, and the share of turns where the model still searched again. |
| **Isolation & security** | Same tenant-scoped service; only for agents granted `search_knowledge` (re-checked like any call); passages stay inside `function_call_output` (G5). |

#### IMP-18
**Take sentiment classification off the critical path** · ⚡ · 🟨 Medium · Effort M · Plan [FX-3.15](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Each new customer message is classified by a separate model call that the agent waits for before composing (`orchestrator.py:461-486`, `sentiment_service.py:108-175`) — a full model round trip added to every turn. |
| **Proposed solution** | Start the classification and round 1 together and gate **sending** on the result. If escalation is required, discard the composed reply and hand off — the same discard path already used when a colleague takes over mid-turn (`orchestrator.py:654-684`). While the classification is pending, run only read-only tools (`search_knowledge`) and defer lead, follow-up and handoff tools until it passes. |
| **Rationale** | A reply is sent only after the orchestrator returns, so composing early never reaches an angry customer; the code's concern about an escalation "that arrives second" is about *sending*, which stays gated. |
| **Expected impact** | One model round trip less on typical turns. |
| **Priority** | 🟨 **Medium** — a solid latency gain, but it changes a deliberate design (AI-04) and needs careful concurrency work. |
| **Trade-offs** | Tokens wasted on escalated turns (a minority); `AsyncSession` is not safe for concurrent use, so only the HTTP calls may overlap; CRM-02 and AI-04 semantics and tests must hold. |
| **Validation** | p50/p95 latency; tests that an escalated turn never sends a reply or runs a side-effecting tool; tokens spent on escalated turns. |
| **Isolation & security** | No change to authorisation. |

#### IMP-19
**Tenant-scoped caching of query embeddings and results** · ⚡ 💰 · 🟦 Low · Effort M · Plan [FX-4.6](#phase-4--scale-and-operations)

| | |
|---|---|
| **Problem** | Every search re-embeds the query, even for repeated FAQs, and an embeddings outage fails even a question asked a minute ago. |
| **Proposed solution** | A Redis cache `wasla:rag:qemb:{tenant}:{space}:{sha256(normalised query)}` → vector (float32 bytes), TTL 24–72 h; optionally a result cache keyed additionally by a per-tenant **knowledge version** that increments on every publish, delete and knowledge-base change, so nothing replaced or deleted can be served from cache. |
| **Rationale** | Redis is already deployed, and the codebase already keys sensitive Redis entries by hash (`realtime.py:86`); `RetrievalService.search` is the one place to read through a cache. |
| **Expected impact** | Saves the embedding round trip on hits (roughly 100–300 ms); keeps repeated questions answerable during an embeddings outage; negligible cost savings, because query embeddings are cheap. |
| **Priority** | 🟦 **Low** — a modest gain, free-text questions rarely repeat byte-for-byte, and a result cache adds invalidation risk. |
| **Trade-offs** | Redis memory (~6 KB per vector); invalidation logic for results; vectors derived from customer text are sensitive (short TTL, purge on workspace deletion). |
| **Validation** | Hit-rate metric; latency of hits vs misses; a test that publish and delete invalidate results immediately. |
| **Isolation & security** | Tenant id and embedding space in every key; entries are never shared across tenants, even for identical text (G3); the result cache honours G4; keys are hashed so customer text is not readable in Redis. |

#### IMP-20
**Faster time-to-searchable for uploads** · ⚡ · 🟦 Low · Effort M · Plan [FX-4.8](#phase-4--scale-and-operations), [FX-2.6](#phase-2--document-lifecycle-replace-never-duplicate), [FX-1.4](#phase-1--correctness-fixes)

| | |
|---|---|
| **Problem** | Embedding batches run strictly one after another with two short transactions each (`document_indexing.py:673-680`), and PDF parsing happens inside the upload request under the plan lock (RV-10). |
| **Proposed solution** | Embed 2–4 batches concurrently under one renewal heartbeat; write chunks with a single multi-row insert; move PDF extraction to the worker by storing the original in the existing object store (which also enables re-extraction, FX-2.6), so the upload returns immediately. |
| **Rationale** | The 10-minute claim lease and the renewal design tolerate parallel batches; object storage is already used for media. |
| **Expected impact** | Roughly 2–4× faster indexing of large documents; uploads no longer block for up to 20 s; the lock-timeout risk goes away. |
| **Priority** | 🟦 **Low** for speed (ingestion runs in the background); the RV-10 part is already in Phase 1 (FX-1.4). |
| **Trade-offs** | Rate-limit pressure (back off concurrency on 429s); storing originals adds storage cost, retention rules and purge coverage. |
| **Validation** | Time-to-ready for a 400,000-character document; 429 rate; the concurrent-upload test from RV-10. |
| **Isolation & security** | Originals under tenant-prefixed object keys with the media subsystem's controls, and included in the workspace purge. |

#### IMP-21
**Search micro-optimisations** · ⚡ · 🟦 Low · Effort S · Plan [FX-4.7](#phase-4--scale-and-operations)

| | |
|---|---|
| **Problem** | Each search spends a separate round trip on `set_config` plus savepoint statements, and workspaces of roughly 3,000–26,000 chunks get the exact scan (about 42 ms) where the index would take about 2.4 ms (measured in `docs/RAG.md`). |
| **Proposed solution** | Set `plan_cache_mode` per role or per pool instead of per query; tune `hnsw.ef_search` with the eval set; raise statistics on `document_chunks.tenant_id`; revisit the mid-size band only if p95 shows it matters. |
| **Rationale** | Configuration-level changes around an already well-measured query. |
| **Expected impact** | Single- to low-double-digit milliseconds per search — small next to a model round trip. |
| **Priority** | 🟦 **Low** — the database is not the bottleneck. |
| **Trade-offs** | Role-level settings affect every query on that role; recall must be re-measured. |
| **Validation** | Search p50/p95 and recall@5 before/after. |
| **Isolation & security** | No predicate changes. |

### 11.9 Lower cost

> [!TIP]
> Most cost savings already appear above: [IMP-17](#imp-17) removes a full prompt per knowledge answer, [IMP-13](#imp-13) bounds runaway tool output, [IMP-06](#imp-06) stops embedding and serving duplicates, and [IMP-02](#imp-02) makes all of it measurable. Embedding spend is small; the model rounds are where the money goes ([§11.2](#112-where-the-time-and-the-money-go-today)).

#### IMP-22
**Cache-friendly, compact prompts** · 💰 ⚡ · 🟨 Medium · Effort S · Plan [FX-3.16](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Every round re-sends instructions, tool schemas, history and all tool outputs (`openai/client.py:241-249`); the prefix shifts between rounds (RV-27), which limits automatic prompt caching; the context JSON spends 13% of its characters on structure and 2.8% on document UUIDs the model does not need (*measured* with 4 typical passages); weak passages are included as long as they pass 0.75. |
| **Proposed solution** | Keep the prefix stable (instructions → tools → history, then rounds in order); replace `document_id` in the model-facing JSON with the short `source` number and keep the mapping server-side for citations; stop adding passages whose distance is far worse than the best one (margin calibrated with IMP-01); if the provider's `prompt_cache_key` parameter is available, set it per agent. |
| **Rationale** | `store: false` is a deliberate privacy choice (`openai/client.py:31-33`) that rules out provider-side conversation state; prefix caching is the compatible lever, and it applies automatically once prefixes are stable and long enough. |
| **Expected impact** | Lower input cost on every tool round (cached prefixes are billed at a discount); about 10% fewer context characters from compaction alone, more from dropping weak passages. |
| **Priority** | 🟨 **Medium** — small, safe and compounding; measure it with IMP-02. |
| **Trade-offs** | Citations must be mapped server-side (FUT-RAG-05); the adaptive cutoff needs calibration. |
| **Validation** | Cached-token share (IMP-02) rises; input tokens per knowledge turn fall; eval accuracy unchanged. |
| **Isolation & security** | Prompt caching only reuses computation for byte-identical prefixes within Wasla's own provider account; it moves no content between requests, and `store: false` stays (G8). |

### 11.10 Security and tenant isolation

#### IMP-23
**Isolation test gate for every new retrieval path** · 🛡️ · 🟥 High · Effort S per path · Plan [FX-1.12](#phase-1--correctness-fixes)

| | |
|---|---|
| **Problem** | This section adds new ways to read knowledge — lexical search, neighbour lookups, caches, re-ranking, pre-retrieval, replace-in-place. Each is a new place where a tenant predicate can be forgotten, and the code itself calls a cross-tenant search "the worst failure this system has available to it" (`knowledge_repository.py:3-7`). |
| **Proposed solution** | For each path, extend the existing property-style tests (two workspaces with near-identical content; assert no foreign document ids or text in any result, including fused, expanded, cached and re-ranked results); keep a mutation check that removing the predicate from the new query fails a test; add injection documents to the eval corpus to confirm retrieved text stays data. |
| **Rationale** | The suite is already written against the property rather than the implementation (`knowledge_repository.py:392-399`), and the project already runs mutation matrices; this applies the same discipline to new code. |
| **Expected impact** | Prevents cross-tenant leaks and prompt-injection regressions as the retrieval surface grows. |
| **Priority** | 🟥 **High** — a release gate for every other recommendation, and cheap per path. |
| **Trade-offs** | Test maintenance; injection tests against the real provider are non-deterministic and cost a little. |
| **Validation** | Mutation runs: deleting any new tenant predicate is caught; the injection corpus produces no instruction-following. |
| **Isolation & security** | This card is the safeguard. |

#### IMP-24
**Row-level security as a fail-closed backstop** · 🛡️ · 🟨 Medium · Effort L · Plan [FX-4.9](#phase-4--scale-and-operations)

| | |
|---|---|
| **Problem** | Isolation is enforced only by application predicates — redundant and well tested, but a single new query without them would leak, and the database has no row-level security anywhere in the repository. |
| **Proposed solution** | Enable RLS on `knowledge_bases`, `documents`, `document_index_generations` and `document_chunks` with `USING (tenant_id = current_setting('app.tenant_id', true)::uuid)`, so a missing setting matches nothing. Set it with `set_config('app.tenant_id', …, true)` at the start of every tenant-scoped transaction (an `after_begin` hook, re-applied after `released()` commits). Give the cross-tenant sweeps and the purge a separate role with `BYPASSRLS`. Roll out in report-only mode first, then enforce. |
| **Rationale** | The runtime and migration identities are already separate roles (`scripts/provision_runtime_db_role.py`), which RLS needs to be effective — table owners bypass policies. |
| **Expected impact** | A forgotten predicate returns nothing instead of another tenant's data; no change to accuracy or cost; a small planner overhead. |
| **Priority** | 🟨 **Medium** — today's isolation is strong; RLS is proportionate insurance as IMP-08, IMP-15, IMP-17 and IMP-19 add query paths. |
| **Trade-offs** | Operational complexity with pooled connections and mid-turn commits; a missing setting fails closed (an outage, not a leak), hence the staged rollout; sweeps and operator commands need the bypass role. |
| **Validation** | Under the runtime role, a query without a tenant predicate returns 0 rows; every cross-tenant test passes with RLS on; search p95 unchanged. |
| **Isolation & security** | This card is the improvement. |

### 11.11 Close the loop

#### IMP-25
**Knowledge-gap report for workspace admins** · ✅ 🎯 · 🟨 Medium · Effort M · Plan [FX-4.10](#phase-4--scale-and-operations)

| | |
|---|---|
| **Problem** | When a search finds nothing, nobody learns what customers asked that the documents do not cover. The question is deliberately never logged (`retrieval_service.py:301`), so gaps never close. |
| **Proposed solution** | A tenant-scoped table of empty and low-confidence searches holding the model-written query (not the raw customer message), its language, the best distance and a timestamp, with short retention (for example 30 days); an admin-only endpoint listing the most frequent unanswered topics, grouped by embedding similarity, so the business can add or fix documents. |
| **Rationale** | The data is already at hand where emptiness is decided (`retrieval_service.py:291-306`), and the project already has tenant-scoped tables, admin-only routes and a workspace purge to plug into. |
| **Expected impact** | Steadily fewer "I don't have that information" answers as workspaces fill real gaps; product value for admins. |
| **Priority** | 🟨 **Medium** — a compounding benefit, but it stores customer-derived text and needs a UI. |
| **Trade-offs** | Privacy: customer-derived queries need retention limits, role-based access, deletion on purge and possibly per-workspace opt-in; storage growth. |
| **Validation** | Empty-search share per workspace falls over time; documents added after viewing the report. |
| **Isolation & security** | `tenant_id` on every row with composite keys like the knowledge tables; added to the workspace purge list (`workspace_purge_service.py:163-168`); never in logs or metric labels (G7). |

#### IMP-26
**Admin retrieval preview** · 🎯 ✅ · 🟦 Low · Effort S · Plan [FX-3.9](#phase-3--retrieval-quality)

| | |
|---|---|
| **Problem** | Admins cannot see what an agent would retrieve; the `SearchRequest`/`SearchResponse` schemas exist with no route (`schemas/knowledge.py:204-229`), so problems such as RV-01 or duplicate versions surface only as bad customer answers. |
| **Proposed solution** | `POST /knowledge/search` for admins returning passages, distances, the confidence tier, which threshold or filter dropped what, and duplicate warnings; metered like any search. |
| **Rationale** | Reuses `RetrievalService` and the unused schemas, with the same admin-only authorisation as the other knowledge write routes. |
| **Expected impact** | Faster detection of bad documents and wrong expectations; fewer support escalations. |
| **Priority** | 🟦 **Low** — it does not change answers by itself; cheap to add when convenient. |
| **Trade-offs** | Each preview costs an embedding call, so it needs rate limiting. |
| **Validation** | Admin usage; time from upload to the first "looks right" check. |
| **Isolation & security** | Same tenant-scoped service and `TenantAdminDep` authorisation as the other knowledge routes; responses contain only the workspace's own documents. |

### 11.12 Recommended sequence

| Step | Recommendations | Why in this order |
|---|---|---|
| 1. Measure and guard | [IMP-01](#imp-01), [IMP-02](#imp-02), [IMP-23](#imp-23), [IMP-13](#imp-13) | Baselines and a safety gate before anything changes; IMP-13 is small and stops empty turns now. |
| 2. Fix what is in the index | [IMP-03](#imp-03), [IMP-04](#imp-04), [IMP-05](#imp-05), then one re-index | Every later gain depends on clean, contextual chunks. |
| 3. Stop serving stale facts | [IMP-06](#imp-06) | Removes contradictions and duplicates before ranking work. |
| 4. Find the right passages | [IMP-08](#imp-08), [IMP-09](#imp-09), [IMP-14](#imp-14) | Largest recall gains, plus a guard on the costliest error. |
| 5. Precision and completeness | [IMP-10](#imp-10), [IMP-15](#imp-15), [IMP-16](#imp-16) | Need the calibration data from step 1. |
| 6. Speed and cost | [IMP-17](#imp-17), [IMP-18](#imp-18), [IMP-22](#imp-22) | Safe to inject context once confidence tiers exist. |
| 7. Later | [IMP-07](#imp-07), [IMP-11](#imp-11), [IMP-12](#imp-12), [IMP-19](#imp-19), [IMP-20](#imp-20), [IMP-21](#imp-21), [IMP-24](#imp-24), [IMP-25](#imp-25), [IMP-26](#imp-26) | Valuable, but either narrower in reach or dependent on the steps above. |

Each step ends with an eval run (IMP-01) and a cost/latency check (IMP-02); a per-workspace flag is widened only when the numbers improve and the isolation gate (IMP-23) passes.

---

## Appendix A — Reproductions and their output

All scripts were run from a scratch directory outside the repository, importing the committed modules. `git status` was clean before and after.

### A.1 Chunker

Junk chunk, orphaned heading, Markdown ignored, mid-word overlap.

<details>
<summary>Show output</summary>

```text
=== 1. a stray 'Page 3' between two long paragraphs: 5 chunk(s)
  [1] len= 536 | 'cepted within fourteen days of delivery. Refunds are …'
  [2] len= 158 | 'cepted within fourteen days of delivery. … of delivery.\n\nPage 3'
  [3] len= 997 | 'Page 3\n\nRefunds are accepted within fourteen days of …'
  -> chunk holding only overlap + 'Page 3': [2] (MIN_CHUNK_CHARACTERS=40, the piece itself is 6 chars)

=== 2. heading followed by a paragraph longer than 1000 chars: 3 chunk(s)
  [0] len=  77 | '## Shipping\n\nWe ship to all governorates within three days.\n\n## Refund policy'
  [1] len=1062 | '## Shipping\n\nWe ship … days.\n\n## Refund policy\n\nCustomers may return any …'
  [2] len= 314 | 'return any unused item. The item must be in its original packaging. …'

=== 5. markdown: headings are plain text to the chunker: 1 chunk(s)
  [0] len=  87 | '# Pricing\n\n## Economy\n4500 EGP per square metre.\n\n## Premium\n7200 EGP per square metre.'

=== 6. overlap tail cut by characters
  -> chunk[1] begins with: 'o foxtrot golf. alpha bravo charlie delt'
```
</details>

### A.2 Hard split and overlap glue

Numbers and Arabic words cut in half, then glued with a blank line.

<details>
<summary>Show output</summary>

```text
input: 2251 chars, 40 lines, no '.!?' -> 3 hard pieces
cut 0: ...'ce 11,329 EGP - warranty 3 y' || 'ears\nSofa model 18 - price 1'...
cut 1: ...'ookshelf model 35 - price 13' || ',795 EGP - warranty 3 years\n'...

inside the final chunk 2:
' 2 years\nBookshelf model 35 - price 13\n\n,795 EGP - warranty 3 years\nSo'

arabic input: 1561 chars -> 2 pieces
cut 0: ...'مين ودعم فني على مدا' || 'ر الساعة الباقة رقم '...
inside the final chunk 1:
'مستخدمين ودعم فني على مدا\n\nر الساعة الباقة رق'
```
</details>

### A.3 Arabic PDF extraction

Presentation forms and bidi order, through the project's own bounded extractor.

<details>
<summary>Show output</summary>

Source HTML (rendered to PDF by the preinstalled Chromium, `DejaVu Sans`, `dir="rtl"`):

```text
سياسة الاسترجاع
يمكن للعميل استرجاع المنتج خلال أربعة عشر يوما من تاريخ الاستلام بشرط أن يكون المنتج في عبوته الأصلية.
سعر التشطيب الاقتصادي 4500 جنيه للمتر المربع.
```

Output of the project's `extract_pdf_bounded`:

```text
raw extract: 'ﺳﻴﺎﺳﺔ اﻻﺳﺘﺮﺟﺎع\nﻳﻤﻜﻦ ﻟﻠﻌﻤﻴﻞ اﺳﺘﺮﺟﺎع اﻟﻤﻨﺘﺞ ﺧﻼل … اﻷﺻﻠﻴﺔ.\n ﺟﻨﻴﻪ ﻟﻠﻤﺘﺮ اﻟﻤﺮﺑﻊ.4500ﺳﻌﺮ اﻟﺘﺸﻄﻴﺐ اﻻﻗﺘﺼﺎدي'
presentation-form chars: 104
contains expected logical phrase: False
contains it after NFKC: True
shared words before NFKC: 0 of 8
shared words after  NFKC: 8 of 8
code points of 'يمكن': ['0x64a', '0x645', '0x643', '0x646']  extracted: ['0xfef3', '0xfee4', '0xfedc', '0xfee6']
```

An English table PDF from the same renderer extracted cleanly (`Starter 1,500 EGP 2` …), so the problem is how the Arabic text layer comes out, not the PDF plumbing. Other PDF producers may behave differently — check real customer files with the SQL in [Appendix C](#appendix-c--operator-sql-for-triage).
</details>

### A.4 pgvector version

What happens below pgvector 0.8.

<details>
<summary>Show output</summary>

```text
$ psql … (PostgreSQL 16.15, pgvector 0.6.0 from Ubuntu 24.04)
 extversion: 0.6.0
SELECT set_config('hnsw.iterative_scan', 'strict_order', true), …;
ERROR:  invalid configuration parameter name "hnsw.iterative_scan"
DETAIL:  "hnsw" is a reserved prefix.

pytest tests/integration/test_knowledge_rag.py tests/integration/test_rag_integrity.py
12 failed, 26 passed — all 12 are searches, each: InvalidNameError … "hnsw.iterative_scan"
```
</details>

### A.5 Update / duplication demo on real PostgreSQL

<details>
<summary>Show output</summary>

Real `KnowledgeService.submit`, `reindex`, the indexing pipeline and `DocumentChunkRepository.search` (planner GUCs skipped because of pgvector 0.6.0), with the test suite's fake embedding model:

```text
1) Upload v1, index it
   created=True doc=e7af83b0
2) Upload the UPDATED price list (same title, new price), index it
   created=True doc=cf2ebd8f  (a second document, not a new version)
   documents now: [('Price list', 'ready'), ('Price list', 'ready')]; active generations: 2
   search('premium finishing costs per square metre') ->
      ('Price list', '7900 EGP', 0.428)
      ('Price list', '7200 EGP', 0.428)
3) Re-index v1 (the only 're-index' the API offers): replaces its own chunks
   v1 chunks before=1 after=1 (no duplication) - but still the OLD 7200 text
4) Same text in a second knowledge base
   created=True; search now returns:
      ('Price list', '7900 EGP', 0.428)
      ('Price list', '7900 EGP', 0.428)
      ('Price list', '7200 EGP', 0.428)
5) Resubmit identical v2 text with a corrected title
   created=False; stored title is still 'Price list'
6) The real RetrievalService on pgvector 0.6.0 (no accommodation)
   KnowledgeSearchUnavailableError: The knowledge base could not be searched just now. …
```
</details>

### A.6 Context budget

Passages that actually reach the model, by requested `top_k` and chunk size.

<details>
<summary>Show output</summary>

```text
top_k= 4 chunk=1000 chars: EN kept=4, AR kept=4, multi-line kept=4
top_k= 4 chunk=1152 chars: EN kept=4, AR kept=4, multi-line kept=4
top_k=10 chunk=1000 chars: EN kept=5, AR kept=5, multi-line kept=5
top_k=10 chunk=1152 chars: EN kept=4, AR kept=4, multi-line kept=4
```
</details>

### A.7 Byte decoding

Customer-sent text files (`extract_text`), not knowledge uploads.

<details>
<summary>Show output</summary>

```text
cp1252 English, smart quote        len= 36 -> '畏\u2072敲畦摮瀠汯捩鉹\u2073楳灭敬›㐱搠祡\u2e73'
cp1252 French                      len= 54 -> '楌牶楡潳\u206e牧瑡極整\ue020瀠牡楴\u2072敤㔠〰…'
cp1256 Arabic short                len= 22 -> 'سعر التشطيب 4500 جنيه'
cp1256 Arabic sentence             len= 54 -> 'مرحبا بكم في متجرنا، الشحن مجاني للطلبات فوق 500'
```
</details>

---

## Appendix B — Every tunable, in one table

| Constant | Value | Location |
|---|---|---|
| `MAX_SUBMITTED_CHARACTERS` | 400,000 | `knowledge_limits.py:38` |
| `MAX_PDF_BYTES` | 300,000 bytes (decoded) | `knowledge_limits.py:43` |
| `MAX_PDF_PAGES` | 40 (refused above) | `knowledge_limits.py:48` |
| `MAX_EXTRACTED_CHARACTERS` | 400,000 | `knowledge_limits.py:52` |
| `EXTRACTION_TIMEOUT_SECONDS` | 20 | `knowledge_limits.py:60` |
| `MAX_CONCURRENT_EXTRACTIONS` | 2 per process | `knowledge_limits.py:65` |
| `EXTRACTION_MEMORY_BYTES` | 768 MiB | `knowledge_limits.py:70` |
| `MAX_CHUNKS_PER_DOCUMENT` | 1,000 | `knowledge_limits.py:76` |
| `MAX_EMBEDDING_CHARACTERS_PER_DOCUMENT` | 600,000 | `knowledge_limits.py:80` |
| `MAX_CHUNK_CHARACTERS` / overlap / minimum | 1,000 / 150 / 40 | `chunking.py:29-34` |
| Embedding model / width | `text-embedding-3-small` / 1,536 | `config.py:482`, `db/models/knowledge.py:59` |
| Embedding batch / timeout / attempts / max Retry-After | 96 / 60 s / 3 / 30 s | `embeddings.py:80-93` |
| `EMBEDDING_SCHEMA_VERSION` | 1 | `embedding_space.py:28` |
| `CLAIM_LEASE` | 10 min | `document_indexing.py:119` |
| `MAX_INDEXING_ATTEMPTS` | 5 | `document_indexing.py:126` |
| Retry backoff | 30 s doubling, cap 15 min, equal jitter | `document_indexing.py:127-128` |
| Recovery poll / grace / batch | 60 s / 300 s / 100 | `ingestion_recovery.py:57-67` |
| `DEFAULT_TOP_K` / `MAX_TOP_K` | 4 / 10 | `retrieval_service.py:54-55` |
| `MAX_DISTANCE` | 0.75 cosine distance (similarity ≥ 0.25) | `retrieval_service.py:61` |
| `MAX_CONTEXT_CHARACTERS` | 6,000 (serialized JSON) | `retrieval_service.py:66` |
| `MAX_QUERY_CHARACTERS` | 2,000 | `retrieval_service.py:69` |
| `MAX_ROUNDS` / tool calls per response / per turn | 3 / 8 / 12 | `orchestrator.py:69-90` |
| HNSW | pgvector defaults (`m=16`, `ef_construction=64`), `vector_cosine_ops` | migration `0039`, `db/models/knowledge.py:390-395` |
| Planner settings per search | `hnsw.iterative_scan=strict_order`, `plan_cache_mode=force_custom_plan` | `knowledge_repository.py:487-492` |
| DB `lock_timeout` / pool | 5,000 ms / 5 + 10 overflow | `config.py:455-471` |

**API surface** (`/api/v1/knowledge`, `app/api/v1/knowledge.py`):

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/bases` | member | ≤ 100, no cursor |
| POST | `/bases` | admin | unique name per workspace |
| GET | `/bases/{id}` | member | |
| GET | `/bases/{id}/documents` | member | newest first, ≤ 100, **no cursor** |
| POST | `/bases/{id}/documents` | admin + plan slot | `202`; dedupe by text hash within the KB |
| GET | `/documents/{id}` | member | status, serving generation, latest attempt, `needs_reindex` |
| POST | `/documents/{id}/ingest` | admin | re-index the **stored** text, coalesced |
| DELETE | `/documents/{id}` | admin | hard delete |
| — | *update content / title* | — | **does not exist** |
| — | *search preview* | — | **does not exist** (schemas only) |
| — | *rename / delete knowledge base* | — | **does not exist** |

---

## Appendix C — Operator SQL for triage

Read-only. Validated against the schema built from the models; on the A.5 demo data they found the three `price list` copies, the cross-KB duplicate, and the workspace with ready documents but no agent able to search them.

```sql
-- pgvector must be >= 0.8.0 (RV-03)
SELECT extversion FROM pg_extension WHERE extname = 'vector';

-- Likely duplicate versions: same title served more than once in a workspace (RV-02)
SELECT tenant_id, lower(title) AS title, count(*) AS copies,
       array_agg(id ORDER BY created_at) AS document_ids
FROM documents
WHERE status = 'ready'
GROUP BY tenant_id, lower(title)
HAVING count(*) > 1;

-- Identical text in more than one knowledge base of a workspace (RV-07)
SELECT tenant_id, content_hash, count(*) AS copies,
       array_agg(knowledge_base_id) AS knowledge_bases
FROM documents
GROUP BY tenant_id, content_hash
HAVING count(*) > 1;

-- PDF documents stored with Arabic presentation forms (RV-01)
SELECT tenant_id, id, title, created_at
FROM documents
WHERE source = 'pdf' AND content ~ '[\uFB50-\uFDFF\uFE70-\uFEFF]';

-- Workspaces with ready documents but no active agent able to search them (RC-7)
SELECT d.tenant_id, count(*) AS ready_documents
FROM documents d
WHERE d.status = 'ready'
  AND NOT EXISTS (
        SELECT 1
        FROM agent_tools t
        JOIN agents a ON a.id = t.agent_id
        WHERE t.tenant_id = d.tenant_id
          AND t.name = 'search_knowledge'
          AND t.enabled
          AND a.status = 'active')
GROUP BY d.tenant_id;
```

---

## Appendix D — Glossary

| Term | Meaning here |
|---|---|
| **Generation** | One indexing attempt of one document (`document_index_generations`). At most one per document may be `active` (served). |
| **Coalescing** | A second re-index request while one is pending/processing reuses it instead of creating another. |
| **Fencing token** | `claim_token`; a worker may publish only if the token it wrote is still there. |
| **Embedding space** | `(provider, model, dimensions, schema_version)`; vectors are compared only within one. |
| **Presentation forms** | Unicode blocks U+FB50–U+FDFF and U+FE70–U+FEFF holding pre-shaped Arabic glyphs; NFKC maps them back to ordinary letters. |
| **NFKC** | Unicode compatibility normalisation (`unicodedata.normalize("NFKC", s)`). |
| **Iterative scan** | pgvector ≥ 0.8 option that keeps scanning the HNSW index until enough rows pass the filters. |
| **RRF** | Reciprocal Rank Fusion: merges ranked lists by summing `1 / (k + rank)`. |
| **MMR** | Maximal Marginal Relevance: picks results that are relevant *and* different from those already picked. |
