<div align="center">

# 🔎 Wasla RAG System — Full Code Review

**Ingestion · text extraction · chunking · embeddings · retrieval · re-indexing**

Reviewed commit `8e3a1ba` · branch `claude/sweet-faraday-209xcu` · 2026-10-29

</div>

> [!IMPORTANT]
> This review was written from the **source code, not from the docs**. Every finding says how it was verified.
> The serious findings were **reproduced on a real stack**: PostgreSQL 16 + pgvector, Redis 7, the real
> `IngestionWorker`, the real `Database.session()` commit/rollback, and the real pgvector search. Only the OpenAI HTTP
> endpoint was mocked. **No application code was changed** to produce this review.

| | |
|---|---|
| **Scope** | Everything that touches the knowledge base: API, schemas, services, repository SQL, models and migration, Redis queue, worker, OpenAI embeddings client, agent tool and orchestrator, entitlements, metering, and the RAG docs |
| **Method** | Read every line of the RAG path. Ran the 108 existing RAG tests on real PostgreSQL + pgvector (**all pass**). Wrote probe scripts that drive the real code ([Appendix A](#appendix-a--how-the-findings-were-reproduced)) |
| **Re-index verdict** | ❌ **Updated documents are duplicated, not replaced.** See [§4](#4-re-indexing-review-are-old-versions-replaced-or-duplicated) |
| **Findings** | 30 issues: 🔴 3 critical · 🟠 4 high · 🟡 11 medium · 🔵 10 low · ⚪ 2 info, plus 18 places where the docs and the code disagree |

---

## Contents

1. [Executive summary](#1-executive-summary)
2. [System map: what actually exists](#2-system-map-what-actually-exists)
3. [Ingestion pipeline, step by step](#3-ingestion-pipeline-step-by-step)
4. [Re-indexing review: are old versions replaced or duplicated?](#4-re-indexing-review-are-old-versions-replaced-or-duplicated)
5. [Retrieval pipeline, step by step](#5-retrieval-pipeline-step-by-step)
6. [Root causes of wrong or missing answers](#6-root-causes-of-wrong-or-missing-answers)
7. [Bug catalogue](#7-bug-catalogue)
8. [Docs vs code](#8-docs-vs-code)
9. [Why the test suite did not catch this](#9-why-the-test-suite-did-not-catch-this)
10. [Fix plan](#10-fix-plan)
11. [Appendices](#appendices)

**How to read the labels**

| Severity | Meaning |
|---|---|
| 🔴 **Critical** | Wrong or missing answers in normal operation. Silent, and the system does not recover by itself |
| 🟠 **High** | Knowledge is lost or leaked under common conditions |
| 🟡 **Medium** | Lowers retrieval quality, is wrong at the edges, or makes the system hard to operate |
| 🔵 **Low** | Edge cases, misreported values, small costs |
| ⚪ **Info** | Hygiene or a future risk |

| Verification | Meaning |
|---|---|
| ✅ **Reproduced** | Shown by running the real code. The probe output is quoted |
| 📖 **Code-verified** | Follows directly from the code. Nothing to run |
| ⚠️ **Unverified risk** | Likely from the code, but depends on outside behaviour (for example the real embedding model) that this review did not exercise |

---

## 1. Executive summary

> [!CAUTION]
> **Re-indexing does not do what the requirement says.** Uploading a changed version of a document creates a
> **second document**. Both versions stay `ready`, both are retrieved, they tie on distance, and in the probe the
> **old price ranked first**. Separately, re-indexing a document takes it **offline**. If the rebuild fails, or the
> worker reads the job before the request commits, it **stays offline with no error and no retry**.

**The five root causes behind most wrong or missing answers:**

1. **A document's only identity is the hash of its text** (RAG-01). Every edit is a new document. There is no update endpoint, so stale versions pile up and compete with current ones.
2. **Failures are erased by a rollback** (RAG-02). The worker records `failed` inside the same transaction it then rolls back. Every provider error, DB error or missing API key leaves the document `pending` with no reason and no automatic retry. It looks like it is still processing, so nobody knows to re-ingest it by hand.
3. **Search visibility depends on `status = 'ready'`** (RAG-03). Re-indexing flips a document to `pending`, so its valid chunks disappear from search until the rebuild commits. With cause 2, it stays hidden until someone re-ingests it by hand.
4. **The Redis job is pushed before the request commits** (RAG-04). This FastAPI version commits *after the response is sent* (verified), so an idle worker can read the document first. New uploads get stranded and re-indexed documents vanish.
5. **Retrieval is one dense top-4 lookup over every knowledge base** (RAG-06, RAG-09…RAG-14). There is no per-agent scoping, no keyword channel, no re-ranking and no de-duplication. Chunks carry no title or heading context, the chunker makes tiny and near-duplicate chunks, and the distance threshold was never calibrated.

**What is solid and should be kept:** tenant isolation (the filter is enforced on the scanned row and tested against real pgvector), the explicit "nothing found, do not guess" tool output, embedding width checks and index-sorted vectors, atomic delete-and-insert of one document's chunks, hard deletes, a queue separate from the agent queue, and keeping customer text out of logs.

| 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low | ⚪ Info | **Total** |
|:-:|:-:|:-:|:-:|:-:|:-:|
| **3** | **4** | **11** | **10** | **2** | **30** |

---

## 2. System map: what actually exists

### 2.1 Components

| Layer | File | What it really does |
|---|---|---|
| API | `app/api/v1/knowledge.py` | 8 routes: list, create and get a KB; list and submit documents; get, re-ingest and delete a document. **No update, search, chunk or KB-delete route** |
| Schemas | `app/schemas/knowledge.py` | `content` ≤ 400,000 chars. `SearchRequest`, `SearchResponse` and `PassageRead` are defined and never used |
| Write service | `app/services/knowledge_service.py` | `submit()`: extract → normalise → hash → upsert → enqueue. `ingest()`: chunk → embed → replace chunks → `ready` |
| Extraction | `app/services/extraction.py` | pypdf text layer, first 40 pages. Byte decoding is shared with WhatsApp media |
| Chunking | `app/services/chunking.py` | Packs paragraphs up to 1,000 chars, 150-char overlap, 40-char minimum |
| Embeddings | `app/integrations/openai/embeddings.py` | `POST /v1/embeddings`, batches of 96, 3 attempts, width check |
| Queue | `app/workers/ingestion_queue.py` | Redis lists `knowledge:ingestion:{pending,inflight,failed}` |
| Worker | `app/workers/ingestion_worker.py` | One job at a time, one DB transaction per job, failures dead-lettered |
| Repository | `app/repositories/knowledge_repository.py` | All SQL, including the pgvector search |
| Models | `app/db/models/knowledge.py`, migration `0007` | 3 tables, `vector(1536)`, no ANN index |
| Read service | `app/services/retrieval_service.py` | Embed the query → search → distance threshold → context text |
| Agent tool | `app/agents/registry.py` | `search_knowledge(query, max_results?)` |
| Orchestrator | `app/agents/orchestrator.py` | Tool loop, max 3 rounds. Tools only if granted |
| Agent worker | `app/workers/ai_worker.py` | Builds the `EmbeddingsClient` for each turn |
| Plan limits | `app/services/entitlement_service.py` | `knowledge_documents` = count of **all** document rows |
| Metering | `app/services/usage_service.py` | One `rag_query` event per search |

### 2.2 Ingestion flow (as implemented, ⚠ marks the defects)

```mermaid
flowchart LR
    A["Admin client"] -- "POST /knowledge/bases/{kb}/documents" --> B["Route<br/>TenantAdmin + DocumentSlot"]
    B --> C["KnowledgeService.submit()<br/>⚠ extract() + pypdf run HERE, sync"]
    C --> D["normalise() then SHA-256(text)"]
    D --> E{"same hash in this KB?"}
    E -- yes --> F["return existing<br/>created=false"]
    E -- no --> G["INSERT documents<br/>status=pending"]
    G --> H["RPUSH knowledge:ingestion:pending<br/>⚠ before COMMIT"]
    H --> I["202 sent to client"]
    I --> J[("COMMIT<br/>after the response")]
    H -. "BLMOVE wakes worker" .-> K["IngestionWorker.run_once()"]
    K --> L["ingest(): status=processing<br/>chunking.split()"]
    L --> M["embed() in batches of 96"]
    M --> N["DELETE old chunks<br/>INSERT new chunks"]
    N --> O[("status=ready, COMMIT")]
    M -- "any error" --> P["mark_failed() + flush<br/>⚠ then ROLLBACK"]
    P --> Q[("job moved to :failed<br/>document stays pending")]
```

### 2.3 Retrieval flow (as implemented)

```mermaid
sequenceDiagram
    autonumber
    participant C as Customer (WhatsApp)
    participant AW as AgentWorker
    participant O as Orchestrator
    participant LLM as OpenAI Responses
    participant T as search_knowledge
    participant R as RetrievalService
    participant E as OpenAI Embeddings
    participant PG as PostgreSQL + pgvector
    C->>AW: message (queued by the webhook)
    AW->>O: answer(conversation)
    O->>LLM: system prompt, history, granted tool specs
    alt the model chooses to call the tool
        LLM-->>O: search_knowledge(query, max_results)
        O->>T: run(query), no knowledge base id
        T->>R: search(query, top_k=4)
        R->>E: embed_one(query)
        R->>PG: nearest k by cosine distance, tenant + ready filters
        PG-->>R: k rows with distance
        R-->>T: keep distance up to 0.75, pack up to 6000 chars
        T-->>O: passages text, or the nothing-found instruction
        O->>LLM: tool output (round 2 of max 3)
    end
    LLM-->>O: reply text
    O-->>AW: outcome
    AW->>C: send reply
```

### 2.4 Data model

```mermaid
erDiagram
    tenants ||--o{ knowledge_bases : owns
    knowledge_bases ||--o{ documents : contains
    documents ||--o{ document_chunks : "derived, ON DELETE CASCADE"
    knowledge_bases ||--o{ document_chunks : "denormalised kb id"
    knowledge_bases {
        uuid id PK
        uuid tenant_id FK
        varchar200 name "UNIQUE tenant_id + name"
        varchar500 description
    }
    documents {
        uuid id PK
        uuid tenant_id FK
        uuid knowledge_base_id FK
        varchar300 title
        enum source "text, markdown, pdf"
        enum status "pending, processing, ready, failed"
        char64 content_hash "UNIQUE tenant_id + kb_id + hash"
        text content "normalised extracted text"
        int chunk_count
        varchar500 error
        timestamptz ingested_at
    }
    document_chunks {
        uuid id PK
        uuid tenant_id FK
        uuid document_id FK
        uuid knowledge_base_id FK
        int ordinal "UNIQUE tenant_id + document_id + ordinal"
        text content
        int token_estimate "stored, never read"
        vector1536 embedding "nullable, no ANN index"
    }
```

### 2.5 Document lifecycle: documented vs actual

```mermaid
stateDiagram-v2
    direction LR
    [*] --> pending : submit
    pending --> processing : worker claims (uncommitted)
    processing --> ready : success
    processing --> failed : documented only
    processing --> pending : actual result of any error (rollback)
    ready --> pending : reingest (document hidden from search)
    failed --> pending : resubmit or reingest
```

> [!NOTE]
> Through the real worker, `processing` is never visible to another connection (probe H) and `failed` cannot be
> reached (probe A). In practice the API only ever shows `pending` and `ready`.

---

## 3. Ingestion pipeline, step by step

### 3.1 Submission (API)

- **Route:** `POST /api/v1/knowledge/bases/{knowledge_base_id}/documents` (`app/api/v1/knowledge.py:99-126`). Admins and owners only (`TenantAdminDep`).
- **Plan check:** `DocumentSlotDep` runs **before** de-duplication and counts every document row of the tenant: versions, failed and pending rows included (`entitlement_service.py:240-245`).
- **Body** (`schemas/knowledge.py:30-39`): `title` ≤ 300, `content` 1 to 400,000 chars, `source` = `text | markdown | pdf`, plus optional `filename` and `media_type`. Those two are stored only. The extractor is chosen from `source`.
- **Missing:** no replace or update endpoint, no multipart upload, no KB rename or delete, no search preview, no chunk listing.

### 3.2 Text extraction (runs inside the HTTP request)

| `source` | What the code does |
|---|---|
| `text` | `chunking.normalise(raw)` and nothing else |
| `markdown` | **Exactly the same as text.** Headings and lists get no special handling |
| `pdf` | `base64.b64decode(raw, validate=True)` → `extraction.extract_pdf()`: pypdf `extract_text()` on the **first 40 pages**, pages joined by blank lines. Empty output means "scan, convert to text first" (422). Then `normalise()` |

- `normalise()` (`chunking.py:57-68`) turns CRLF into LF, collapses spaces and tabs, strips each line, and squeezes 3+ newlines to 2. It does **no Unicode normalisation**.
- No OCR. No DOCX, HTML or CSV.
- `extraction.extract_text()` (byte decoding: utf-8 → utf-16 → cp1256 → latin-1) is **not used by the knowledge base**, because KB text arrives as a JSON string. Only WhatsApp media uses it.

### 3.3 Identity and de-duplication

- `content_hash = sha256(normalised_text)` (`knowledge_service.py:59-66`).
- `DocumentRepository.upsert()` (`knowledge_repository.py:123-165`) looks up `(tenant, kb, content_hash)`. A hit returns the existing row and resets `failed` to `pending`. A miss inserts a new row. The unique constraint `uq_documents_tenant_id_knowledge_base_id_content_hash` backs this (`models/knowledge.py:109-114`).
- **So the content hash is the document's only identity.** Title and filename play no part. This is the root cause of RAG-01.

### 3.4 Queue

- Redis lists under `knowledge:ingestion:` (`ingestion_queue.py:93-123`): `pending` → `BLMOVE` → `inflight` → `LREM` on success, or `RPUSH :failed` on error. The payload is `{"document_id","tenant_id"}`.
- The push happens inside `submit()` and `reingest()`, **before the request's transaction commits** (`knowledge_service.py:203-205, 227-230`).
- Nothing reads `inflight` or `failed`, and nothing calls `list_pending()`.

### 3.5 Worker

- `IngestionWorker.run_once()` (`ingestion_worker.py:82-112`) reserves one job (5 s block) and opens **one** `Database.session()`, which commits on success and **rolls back on any exception** (`db/session.py:69-80`). Inside that block it builds the `EmbeddingsClient` (which raises when no API key is set) and calls `KnowledgeService.ingest()` (`ingestion_worker.py:114-127`).
- Any exception sends the job to `:failed`. There is no retry and no backoff.
- There is one ingestion loop per worker process, sharing the event loop with the other loops (`app/workers/runner.py`).

### 3.6 Chunking (`app/services/chunking.py`)

Constants: `MAX_CHUNK_CHARACTERS = 1000` (`:29`), `CHUNK_OVERLAP_CHARACTERS = 150` (`:31`), `MIN_CHUNK_CHARACTERS = 40` (`:34`).

```mermaid
flowchart TD
    A["normalise(text)"] --> B["split on blank lines into paragraphs"]
    B --> C{"paragraph over 1000 chars?"}
    C -- no --> D["add to buffer while buffer stays within 1000"]
    C -- yes --> E["flush buffer, even if it is a lone heading"]
    E --> F["split on sentence ends . ! ? ۔ ؟ then whitespace"]
    F --> G{"sentence over 1000?"}
    G -- yes --> H["hard cut every 1000 chars"]
    G -- no --> I["pack sentences within 1000"]
    D --> J["pieces"]
    H --> J
    I --> J
    J --> K["prefix each piece with the last 150 chars of the previous piece"]
    K --> L{"40 chars or more, or first chunk?"}
    L -- yes --> M["Chunk(ordinal, content)"]
    L -- no --> N["drop"]
```

`token_estimate` is ASCII chars / 4 plus other chars / 2. It is stored on each chunk and never read.

### 3.7 Embeddings (`app/integrations/openai/embeddings.py`)

- `POST https://api.openai.com/v1/embeddings` with `model = OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`) and `dimensions = 1536`, always sent (`:110-119`).
- Batches of 96 inputs, sent one after another (`:42, :97-101`).
- **Retries:** 429, 5xx and transport errors get up to 3 attempts, sleeping 1 s then 2 s (linear, no jitter, `Retry-After` ignored). Other 4xx fail at once (`:121-161`).
- **Response:** vectors are sorted by `index` and their width must be 1536 (`:198-233`). The `usage` field is ignored.
- **Timeout:** 60 s, hard-coded. The `OPENAI_TIMEOUT_SECONDS` setting is never read (`config.py:85`).

### 3.8 Storing chunks

`KnowledgeService._rebuild()` (`knowledge_service.py:308-334`) embeds **all** chunks first, then deletes the document's old chunks and inserts the new ones, all inside the worker's single transaction. ✅ This part is correct: re-running ingestion never doubles **one document's** chunks. `mark_ready()` then sets the status, `chunk_count` and `ingested_at`.

### 3.9 Re-ingest and delete

- `POST /documents/{id}/ingest` → `reingest()` sets `status = pending`, clears `error` and enqueues (`knowledge_service.py:218-231`). It re-chunks the **stored** content. New content cannot be supplied.
- `DELETE /documents/{id}` hard-deletes the chunks and the document (FK cascade) (`:336-351`). ✅ Correct.

---

## 4. Re-indexing review: are old versions replaced or duplicated?

> [!CAUTION]
> **Verdict: duplicated.** The system has no concept of a document *version*. "Re-index" today only means
> "re-chunk the same stored text", and even that takes the document offline while it runs.

### 4.1 Scenario matrix

| # | Scenario | Expected | Actual | Evidence |
|---|---|---|---|---|
| R1 | Upload the **same** text again | one document | ✅ one document (`created: false`). ⚠️ A new title or filename is silently dropped | probe E |
| R2 | Upload a **changed** version (one price edited) | v2 replaces v1 | ❌ **two documents, both `ready`, both retrieved**, tied on distance, old price ranked first, 2 plan slots used | probe B |
| R3 | Re-ingest a `ready` document, success | rebuilt, no downtime | ⚠️ rebuilt without duplication, but **hidden from search** from the moment the request commits until the worker finishes | probe C |
| R4 | Re-ingest, embedding provider fails | old version keeps serving, error shown | ❌ **stuck `pending`**, error lost, valid chunks hidden | probe C |
| R5 | Re-ingest, worker reads the job before the request commits | rebuilt | ❌ worker sees `ready`, does nothing, then `pending` commits. **Hidden, with no job left to fix it** | probe D2 |
| R6 | Duplicate jobs for one document | harmless | ✅ the second job sees `ready` and does nothing. If both start before either commits, the document row lock makes them run one after the other, so the document is rebuilt twice: wasted embedding calls, no duplicates | probe (two workers on one document) |
| R7 | Delete, then upload the new version | only v2 | ✅ works by hand, but there is a knowledge gap until v2 is `ready` | code |
| R8 | Embedding model or chunker changes | re-embed everything | ❌ no bulk re-index. Per-document re-ingest has the R3–R5 problems. Vectors from two models can mix silently | code |

### 4.2 The proof (probe B: one price changed, uploaded under the same title)

```text
v1 created=True {'status': 'ready', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Finishing price list'}
v2 created=True {'status': 'ready', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Finishing price list'}
search: [('Finishing price list', 0.419, 'mentions 7200'), ('Finishing price list', 0.419, 'mentions 8500')]
plan slots used by one logical document: 2
```

The two versions embed almost identically, so they tie and take the same top-k slots. The order between them is arbitrary. Here the **old** price came first.

### 4.3 Probe C: re-index availability

```text
ready        : {'status': 'ready', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Prices'} search -> [('Prices', 0.419, 'mentions 7200')]
reingest req : {'status': 'pending', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Prices'} search -> []
worker failed: {'status': 'pending', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Prices'} search -> [] {'pending': 0, 'inflight': 0, 'failed': 1}
```

The old chunks are still in the table (`chunk_rows: 1`) and still valid, but the document answers nothing.

### 4.4 Why it behaves this way

1. **Identity is the content hash** (`uq_documents_tenant_id_knowledge_base_id_content_hash`), so a new version is a new document.
2. **There is no update API.** Nothing can say "this upload replaces document X".
3. **Visibility depends on `status`, not on which chunk set is live.** Any re-index makes the document disappear.
4. **Failures are recorded inside the transaction that is then rolled back** (RAG-02).
5. **The job is enqueued before the commit** (RAG-04).

The target design (stable identity, chunk generations, atomic swap) is in [Stage 1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement).

---

## 5. Retrieval pipeline, step by step

### 5.1 How retrieval is triggered

- **Only** through the `search_knowledge` tool (`registry.py:225-291`). Agents are created with **no tool grants** (`agent_service.py:73-106`), so an admin must grant it. Even then, the model decides whether to call it. Nothing forces retrieval and there is no `tool_choice` (`orchestrator.py:189-213`).
- Tool arguments: `query` (required string) and `max_results` (optional integer). There is no knowledge-base argument, and the handler never passes one (`registry.py:242-253`).

### 5.2 The query

`embed_one(query.strip())` with the same model. One `rag_query` usage event is recorded per search (`retrieval_service.py:124-129`). The query is the model's own wording. There is no rewriting and no conversation context.

### 5.3 The SQL (`knowledge_repository.py:254-296`, written out as SQL)

```sql
SELECT c.*, (c.embedding <=> :q) AS distance, d.title
FROM   document_chunks c
JOIN   documents d ON d.id = c.document_id
WHERE  c.tenant_id = :tenant
  AND  d.tenant_id = :tenant
  AND  d.status = 'ready'
  AND  c.embedding IS NOT NULL
  -- AND c.knowledge_base_id = :kb   -- supported, never used by the agent
ORDER  BY c.embedding <=> :q
LIMIT  :k;
```

This is an exact (brute-force) cosine search. There is no ANN index (migration `0007:11-16`). `c.*` also pulls the 1536-float embedding of every result row.

### 5.4 Top-k, filters and threshold

| Knob | Value | Where |
|---|---|---|
| Default top-k | 4 | `retrieval_service.py:34` |
| Max top-k | 10 (clamped to 1..10) | `:35`, `:124` |
| Distance threshold | cosine distance ≤ **0.75** (similarity ≥ 0.25) | `:39`, `:135` |
| Context budget | **6,000 characters**, so at most about 5 passages | `:42`, `:84-92` |
| Tenant filter | mandatory, on both chunk and document | repo `:281-282` |
| Status filter | the document must be `ready` | repo `:283` |
| KB filter | supported, unused | repo `:289-290` · `registry.py:253` |

### 5.5 Re-ranking, hybrid search, de-duplication

**None.** The k nearest rows by cosine distance are the final answer. Ties have no defined order. Near-duplicates (overlapping chunks, versions, copies in other KBs) are not collapsed. There is no keyword, BM25 or trigram channel, no cross-encoder or LLM re-ranker, and no MMR.

### 5.6 Context assembly (`Retrieval.as_context`, `retrieval_service.py:70-92`)

- Format: `[i] From “<document title>”:\n<chunk text>`, joined by blank lines. It **stops at the first block that would exceed** the remaining budget.
- Empty result: a fixed instruction, "No information about this was found … offer to pass the question to a colleague." ✅ Good practice.
- Distances, document ids and ordinals are not passed to the model.

### 5.7 Tenant isolation

✅ **Sound.** Every read is tenant-filtered on the scanned row, the KB filter cannot escape the tenant, and both are tested against real pgvector (`tests/integration/test_knowledge_rag.py:171-231`).

---

## 6. Root causes of wrong or missing answers

| What the business sees | Root cause | Bugs |
|---|---|---|
| The agent quotes an **old price or outdated policy** | Each edit adds a version, every version is retrievable, and the versions tie | RAG-01, RAG-09 |
| The agent says **"I don't have that information"** about something that was uploaded | The document never became `ready` (failure rolled back, commit race, no sweeper), a re-index hid it, it sat past page 40, the PDF was too large, or the agent was never granted the tool | RAG-02, 03, 04, 05, 07, 15 |
| The agent mentions **internal or irrelevant material** | Every KB of the workspace is searched. There is no per-agent scoping | RAG-06, RAG-09 |
| Answers are **vague or miss specifics** (prices, SKUs, model numbers) | Dense-only search with no keyword channel or re-ranker, chunks without title or heading context, heading-only and near-duplicate chunks, numbers cut by hard splits | RAG-09, 12, 13 |
| **Arabic works worse** than English | No Unicode or Arabic normalisation, an uncalibrated threshold, and Arabic PDF extraction never validated | RAG-10, RAG-14 |
| **Fewer passages than requested** | The 6,000-char budget silently caps the result | RAG-11 |
| Answers get **worse after a config change** | Chunks do not record which embedding model produced them | RAG-16 |
| **Nobody can tell why** an answer was wrong | No search preview, no chunk inspection | RAG-17 |

---

## 7. Bug catalogue

### 7.1 Summary

| ID | Sev | Area | Title | Verified |
|---|---|---|---|---|
| [RAG-01](#rag-01) | 🔴 | Re-index | Updated documents are duplicated, never replaced | ✅ |
| [RAG-02](#rag-02) | 🔴 | Worker | Failed ingestion is never recorded; the document is stuck in `pending` | ✅ |
| [RAG-03](#rag-03) | 🔴 | Re-index | Re-indexing takes a document offline, permanently if the rebuild fails | ✅ |
| [RAG-04](#rag-04) | 🟠 | Queue | Job enqueued before commit: uploads stranded, re-indexed documents vanish | ✅ |
| [RAG-05](#rag-05) | 🟠 | Ops | Nothing recovers stuck work (no sweeper, reaper, retry or dead-letter tooling) | 📖 |
| [RAG-06](#rag-06) | 🟠 | Retrieval | Agents search every knowledge base; per-agent scoping does not exist | ✅ |
| [RAG-07](#rag-07) | 🟠 | Extraction | PDF knowledge silently incomplete (40 pages) or refused (about 293 KiB) | ✅ |
| [RAG-08](#rag-08) | 🟡 | Extraction | PDF parsing blocks the API event loop | ✅ |
| [RAG-09](#rag-09) | 🟡 | Retrieval | Dense-only top-k: no re-ranking, hybrid search or de-duplication | ✅ |
| [RAG-10](#rag-10) | 🟡 | Retrieval | Distance threshold 0.75 is global and uncalibrated | ⚠️ |
| [RAG-11](#rag-11) | 🟡 | Retrieval | `max_results` silently capped by the 6,000-char budget | ✅ |
| [RAG-12](#rag-12) | 🟡 | Chunking | Tiny, near-duplicate, oversized and mid-word chunks; numbers split | ✅ |
| [RAG-13](#rag-13) | 🟡 | Chunking | No title or heading context in embeddings; Markdown structure ignored | ✅ |
| [RAG-14](#rag-14) | 🟡 | Extraction | No Unicode or Arabic normalisation | ✅ / ⚠️ |
| [RAG-15](#rag-15) | 🟡 | Agent | Retrieval is optional and left to the model | 📖 |
| [RAG-16](#rag-16) | 🟡 | Embeddings | No record of which model produced a vector; model changes corrupt silently | 📖 |
| [RAG-17](#rag-17) | 🟡 | API | No way to preview retrieval or inspect chunks | 📖 |
| [RAG-18](#rag-18) | 🟡 | API | Document list capped at 100 with no pagination | 📖 |
| [RAG-19](#rag-19) | 🔵 | Extraction | Strict base64 rejects line breaks; `byte_size` is the base64 length | ✅ |
| [RAG-20](#rag-20) | 🔵 | Ingestion | Resubmitting identical text drops the new title and metadata | ✅ |
| [RAG-21](#rag-21) | 🔵 | Ingestion | Two concurrent identical uploads give an HTTP 500 | ✅ |
| [RAG-22](#rag-22) | 🔵 | Worker | `processing` is never visible; a row lock is held during embedding | ✅ |
| [RAG-23](#rag-23) | 🔵 | Metering | Embedding tokens are not metered | 📖 |
| [RAG-24](#rag-24) | 🔵 | Billing | Plan slots count versions and failures, and block resubmission at the limit | ✅ |
| [RAG-25](#rag-25) | 🔵 | Extraction | UTF-16 is tried before cp1256 (WhatsApp text files) | ✅ |
| [RAG-26](#rag-26) | 🔵 | Embeddings | Fragile retry policy; `OPENAI_TIMEOUT_SECONDS` is ignored | 📖 |
| [RAG-27](#rag-27) | 🔵 | Agent | Misleading tool output on an empty query or a provider failure | 📖 |
| [RAG-28](#rag-28) | 🔵 | Security | Retrieved text is not marked as data | 📖 |
| [RAG-29](#rag-29) | ⚪ | Perf | The search fetches every result's embedding vector | 📖 |
| [RAG-30](#rag-30) | ⚪ | Perf | A future ANN index needs filter-aware design; pgvector image unpinned | 📖 |

### 7.2 Critical

<a id="rag-01"></a>
#### RAG-01 · 🔴 Updated documents are duplicated, never replaced

**Verified:** ✅ probe B · **Where:** `app/repositories/knowledge_repository.py:142-165` · `app/services/knowledge_service.py:193-205` · `app/db/models/knowledge.py:109-114` · no update route in `app/api/v1/knowledge.py`

**What happens.** The SHA-256 of the normalised text is the document's only identity. Change one character and the hash changes, `upsert()` misses, and a new `documents` row is ingested next to the old one. Nothing links the two rows and nothing deletes the old one. Both are `ready` and both are searched.

**Impact.** The agent sees contradicting passages for the same question. Because the versions embed almost identically, they tie for the same top-k slots and the order is arbitrary; in the probe the old price ranked first. Every edit also uses another `knowledge_documents` plan slot, and the document list shows several entries with the same title and no hint which one is current.

**Fix.** [Stage 1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement). Until then, users should delete the old document before uploading the new one.

<a id="rag-02"></a>
#### RAG-02 · 🔴 Failed ingestion is never recorded; the document is stuck in `pending`

**Verified:** ✅ probes A and A2 · **Where:** `app/services/knowledge_service.py:281-294` · `app/workers/ingestion_worker.py:114-127` · `app/db/session.py:69-80`

**What happens.** `ingest()` writes `status = failed` and the error with a flush, then re-raises so the worker can dead-letter the job. The exception leaves the worker's `async with database.session()` block, which **rolls back**, so the status and error return to the last committed values: `pending`, no error.
- Exceptions that are not `WaslaError` (a database error, a bug) never reach `mark_failed()` at all.
- A missing `OPENAI_API_KEY` raises while the `EmbeddingsClient` is being built, before `ingest()` runs.

```text
A.  after submit : {'status': 'pending', 'error': None, 'chunk_count': 0, 'chunk_rows': 0, 'title': 'Prices'} {'pending': 1, 'inflight': 0, 'failed': 0}
    after worker : {'status': 'pending', 'error': None, 'chunk_count': 0, 'chunk_rows': 0, 'title': 'Prices'} {'pending': 0, 'inflight': 0, 'failed': 1}
A2. after worker : {'status': 'pending', 'error': None, 'chunk_count': 0, 'chunk_rows': 0, 'title': 'Prices'} {'pending': 0, 'inflight': 0, 'failed': 1}
```

A is a provider outage (HTTP 503 on every attempt). A2 is a worker with no `OPENAI_API_KEY`.

**Impact.** The documented recovery loop ("see `failed` and the reason, fix it, re-ingest") cannot happen. The admin sees `pending`, which looks like work in progress, so nothing prompts a manual re-ingest. Every provider hiccup strands a document until someone happens to re-upload or re-ingest it, because nothing retries automatically (RAG-05).

**Why the tests pass.** `tests/integration/test_knowledge_rag.py:295-316` calls `ingest()` directly on a test session that is never rolled back.

**Fix.** [0.1](#stage-0--hotfixes-stop-silent-knowledge-loss).

<a id="rag-03"></a>
#### RAG-03 · 🔴 Re-indexing takes a document offline, permanently if the rebuild fails

**Verified:** ✅ probe C · **Where:** `app/services/knowledge_service.py:218-231` · `app/repositories/knowledge_repository.py:283`

**What happens.** Whether a document is searchable depends only on `documents.status`. `reingest()` flips a `ready` document to `pending`. From that commit until the worker commits `ready` again, all of its chunks, which are still present and valid, are excluded from search. If the rebuild fails, RAG-02 leaves the document `pending` and excluded indefinitely.

**Impact.** The route's docstring promises "Safe to call on a document that is already ready … the worst case is some wasted embedding calls" (`api/v1/knowledge.py:152-154`). The real worst case is that the document silently stops answering. Re-indexing everything (after a chunker change, which is what the code comments say `reingest` is for) causes an outage across the workspace that lasts as long as the queue takes to drain. `test_knowledge_rag.py:318-340` asserts this outage as the intended behaviour.

**Fix.** [0.2](#stage-0--hotfixes-stop-silent-knowledge-loss) for now, [Stage 1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement) for good.

### 7.3 High

<a id="rag-04"></a>
#### RAG-04 · 🟠 Job enqueued before commit: uploads stranded, re-indexed documents vanish

**Verified:** ✅ probes D1, D2 and the FastAPI ordering probe · **Where:** `app/services/knowledge_service.py:203-205, 227-230` · `app/core/dependencies.py:34-38` · `app/workers/ingestion_worker.py:93-109`

**What happens.** The Redis push runs inside the request, before its transaction commits. In FastAPI 0.141 the request session is committed in the dependency teardown, which runs **after the response has been sent** (verified: handler returns, then response sent, then commit). A worker blocked on `BLMOVE` wakes immediately and reads the document over a different connection:

- **New upload:** the row is not visible yet → `TenantIsolationError` → job dead-lettered → the row then commits as `pending` with no job anywhere → **stranded**, with nothing left to process it.
- **Re-index of a `ready` document:** the worker reads the old committed `ready` → "already ready", does nothing → job released → the request then commits `pending` → **hidden, with no job and no error**.

```text
D1. after commit : {'status': 'pending', 'error': None, 'chunk_count': 0, 'chunk_rows': 0, 'title': 'Prices'} {'pending': 0, 'inflight': 0, 'failed': 1}
D2. after commit : {'status': 'pending', 'error': None, 'chunk_count': 1, 'chunk_rows': 1, 'title': 'Prices'} {'pending': 0, 'inflight': 0, 'failed': 0} search -> []
```

> [!WARNING]
> Moving the enqueue into a FastAPI `BackgroundTask` does **not** fix this. In this FastAPI version background tasks
> also run **before** the commit (verified).

**Likelihood.** It depends on timing and was not measured in production. But the window covers the whole response write, and the worker's path to its SELECT is short, so expect it to happen. ADR-015 noted the enqueue-before-commit ordering but only considered the rollback case.

**Fix.** [0.3](#stage-0--hotfixes-stop-silent-knowledge-loss) for now, [Stage 2](#stage-2--a-durable-ingestion-queue) for good.

<a id="rag-05"></a>
#### RAG-05 · 🟠 Nothing recovers stuck work

**Verified:** 📖 · **Where:** `app/repositories/knowledge_repository.py:114-121` (`list_pending` has no caller) · `app/workers/ingestion_queue.py:102-123` (nothing reads `inflight` or `failed`) · `app/workers/ingestion_worker.py:101-109` (no retry) · `TASKS.md:154, 201`

There is no sweeper for `pending` documents and no reaper for `inflight` jobs. A worker killed mid-job (an OOM kill, a lost node, or a job that outlasts the worker's 60 s `stop_grace_period` in `docker-compose.prod.yml`) leaves its job in `inflight` until an operator moves it by hand. There is no automatic retry with backoff, and no API or CLI to see or requeue dead letters. TASKS.md says documents are stranded only by "a queue outage"; in fact RAG-02 and RAG-04 strand them during normal operation.

**Fix.** [0.4](#stage-0--hotfixes-stop-silent-knowledge-loss), then [Stage 2](#stage-2--a-durable-ingestion-queue).

<a id="rag-06"></a>
#### RAG-06 · 🟠 Agents search every knowledge base; per-agent scoping does not exist

**Verified:** ✅ probe G · **Where:** `app/agents/registry.py:242-253` · `app/services/retrieval_service.py:111-118` · `app/db/models/agent.py:151` (`AgentTool.config` exists and is never read)

The repository supports one `knowledge_base_id` filter, but the only production caller never passes it, and nothing links an agent to a knowledge base. The KB model docstring (`models/knowledge.py:78-80`) and `docs/RAG.md:9` both say agents can be pointed at different sets.

```text
tool output the model receives:
   [1] From “Price list”:
   [2] From “Price list”:
   [3] From “Staff memo”:
```

The workspace had three KBs: "General" holding the price list, "Archive" holding a copy of the same file, and "Internal HR" holding a staff memo with commission rates marked "do not share with customers". One customer-facing search returned both copies of the price list and the HR memo.

**Impact.** Internal or obsolete material can be quoted to customers, copies kept in other KBs crowd the top-k, and a workspace that keeps draft or archive KBs degrades its own answers.

**Fix.** [4.1](#stage-4--retrieval-quality).

<a id="rag-07"></a>
#### RAG-07 · 🟠 PDF knowledge silently incomplete or refused

**Verified:** ✅ probes P1 and P2 · **Where:** `app/services/extraction.py:32, 58` · `app/schemas/knowledge.py:36` · `app/services/knowledge_service.py:47, 81-93`

- **Pages after 40 are dropped with no signal.** A 45-page PDF is read up to `PAGE-MARKER-040` and still marked `ready`. Nothing in the API says pages are missing.
- **Size cap of about 293 KiB.** PDFs travel as base64 inside a JSON field capped at 400,000 characters, while nginx allows 10 MiB and the app 32 MiB. Text-only PDFs fit; brochures with images usually do not.
- Scanned PDFs are refused because there is no OCR. That is by design, but together with the two limits above, PDF coverage is narrow.

**Impact.** The agent says "no information" about content that is in the uploaded catalogue.

**Fix.** [3.1–3.3](#stage-3--extraction-and-chunking-quality).

### 7.4 Medium

<a id="rag-08"></a>
#### RAG-08 · 🟡 PDF parsing blocks the API event loop

**Verified:** ✅ probe P3 · **Where:** `app/services/knowledge_service.py:189` → `extraction.py:56-59` · `app/core/limits.py:173`

pypdf is pure CPU work and runs inside an `async` request handler. A simple 40-page PDF took **about 400 ms of blocking CPU**; complex PDFs take longer. While it runs, every other request on that API process waits, including Meta webhooks, which the same app serves. The timeout middleware uses `asyncio.timeout`, which cannot interrupt code that never awaits, so it does not bound this. The docs say extraction happens in the background (`RAG.md:17-27`, `ARCHITECTURE.md:202`, `api/v1/knowledge.py:11-13`).

**Fix.** [3.1, 3.2](#stage-3--extraction-and-chunking-quality).

<a id="rag-09"></a>
#### RAG-09 · 🟡 Dense-only top-k: no re-ranking, hybrid search or de-duplication

**Verified:** ✅ probes B and G (duplicates take result slots) · **Where:** `app/repositories/knowledge_repository.py:276-288` · `app/services/retrieval_service.py:124-135`

The top-k by cosine distance is final. Exact names, SKUs, model numbers and prices are weak spots for embeddings, and there is no keyword channel to cover them. There is no re-ranker and no diversity step, and overlapping chunks, versions and copies in other KBs are not collapsed. Ties have no defined order.

**Fix.** [4.2–4.4](#stage-4--retrieval-quality).

<a id="rag-10"></a>
#### RAG-10 · 🟡 Distance threshold 0.75 is global and uncalibrated

**Verified:** ⚠️ code only. The real model's behaviour was **not** measured (this review made no OpenAI calls) · **Where:** `app/services/retrieval_service.py:36-39`

It is a fixed constant that cannot be set per workspace or per language. The comment justifying it ("unrelated text sits around 0.8 and above") is not backed by any test against the real model. The suite uses a bag-of-words fake (`tests/fake_embeddings.py`), so `test_a_question_about_nothing_stored_finds_nothing` validates the fake, not production. If the threshold is too loose, irrelevant passages from the same domain pass; if too tight, cross-lingual matches (an Egyptian Arabic question against an English document) are dropped.

**Fix.** [4.5](#stage-4--retrieval-quality) and [Stage 6](#stage-6--evaluation-harness-ongoing).

<a id="rag-11"></a>
#### RAG-11 · 🟡 `max_results` silently capped by the 6,000-char budget

**Verified:** ✅ · **Where:** `app/services/retrieval_service.py:42, 84-92` · `app/agents/registry.py:281-288` (promises "1 to 10")

```text
top_k= 4: passages delivered to the model = 4 (context 4718 chars, budget 6000)
top_k= 6: passages delivered to the model = 5 (context 5936 chars, budget 6000)
top_k=10: passages delivered to the model = 5 (context 5936 chars, budget 6000)
```

The loop uses `break` rather than `continue`, so it stops at the first block that does not fit. If the very first block were too large, the tool would return `""`, the exact failure its docstring warns about. That cannot happen with today's constants.

**Fix.** [4.6](#stage-4--retrieval-quality).

<a id="rag-12"></a>
#### RAG-12 · 🟡 Tiny, near-duplicate, oversized and mid-word chunks; numbers split

**Verified:** ✅ probes E1–E6 · **Where:** `app/services/chunking.py:89-96, 141-146, 149-172`

| Defect | Evidence |
|---|---|
| The minimum-size check runs **after** the 150-char overlap is added, so it never drops a tiny piece | `"Page 2"` became a 51-char chunk, 43 of its chars copied from the previous chunk (E1) |
| Overlap makes near-duplicate chunks when a paragraph spills just past 1,000 chars | a 195-char chunk, about 77% copied (E1) |
| A heading before a long paragraph becomes chunk 0 on its own | the 16-char chunk `"## Refund policy"` (E2) |
| Chunks exceed the 1,000-char budget | 1,107 chars measured (E4); up to about 1,152 possible |
| The overlap is cut by character count, mid-word | a chunk starting `"y inside Cairo takes…"` (E4) |
| Hard splits ignore whitespace and line breaks, and cut numbers | chunk ends `"… price 1,"`, next piece starts `"500 EGP …"` (E5); hard-wrapped PDF lines cut mid-line (E6) |
| The sentence regex splits on abbreviations | `"e.g. "` and `"Dr. "` end a "sentence" (code) |

**Fix.** [3.5](#stage-3--extraction-and-chunking-quality).

<a id="rag-13"></a>
#### RAG-13 · 🟡 No title or heading context in embeddings; Markdown structure ignored

**Verified:** ✅ probe E3 · **Where:** `app/services/chunking.py:71-108` · `app/services/knowledge_service.py:319` · claims in `knowledge_service.py:72-73` and `RAG.md:11`

Only the raw chunk text is embedded. Neither the document title nor the section heading goes with it, so a passage like "It costs 500 EGP and ships in 3 days" never says what "it" is, and "How much is the X200?" cannot find it. Markdown headings and lists are treated as ordinary lines: `source = markdown` behaves exactly like `text`, and a whole single-newline price list stays one paragraph.

**Fix.** [3.6](#stage-3--extraction-and-chunking-quality).

<a id="rag-14"></a>
#### RAG-14 · 🟡 No Unicode or Arabic normalisation

**Verified:** ✅ the missing normalisation (E7) · ⚠️ real-world Arabic PDF extraction not tested · **Where:** `app/services/chunking.py:57-68`

```text
normalise() output : ['0xfee3', '0xfeae', '0xfea3', '0xfe92', '0xfe8e']   (presentation forms kept)
NFKC would give    : مرحبا ['0x645', '0x631', '0x62d', '0x628', '0x627']
tatweel kept       : 'سعـــر' (U+0640 not removed)
NBSP runs kept     : 'price:\xa0\xa0\xa0500'
```

Diacritics, zero-width characters and soft hyphens are kept too. Not verified: how pypdf orders and encodes Arabic from real-world PDFs. Visual (reversed) order and presentation forms are common failures of PDF text layers, and the tests contain no Arabic PDF.

**Fix.** [3.4](#stage-3--extraction-and-chunking-quality).

<a id="rag-15"></a>
#### RAG-15 · 🟡 Retrieval is optional and left to the model

**Verified:** 📖 · **Where:** `app/services/agent_service.py:73-106` · `app/agents/orchestrator.py:189-213` · `claude.md:1036`

The knowledge base is consulted only if an admin grants `search_knowledge` **and** the model chooses to call it. Nothing warns a workspace that has documents but no agent with the tool, and the orchestrator never pre-fetches context for the customer's latest message. The product spec lists "Retrieve relevant knowledge" as a fixed orchestration step.

**Fix.** [4.7](#stage-4--retrieval-quality).

<a id="rag-16"></a>
#### RAG-16 · 🟡 No record of which model produced a vector; model changes corrupt silently

**Verified:** 📖 · **Where:** `app/core/config.py:75` · `app/integrations/openai/embeddings.py:111-117` · `app/db/models/knowledge.py:204-207`

Chunks do not store which model embedded them. Point `OPENAI_EMBEDDING_MODEL` at another model that can return 1536 dimensions (for example `text-embedding-3-large`) and it passes the width check, mixing two incompatible vector spaces. OpenAI documents the `dimensions` parameter as supported only by `text-embedding-3` and later models, so pointing it at an older model should make every ingestion fail, and RAG-02 then strands each document. ADR-018 admits "the documents still want re-ingesting", but nothing triggers or supports a bulk re-index.

**Fix.** [1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement) (schema) and [5.7](#stage-5--api-operations-metering-and-docs).

<a id="rag-17"></a>
#### RAG-17 · 🟡 No way to preview retrieval or inspect chunks

**Verified:** 📖 · **Where:** `app/schemas/knowledge.py:104-129` (`PassageRead`, `SearchRequest`, `SearchResponse`, documented as "for the search preview endpoint") with no matching route in `app/api/v1/knowledge.py`

When an agent answers wrongly, nobody can see what retrieval returned, and there is no endpoint that lists a document's chunks. This is the biggest obstacle to diagnosing every other issue here.

**Fix.** [5.1](#stage-5--api-operations-metering-and-docs). Do it early.

<a id="rag-18"></a>
#### RAG-18 · 🟡 Document list capped at 100 with no pagination

**Verified:** 📖 · **Where:** `app/api/v1/knowledge.py:39, 81-96` · `app/repositories/knowledge_repository.py:101-112`

Documents older than the newest 100 cannot be found through the API, so they cannot be re-indexed or deleted, and with RAG-01 versions pile up quickly.

**Fix.** [5.2](#stage-5--api-operations-metering-and-docs).

### 7.5 Low

<a id="rag-19"></a>
**RAG-19 · 🔵 Strict base64 rejects line breaks; `byte_size` is the base64 length.** ✅ probes F2 and P4 · `knowledge_service.py:83, 200`. `validate=True` rejects MIME-wrapped base64 and data-URL prefixes. For PDFs, `byte_size` records the base64 length: 3,520 bytes for a 2,640-byte file (+33%). → [3.3](#stage-3--extraction-and-chunking-quality)

<a id="rag-20"></a>
**RAG-20 · 🔵 Resubmitting identical text drops the new title and metadata.** ✅ probe E · `knowledge_repository.py:142-150`. Re-uploading with the title "Official price list 2026" kept `'Price list (DRAFT - do not use)'`. → [1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement), [5.3](#stage-5--api-operations-metering-and-docs)

<a id="rag-21"></a>
**RAG-21 · 🔵 Two concurrent identical uploads give an HTTP 500.** ✅ probe F (`['ok', 'IntegrityError']`) · `knowledge_repository.py:142-165`. The same race exists for knowledge-base names (`:63-71`). → [5.3](#stage-5--api-operations-metering-and-docs)

<a id="rag-22"></a>
**RAG-22 · 🔵 `processing` is never visible; a row lock is held during embedding.** ✅ probe H (another connection saw `['pending']` mid-embedding) · `knowledge_service.py:278-282`. The status update stays uncommitted while the provider is called, so a concurrent re-index or delete of that document blocks until the job ends. That can be many minutes for a large document: 60 s timeout × 3 attempts × each batch. → [Stage 1](#stage-1--versioned-documents-with-an-atomic-swap-the-re-index-requirement)

<a id="rag-23"></a>
**RAG-23 · 🔵 Embedding tokens are not metered.** 📖 · `embeddings.py:198-233` ignores the provider's `usage`. Ingestion, the largest embedding cost, records nothing at all; queries are counted only as `rag_query` events. ADR-027 and TASKS.md:281 say every path that consumes something is metered. → [5.4](#stage-5--api-operations-metering-and-docs)

<a id="rag-24"></a>
**RAG-24 · 🔵 Plan slots count versions and failures, and block resubmission at the limit.** ✅ probe B (one logical document used 2 slots) · `entitlement_service.py:240-245`, `api/v1/knowledge.py:109`. The slot check runs before de-duplication, so a workspace at its limit cannot even resubmit an existing document. → [5.5](#stage-5--api-operations-metering-and-docs)

<a id="rag-25"></a>
**RAG-25 · 🔵 UTF-16 is tried before cp1256 (WhatsApp text files).** ✅ · `extraction.py:39, 70-84`. **144 of 2,830 (5.1%)** cp1256 Arabic samples came back as Private-Use garbage, for example `'في' → '\ueddd'`, mostly short texts. The test at `tests/unit/test_extraction.py:113-115` only asserts `!= ""`. This is not on the KB path. → [3.7](#stage-3--extraction-and-chunking-quality)

<a id="rag-26"></a>
**RAG-26 · 🔵 Fragile retry policy; `OPENAI_TIMEOUT_SECONDS` is ignored.** 📖 · `embeddings.py:33-35, 128-161` · `config.py:85`. Three attempts with 1 s and 2 s linear backoff, no jitter and no `Retry-After`. A short 429 burst during a bulk upload fails whole documents, which RAG-02 then strands. → [Stage 2](#stage-2--a-durable-ingestion-queue), [5.6](#stage-5--api-operations-metering-and-docs)

<a id="rag-27"></a>
**RAG-27 · 🔵 Misleading tool output on an empty query or a provider failure.** 📖 · `retrieval_service.py:120-122` · `orchestrator.py:297-299`. An empty query returns "No information about this was found", so a model mistake becomes "the company has no information". A provider failure returns "That did not work: …" without telling the model not to answer from memory. → [4.7](#stage-4--retrieval-quality)

<a id="rag-28"></a>
**RAG-28 · 🔵 Retrieved text is not marked as data.** 📖 · `retrieval_service.py:84-92`. Passages are pasted in as plain tool output, so instructions inside a document look the same as real instructions. Only admins can upload, but documents often come from third parties such as supplier catalogues. → [4.6](#stage-4--retrieval-quality)

### 7.6 Info

<a id="rag-29"></a>
**RAG-29 · ⚪ The search fetches every result's embedding vector.** `knowledge_repository.py:278` selects the whole `DocumentChunk`, including 1536 floats per row that are never used. → [5.8](#stage-5--api-operations-metering-and-docs)

<a id="rag-30"></a>
**RAG-30 · ⚪ A future ANN index needs filter-aware design; pgvector image unpinned.** Migration `0007:11-16` · `docker-compose.yml:9` · `docker-compose.prod.yml:10`. With an HNSW or IVFFlat index, the tenant, KB and status filters are applied *after* the index scan. At the default `hnsw.ef_search = 40`, a small tenant in a large table can get fewer than k results. Plan for pgvector ≥ 0.8 iterative index scans or partial or partitioned indexes, and pin `pgvector/pgvector:pg16` to an exact version. → [5.9](#stage-5--api-operations-metering-and-docs)

---

## 8. Docs vs code

`Documentation_Protocol.md` §13 treats documentation drift as a bug. These are the RAG claims the code contradicts.

| # | Claim | Claimed in | What the code does | Bug |
|---|---|---|---|---|
| 1 | Agents can be pointed at different knowledge bases | `docs/RAG.md:9` · `models/knowledge.py:78-80` | No agent–KB link; the tool never filters | RAG-06 |
| 2 | "the first upload creates it" (the default KB) | `docs/RAG.md:9` · `knowledge_service.py:138-141` | Upload requires an existing KB id. `ensure_default_knowledge_base()` is only called by tests | — |
| 3 | "headings and lists are structure the chunker uses" | `docs/RAG.md:11` · `knowledge_service.py:72-73` | The chunker splits only on blank lines and sentence ends | RAG-13 |
| 4 | Extraction happens in the worker; "nothing … in the request" | `docs/RAG.md:17-27` · `ARCHITECTURE.md:186-202` · `api/v1/knowledge.py:11-13` | Extraction, PDF parsing included, runs in the request. It calls no provider | RAG-08 |
| 5 | "Failure is recorded on the document … status becomes failed" | `docs/RAG.md:33` · `docs/API.md:128` · `ARCHITECTURE.md:210` · `models/knowledge.py:48-49` | Rolled back by the worker's session; the document stays `pending` | RAG-02 |
| 6 | A stranded document "is findable through `list_pending`" | `docs/RAG.md:33` · `DECISIONS.md:394` | The method exists but nothing calls it. Documents are stranded without any outage too | RAG-04, 05 |
| 7 | Re-ingest is "safe … worst case is some wasted embedding calls" | `api/v1/knowledge.py:152-154` | The document goes offline and can stay offline | RAG-03, 04 |
| 8 | "requeueing is safe" / "a duplicated queue job harmless" | `DECISIONS.md:391` · `docs/RAG.md:31` · `ARCHITECTURE.md:452` | True for the chunk count, but every re-index hides the document | RAG-03 |
| 9 | "PDF extraction … [is] not" implemented | `ARCHITECTURE.md:184` | PDF extraction exists since phase 9, as `docs/RAG.md:3` says | — |
| 10 | "Usage recording (Phase 12) is not" implemented | `ARCHITECTURE.md:163` | `rag_query` is recorded | — |
| 11 | `[ ] Deferred to Phase 12: RAG usage metering` | `TASKS.md:155` | Implemented (`retrieval_service.py:129`) | — |
| 12 | `content_hash` is the "SHA-256 of the submitted bytes" | `models/knowledge.py:97-99, 136` | It is the SHA-256 of the normalised extracted text | — |
| 13 | A nullable embedding "lets ingestion fail partway without losing the chunking work" | `models/knowledge.py:202-203` | Chunks are written only after all embeddings succeed, in one transaction | — |
| 14 | "the search preview endpoint" | `schemas/knowledge.py:105` | No such route | RAG-17 |
| 15 | "Poll the document until its status is `ready`" | `api/v1/knowledge.py:113-114` · `docs/API.md:128` | The client may poll `pending` indefinitely | RAG-02, 04 |
| 16 | "Retrieve relevant knowledge" is an orchestration step | `claude.md:1036` | An optional tool the model decides to call | RAG-15 |
| 17 | Enqueue-before-commit only risks a job for a missing row | `DECISIONS.md:312` (ADR-015) | It also lets the worker read before the commit | RAG-04 |
| 18 | "Embeddings of unrelated text sit around 0.8 and above" | `retrieval_service.py:36-38` | Never measured with the configured model | RAG-10 |

**Dead or test-only code:** `ensure_default_knowledge_base()`, `DocumentRepository.list_pending()`, `DocumentChunkRepository.count_for_document()` and `list_for_document()`, `Document.is_retrievable`, `DocumentChunk.token_estimate` (written, never read), and `SearchRequest`, `SearchResponse` and `PassageRead`.

---

## 9. Why the test suite did not catch this

All **108** RAG tests pass against real PostgreSQL + pgvector while every 🔴 bug above is present. These are the gaps:

1. The failure tests (`test_knowledge_rag.py:295-365`) call `ingest()` on a session that never rolls back, so RAG-02 cannot show up.
2. No test runs `IngestionWorker.run_once()`. It is only constructed, in `tests/unit/test_worker_runner.py:131`.
3. No test updates a document (RAG-01). `test_submitting_the_same_text_twice_is_one_document` (`:234`) covers identical text only and never checks the title (RAG-20).
4. `test_a_failed_document_is_not_retrievable` (`:318`) **asserts** the re-index outage (RAG-03) as the desired behaviour.
5. No test covers the order of commit and enqueue across a real request (RAG-04).
6. Retrieval tests use a bag-of-words fake, so the threshold and ranking are never checked against the real model (RAG-10).
7. The chunking tests do not check the minimum size after overlap, the maximum including overlap, word-aligned overlap or headings (RAG-12, RAG-13).
8. The cp1256 test asserts only `!= ""` (RAG-25). There is no Arabic PDF fixture (RAG-14) and no PDF over 40 pages (RAG-07).

---

## 10. Fix plan

> [!TIP]
> The order is: **stop the silent loss first** (Stage 0, a few days), **build the versioned re-index** you asked for
> (Stage 1), **make the queue durable** (Stage 2), then improve quality (Stages 3–4) with an evaluation set measuring
> every change (Stage 6). Ship the search preview endpoint (5.1) early: it makes everything after it measurable.

```mermaid
flowchart LR
    S0["Stage 0<br/>Hotfixes"] --> S1["Stage 1<br/>Versioned documents<br/>+ atomic swap"]
    S1 --> S2["Stage 2<br/>Durable Postgres queue"]
    S0 --> P["5.1 Search preview"]
    S1 --> S3["Stage 3<br/>Extraction + chunking v2"]
    S3 --> RI["Bulk re-index<br/>with no downtime"]
    P --> S4["Stage 4<br/>Retrieval quality"]
    S6["Stage 6<br/>Evaluation set"] --> S3
    S6 --> S4
    S2 --> S5["Stage 5<br/>API, ops, metering, docs"]
```

**Effort sizes** (rough, one engineer, tests and docs included): **S** ≤ 1 day · **M** 2–4 days · **L** 1–2 weeks.

### Stage 0 — Hotfixes (stop silent knowledge loss)

| # | Change | Fixes | Size |
|---|---|---|---|
| 0.1 | **Record failures in a new transaction.** In `IngestionWorker._handle`, catch any exception, then open a fresh `Database.session()` and write `failed` and the reason. Catch `Exception`, not only `WaslaError`. Build the `EmbeddingsClient` inside the protected block so a missing key is recorded too | RAG-02 | S |
| 0.2 | **Re-index without downtime.** `reingest()` stops setting `pending` on a `ready` document and enqueues a job with `force=True`; decode `force` as optional so jobs already in the queue still parse. `ingest(force=True)` rebuilds even when the document is `ready`; the delete and insert already share one transaction, so the old chunks serve until the commit. A failed forced rebuild keeps `ready` and writes the reason to `error` | RAG-03, part of RAG-04 | S |
| 0.3 | **Close the commit race.** Commit before pushing to Redis: either the two routes commit explicitly, or `Database.session()` runs post-commit callbacks after `commit()` returns. `BackgroundTasks` are **not** enough (verified). Also have the worker treat "document not found" as retryable, requeueing with a delay up to N times, instead of dead-lettering it at once | RAG-04 | S |
| 0.4 | **Recover stuck work.** Add a periodic sweep to the ingestion worker: requeue documents `pending` for over 10 minutes, and move `inflight` entries older than a visibility timeout back to `pending`. That is safe here because ingestion is idempotent (ADR-019), unlike agent jobs. Add an admin endpoint to list and requeue dead letters | RAG-05 | M |
| 0.5 | **One-off repair.** Requeue every document currently `pending`. Produce a report of likely duplicate versions (same KB, same title or filename, different hash) for an admin to review. **Do not delete anything automatically** | data | S |
| 0.6 | **Tests that would have caught it.** Run the real `IngestionWorker.run_once()` with a mocked 503 (Appendix A is a ready-made start), turn D1 and D2 into race tests, and change `test_a_failed_document_is_not_retrievable` so a failed re-index **keeps serving** the previous version | all above | S |

### Stage 1 — Versioned documents with an atomic swap (the re-index requirement)

**Goal:** an updated document **replaces** its previous version, with **no downtime** and **no duplicates**. If a new version fails, the **previous one keeps serving** and the error is visible.

**1. Schema (new migration):**

```sql
-- Stable identity, named by the client (see the warning below about defaulting it to the filename).
ALTER TABLE documents ADD COLUMN source_key          varchar(300);
ALTER TABLE documents ADD COLUMN active_generation   integer NOT NULL DEFAULT 0;  -- 0 = nothing served yet
ALTER TABLE documents ADD COLUMN target_generation   integer NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN pending_content     text;                         -- version being built
ALTER TABLE documents ADD COLUMN pending_hash        varchar(64);
ALTER TABLE documents ADD COLUMN index_state         varchar(20) NOT NULL DEFAULT 'idle';  -- idle|queued|building|failed (an enum in the real migration)
ALTER TABLE documents ADD COLUMN attempts            integer NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN next_attempt_at     timestamptz;
ALTER TABLE documents ADD COLUMN last_error          varchar(500);

ALTER TABLE document_chunks ADD COLUMN generation      integer NOT NULL DEFAULT 1;
ALTER TABLE document_chunks ADD COLUMN embedding_model varchar(100) NOT NULL DEFAULT 'text-embedding-3-small';

-- Identity: one logical document per (KB, source_key). Hash de-duplication applies only to unkeyed pastes.
CREATE UNIQUE INDEX uq_documents_kb_source_key ON documents (tenant_id, knowledge_base_id, source_key)
    WHERE source_key IS NOT NULL;
ALTER TABLE documents DROP CONSTRAINT uq_documents_tenant_id_knowledge_base_id_content_hash;
CREATE UNIQUE INDEX uq_documents_kb_hash_unkeyed ON documents (tenant_id, knowledge_base_id, content_hash)
    WHERE source_key IS NULL;

ALTER TABLE document_chunks DROP CONSTRAINT uq_document_chunks_tenant_id_document_id_ordinal;
ALTER TABLE document_chunks ADD CONSTRAINT uq_document_chunks_doc_generation_ordinal
    UNIQUE (tenant_id, document_id, generation, ordinal);

-- Backfill. Every chunk that exists today is generation 1 (the column default).
-- Documents that have chunks serve them. This also restores documents hidden by RAG-03/RAG-04:
-- a document's text never changes, so its existing chunks are still valid.
UPDATE documents d SET active_generation = 1, target_generation = 1, status = 'ready'
 WHERE EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id);
-- Documents without chunks (stranded by RAG-02/RAG-04) are queued to build generation 1.
UPDATE documents d SET index_state = 'queued', target_generation = 1, pending_content = content,
                       pending_hash = content_hash, next_attempt_at = now()
 WHERE NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id);
```

> [!NOTE]
> This SQL was run against the current schema inside a transaction that was then rolled back. It applies cleanly, and on
> the probe database it restored the **2** documents hidden by RAG-03/RAG-04 and queued the **4** stranded ones. Write
> the real change as an Alembic migration with matching model changes, since CI runs `alembic check`.

**2. Identity rules (API):**

| Request | Result |
|---|---|
| `POST …/documents` with a `source_key` (or `filename`) that already exists in the KB | **New version** of that document (`created: false, version_queued: true`) |
| Same, and the text equals the version already served | No-op for the content; the title and metadata **are** updated |
| `POST …/documents` with no key | Today's behaviour: de-duplicate by hash |
| `PUT /knowledge/documents/{id}/content` | Explicit new version |
| `PATCH /knowledge/documents/{id}` | Title or filename only, no re-embedding |
| `POST /knowledge/documents/{id}/ingest` | Rebuild the same content as a new generation while the old one serves |

> [!WARNING]
> Defaulting `source_key` to the filename is convenient, but two different files that happen to share a name (two
> brands' `catalog.pdf` in one KB) would then replace each other. If that is a risk for your users, require an explicit
> `source_key` or a `replace_existing` flag instead. This is a product decision.

**3. Build-and-swap algorithm (the worker):**

```mermaid
sequenceDiagram
    autonumber
    participant API as API request
    participant DB as PostgreSQL
    participant W as Ingestion worker
    participant E as Embeddings API
    API->>DB: store pending_content, target_generation = G+1, index_state = queued
    API->>DB: COMMIT
    Note over DB: search keeps serving active_generation G
    W->>DB: claim row FOR UPDATE SKIP LOCKED, set building, remember G+1, COMMIT
    W->>E: embed the chunks of pending_content (no transaction held)
    E-->>W: vectors
    W->>DB: BEGIN, lock the row, check target_generation is still G+1
    W->>DB: INSERT chunks with generation G+1
    W->>DB: UPDATE active_generation = G+1, content = pending_content
    W->>DB: DELETE chunks whose generation is not G+1
    W->>DB: COMMIT, the atomic swap
    Note over DB: search now serves only G+1
    alt embedding or insert fails
        W->>DB: new transaction: attempts + 1, last_error, next_attempt_at, index_state
        Note over DB: G keeps serving and the error shows in the API
    end
```

- **Search change:** join on `c.generation = d.active_generation` instead of `d.status = 'ready'`, and add `c.embedding_model = :current_model` (RAG-16).
- **A stale job** (a newer upload bumped `target_generation` while it ran) finds the mismatch in its swap transaction and discards its work. That makes concurrent jobs and duplicated jobs safe.
- **No long transactions:** embedding happens between two short transactions, which also fixes RAG-22.
- **Status in the API:** `status` = serving state (`pending` = never served, `ready` = serving a version, `failed` = never served and the build failed), plus `index_state`, `last_error`, `active_generation` and `updated_at` for the build pipeline.
- **Plan slots:** count logical documents, not versions (RAG-24).

**4. Existing data.** Back-fill `source_key` from `filename` where that is unique within the KB. Ship an admin report of duplicate groups (same title or filename, different hash) with a merge action that keeps the newest version as the active one. Never merge automatically.

**5. Definition of done (as acceptance tests):**

- [ ] Uploading v2 with the same `source_key` leaves **one** document row, chunks of generation 2 only, and search returns only v2 text
- [ ] While v2 is being built, search returns v1, so there is **no outage**
- [ ] If v2 fails, v1 keeps serving, the API shows `last_error`, and a retry later succeeds
- [ ] Two jobs racing on one document produce one swap, no `IntegrityError`, and the stale job does nothing
- [ ] A job claimed before the submitting request commits is impossible (Stage 2) or harmless (Stage 0.3)
- [ ] A failure through the real worker is persisted
- [ ] v1 plus v2 count as **one** plan slot
- [ ] Re-uploading identical content with a new title updates the title and re-embeds nothing

**6. Records:** per `Documentation_Protocol.md`, add ADR-036 "Documents are versioned; chunks are swapped by generation", and update `docs/RAG.md`, `ARCHITECTURE.md` §7, `docs/API.md` and `TASKS.md` **in the same commits** as the code.

### Stage 2 — A durable ingestion queue

| # | Change | Fixes | Size |
|---|---|---|---|
| 2.1 | Claim work from PostgreSQL: `SELECT … FROM documents WHERE index_state = 'queued' AND next_attempt_at <= now() ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1`. This is the same pattern ADR-022 already chose for follow-ups. Claiming sets a lease (`next_attempt_at = now() + lease`) and the claim query also picks `building` rows whose lease has expired, so a worker that dies mid-build is retried; that replaces the in-flight reaper. Redis becomes an optional wake-up signal | RAG-04, RAG-05 (structurally) | M |
| 2.2 | Exponential backoff with jitter, honour `Retry-After`, separate retryable and permanent errors, `failed` once attempts run out. The previous version keeps serving | RAG-26, RAG-02 | S |
| 2.3 | Retire `inflight` and `:failed` for ingestion. A dead letter becomes `index_state = failed`, visible and re-queueable through the API | RAG-05 | S |
| 2.4 | Metrics and alerts: queued count, age of the oldest queued document, failed count, build duration | ops | S |
| 2.5 | ADR-037 "Ingestion is claimed from PostgreSQL; Redis only wakes the worker", superseding the parts of ADR-019 it replaces | docs | S |

### Stage 3 — Extraction and chunking quality

| # | Change | Fixes | Size |
|---|---|---|---|
| 3.1 | **Multipart upload** (`python-multipart` is already a dependency). Store the original through the storage interface (ADR-023) and **extract in the worker**. Keep the JSON door for small pastes | RAG-07, RAG-08 | M |
| 3.2 | Run pypdf in a worker thread or process with a CPU-time limit and a decompressed-size cap. Make the page cap configurable and record `page_count`, `pages_indexed` and `truncated`, shown in `DocumentRead` | RAG-07, RAG-08 | S |
| 3.3 | Accept base64 with whitespace or a data-URL prefix, and record the real byte size | RAG-19 | S |
| 3.4 | **Normalise the text that gets embedded:** NFKC; strip tatweel, zero-width characters and soft hyphens; turn NBSP into a space; optionally drop Arabic diacritics. Keep the original text for display. Add Arabic PDF fixtures (exported from Word and from Google Docs) that assert logical-order Arabic; try pypdfium2 if pypdf fails them | RAG-14 | M |
| 3.5 | **Chunker v2:** merge tiny pieces into a neighbour **before** adding overlap; align the overlap to word or sentence boundaries; count the overlap inside the budget; split on line breaks before any hard cut; hard-cut at the last whitespace; handle abbreviations; drop near-duplicate chunks | RAG-12 | M |
| 3.6 | **Structure and context:** split Markdown by heading and carry the heading path with each chunk; embed `"{title} › {heading path}\n\n{chunk}"` while storing the display text separately; give `source = markdown` its own handling | RAG-13 | M |
| 3.7 | Media text decoding: try UTF-16 only when a BOM is present, try cp1256 before latin-1, and make the test assert equality | RAG-25 | S |
| 3.8 | After v2 ships, **bulk re-index every document** with a command. This is safe only once Stage 1 makes re-indexing free of downtime | — | S |

### Stage 4 — Retrieval quality

| # | Change | Fixes | Size |
|---|---|---|---|
| 4.1 | **Per-agent KB scoping:** store `{"knowledge_base_ids": [...]}` in the `search_knowledge` grant's `AgentTool.config`, pass the grant config into `ToolContext`, and let the repository filter on a list. Decide the default for existing agents explicitly (all KBs keeps today's behaviour) | RAG-06 | S–M |
| 4.2 | **Candidates plus fusion:** take the top 20–30 by vector and add a keyword channel (PostgreSQL full-text search with the `simple` configuration, and/or `pg_trgm`; check that the extension exists on the production image), fused with Reciprocal Rank Fusion | RAG-09 | M |
| 4.3 | **De-duplicate and diversify:** merge adjacent chunks of one document into one passage, drop near-identical content (copies and versions), apply MMR | RAG-09 | S |
| 4.4 | **Re-rank, if the evaluation shows it pays:** score the fused candidates with a small LLM call using structured output (the Responses client already has `StructuredFormat`) or a cross-encoder. Keep it only if the gain is worth the latency and cost | RAG-09 | M |
| 4.5 | **Calibrate the threshold** from the Stage 6 evaluation set, per language. Make it a per-workspace setting with a measured default, and log distance histograms (never the text) | RAG-10 | S |
| 4.6 | **Context assembly:** budget in tokens (`estimate_tokens` exists), return only what fits and say how many passages were left out, add citation ids `[doc:ordinal]`, fence passages as reference data and not instructions, use `continue` instead of `break`, and never return an empty string | RAG-11, RAG-28 | S |
| 4.7 | **Make grounding dependable:** optionally retrieve automatically for the latest customer message, or set a per-agent `tool_choice` policy. Warn in the API when a workspace has documents but no active agent has `search_knowledge`. Treat an empty query as an argument error. Make failure output say "do not guess" | RAG-15, RAG-27 | M |
| 4.8 | Optional: rewrite the query using the conversation for follow-ups such as "and how much is the bigger one?" | quality | M |

### Stage 5 — API, operations, metering and docs

| # | Change | Fixes | Size |
|---|---|---|---|
| 5.1 | `POST /knowledge/search` preview using the existing schemas, plus admin-only `GET /knowledge/documents/{id}/chunks` | RAG-17 | S |
| 5.2 | Cursor pagination for documents; rename and delete for knowledge bases | RAG-18 | S |
| 5.3 | Idempotent inserts (`INSERT … ON CONFLICT DO NOTHING RETURNING`) for documents and KB names, so no more 500s; update metadata on resubmission | RAG-20, RAG-21 | S |
| 5.4 | Meter embedding tokens for ingestion and queries from the provider's `usage`, as new usage event types | RAG-23 | S |
| 5.5 | Count logical documents, and run the slot check only when a new logical document would be created | RAG-24 | S |
| 5.6 | Embedding client: honour `OPENAI_TIMEOUT_SECONDS`, and allow more attempts for ingestion than for live queries | RAG-26 | S |
| 5.7 | Store `embedding_model` per chunk, filter the search to the configured model, add a bulk re-embed command for model changes, and check at startup that the configured model supports `dimensions` | RAG-16 | S |
| 5.8 | Select only the columns the search needs | RAG-29 | S |
| 5.9 | Pin the pgvector image to an exact version. When adding HNSW, use pgvector ≥ 0.8 iterative scans or partial indexes, and measure recall across tenant sizes | RAG-30 | M |
| 5.10 | **Docs:** fix every row of [§8](#8-docs-vs-code) and remove or wire up the dead code, in the same commits as the fixes, per `Documentation_Protocol.md` §8 | drift | S |

### Stage 6 — Evaluation harness (ongoing)

- A **golden set** per workspace type: 50–200 questions, each with the expected document and passage, in **English, Modern Standard Arabic and Egyptian Arabic**, plus questions that **should** get "I don't know".
- **Metrics:** recall@k, MRR, no-answer precision, the share of duplicates in the top-k, and how often a stale version is returned.
- **Run it nightly** with the real embedding model (an API key in CI secrets), and require it to pass before any chunker, threshold or re-ranking change merges.

### Order of work

| Order | Item | Fixes | Size | Why at this point |
|:-:|---|---|:-:|---|
| 1 | 0.1 Record failures | RAG-02 | S | Every provider hiccup strands a document today |
| 2 | 0.3 Commit race | RAG-04 | S | Normal uploads can be stranded today |
| 3 | 0.2 Re-index without downtime | RAG-03 | S | Re-indexing takes knowledge offline |
| 4 | 0.4, 0.5 Sweep and repair | RAG-05 | M | Recovers everything already stuck |
| 5 | 5.1 Search preview | RAG-17 | S | Makes all later work measurable |
| 6 | Stage 1 Versioned documents | RAG-01, 16, 20, 22, 24 | L | Your requirement; stops stale answers |
| 7 | 4.1 KB scoping | RAG-06 | S–M | Internal material reaching customers |
| 8 | Stage 2 Durable queue | RAG-04, 05, 26 | M | Removes this whole class of bug |
| 9 | Stage 6 Evaluation set | — | M | Needed before tuning quality |
| 10 | Stage 3 Extraction and chunking | RAG-07, 08, 12, 13, 14, 19, 25 | L | Quality |
| 11 | Stage 4 Retrieval | RAG-09, 10, 11, 15, 27, 28 | L | Quality |
| 12 | The rest of Stage 5 | the rest | M | Hygiene |

---

## Appendices

### Appendix A — How the findings were reproduced

**Environment:** PostgreSQL 16 with pgvector 0.6.0, Redis 7, Python 3.13, the project installed with `pip install -e ".[dev]"` (FastAPI 0.141.1, SQLAlchemy 2.0.55, pypdf 6.21.4) and reportlab for generating PDFs.

**Baseline:** the RAG tests, **108 passed, 0 skipped**:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/wasla_review python -m pytest tests/unit/test_chunking.py tests/unit/test_extraction.py tests/unit/test_embeddings_client.py tests/unit/test_ingestion_queue.py tests/unit/test_knowledge_models.py tests/integration/test_knowledge_rag.py tests/integration/test_knowledge_endpoints.py
```

**The pipeline probe.** It drives the real `KnowledgeService`, Redis `IngestionQueue`, `IngestionWorker`, `Database.session()`, `RetrievalService`, pgvector search and the `search_knowledge` tool. Only the OpenAI HTTP endpoint is replaced, by an `httpx.MockTransport` returning the test suite's bag-of-words vectors, or a 503. It imports `tests.fake_embeddings`, so save it at the repository root and run it from there, against an empty database that has the `vector` and `pgcrypto` extensions:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/wasla_probe REDIS_URL=redis://localhost:6379/5 python probe_pipeline.py 2>/dev/null
```

<details>
<summary><b>probe_pipeline.py</b> (click to expand). It drops and recreates every table in the target database and flushes the Redis DB it is given</summary>

```python
"""End-to-end probes of the real ingestion/retrieval pipeline.

Real: KnowledgeService, IngestionQueue (Redis), IngestionWorker, Database.session()
commit/rollback, RetrievalService, pgvector cosine search, the search_knowledge tool.
Mocked: only the OpenAI HTTP endpoint (deterministic bag-of-words vectors from
tests/fake_embeddings.py, or a 503 to simulate an outage).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/wasla_probe")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/5")
os.environ.setdefault("OPENAI_API_KEY", "placeholder-not-a-key")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

import app.workers.ingestion_worker as ingestion_worker_module  # noqa: E402
from app.agents.registry import ToolContext, build_default_registry  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.redis import RedisClient  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.models.billing import LimitKey  # noqa: E402
from app.db.models.knowledge import EMBEDDING_DIMENSIONS, Document  # noqa: E402
from app.db.models.tenant import Tenant  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.integrations.openai.embeddings import EmbeddingsClient  # noqa: E402
from app.repositories.knowledge_repository import (  # noqa: E402
    DocumentChunkRepository,
    DocumentRepository,
)
from app.services.entitlement_service import EntitlementService  # noqa: E402
from app.services.knowledge_service import KnowledgeService  # noqa: E402
from app.services.retrieval_service import RetrievalService  # noqa: E402
from app.workers.ingestion_queue import IngestionQueue  # noqa: E402
from tests.fake_embeddings import embed_text  # noqa: E402

PRICES_V1 = """Apartment finishing price list

Economy finishing costs 4500 EGP per square metre and covers plaster, paint,
ceramic flooring and basic electrical work.

Premium finishing costs 7200 EGP per square metre and adds imported sanitary
fittings, gypsum ceiling detail and full air conditioning installation.
"""
# The same document after the business raised one price.
PRICES_V2 = PRICES_V1.replace("7200", "8500")

HR_MEMO = """Internal staff memo

Premium finishing staff bonus: sales staff earn 300 EGP per square metre of premium
finishing sold. Do not share commission rates with customers.
"""

QUERY = "premium finishing costs per square metre"

MODE = {"fail": False, "observer": None}


async def openai_handler(request: httpx.Request) -> httpx.Response:
    if MODE["observer"] is not None:
        await MODE["observer"]()
    if MODE["fail"]:
        return httpx.Response(503, json={"error": {"code": "server_error", "type": "server_error"}})
    body = json.loads(request.content)
    data = [
        {"object": "embedding", "index": i, "embedding": embed_text(text)}
        for i, text in enumerate(body["input"])
    ]
    return httpx.Response(200, json={"object": "list", "data": data, "usage": {"total_tokens": 1}})


def mock_http_client(*, seconds: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(openai_handler))


# The worker builds its own HTTP client; point it at the fake endpoint.
ingestion_worker_module.build_http_client = mock_http_client

settings = Settings()
database = Database(settings)
redis = RedisClient(settings)
worker = ingestion_worker_module.IngestionWorker(database=database, redis=redis, settings=settings)
queue = worker.queue


def banner(title: str) -> None:
    print("\n" + "=" * 86 + f"\n{title}\n" + "=" * 86)


async def build_schema() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    await redis.client.flushdb()


async def new_tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with database.session() as session:
        tenant = Tenant(name=slug.title(), slug=slug)
        session.add(tenant)
        await session.flush()
        base = await KnowledgeService(session=session, tenant_id=tenant.id).create_knowledge_base(
            name="General"
        )
        return tenant.id, base.id


async def api_submit(tenant_id, base_id, title, text):
    """Mirror of POST /bases/{id}/documents: handler runs, then the session commits."""
    async with database.session() as session:
        service = KnowledgeService(session=session, tenant_id=tenant_id, queue=IngestionQueue(redis.client))
        document, created = await service.submit(knowledge_base_id=base_id, title=title, raw=text)
        return document.id, created


async def api_reingest(tenant_id, document_id):
    """Mirror of POST /documents/{id}/ingest."""
    async with database.session() as session:
        service = KnowledgeService(session=session, tenant_id=tenant_id, queue=IngestionQueue(redis.client))
        await service.reingest(document_id)


async def state(tenant_id, document_id):
    async with database.session() as session:
        document = await DocumentRepository(session, tenant_id=tenant_id).get_by_id(document_id)
        rows = await DocumentChunkRepository(session, tenant_id=tenant_id).count_for_document(
            document_id=document_id
        )
        return {
            "status": document.status.value,
            "error": document.error,
            "chunk_count": document.chunk_count,
            "chunk_rows": rows,
            "title": document.title,
        }


async def depths():
    inflight = await redis.client.llen("knowledge:ingestion:inflight")
    return {"pending": await queue.depth(), "inflight": inflight, "failed": await queue.failed_depth()}


async def search(tenant_id, query=QUERY):
    async with mock_http_client() as http:
        embeddings = EmbeddingsClient(
            http=http, api_key="placeholder", model="m", dimensions=EMBEDDING_DIMENSIONS
        )
        async with database.session() as session:
            found = await RetrievalService(session=session, tenant_id=tenant_id, embeddings=embeddings).search(
                query=query
            )
            return [(p.document_title, round(p.distance, 3), _price(p.content)) for p in found.passages]


def _price(content: str) -> str:
    for token in ("7200", "8500", "300 EGP"):
        if token in content:
            return f"mentions {token}"
    return "no premium price"


async def main() -> None:
    await build_schema()

    banner("A. A provider outage during ingestion: is FAILED persisted by the real worker?")
    tenant, base = await new_tenant("probe-a")
    document, _ = await api_submit(tenant, base, "Prices", PRICES_V1)
    print("  after submit :", await state(tenant, document), await depths())
    MODE["fail"] = True
    await worker.run_once(wait_seconds=1)
    MODE["fail"] = False
    print("  after worker :", await state(tenant, document), await depths())

    banner("A2. No OPENAI_API_KEY configured")
    await redis.client.delete("knowledge:ingestion:failed")
    tenant, base = await new_tenant("probe-a2")
    document, _ = await api_submit(tenant, base, "Prices", PRICES_V1)
    keyless = ingestion_worker_module.IngestionWorker(
        database=database, redis=redis, settings=Settings(openai_api_key="")
    )
    await keyless.run_once(wait_seconds=1)
    print("  after worker :", await state(tenant, document), await depths())

    banner("B. Updating a document (one price changed) - replaced or duplicated?")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-b")
    v1, created_v1 = await api_submit(tenant, base, "Finishing price list", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    v2, created_v2 = await api_submit(tenant, base, "Finishing price list", PRICES_V2)
    await worker.run_once(wait_seconds=1)
    print(f"  v1 created={created_v1} {await state(tenant, v1)}")
    print(f"  v2 created={created_v2} {await state(tenant, v2)}")
    print("  search:", await search(tenant))
    async with database.session() as session:
        used = await EntitlementService(
            session, tenant_id=tenant, default_plan_code=settings.default_plan_code
        )._used(LimitKey.KNOWLEDGE_DOCUMENTS, subscription=None)
    print(f"  plan slots used by one logical document: {used}")

    banner("C. Re-indexing a READY document: availability while it runs, and if it fails")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-c")
    document, _ = await api_submit(tenant, base, "Prices", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    print("  ready        :", await state(tenant, document), "search ->", await search(tenant))
    await api_reingest(tenant, document)
    print("  reingest req :", await state(tenant, document), "search ->", await search(tenant))
    MODE["fail"] = True
    await worker.run_once(wait_seconds=1)
    MODE["fail"] = False
    print("  worker failed:", await state(tenant, document), "search ->", await search(tenant), await depths())

    banner("D1. Race: worker reserves the job before the submitting request commits")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-d1")
    async with database.session() as session:  # the API request's transaction
        service = KnowledgeService(session=session, tenant_id=tenant, queue=IngestionQueue(redis.client))
        doc, _ = await service.submit(knowledge_base_id=base, title="Prices", raw=PRICES_V1)
        document = doc.id
        await worker.run_once(wait_seconds=1)  # BLMOVE wakes the worker as soon as RPUSH lands
    print("  after commit :", await state(tenant, document), await depths())

    banner("D2. Race: reingest of a READY document, worker runs before the request commits")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-d2")
    document, _ = await api_submit(tenant, base, "Prices", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    async with database.session() as session:
        service = KnowledgeService(session=session, tenant_id=tenant, queue=IngestionQueue(redis.client))
        await service.reingest(document)
        await worker.run_once(wait_seconds=1)  # sees committed READY -> "reused" no-op -> released
    print("  after commit :", await state(tenant, document), await depths(), "search ->", await search(tenant))

    banner("E. Resubmitting identical text with a corrected title")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-e")
    document, _ = await api_submit(tenant, base, "Price list (DRAFT - do not use)", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    again, created = await api_submit(tenant, base, "Official price list 2026", PRICES_V1)
    print(f"  same id={again == document} created={created} stored title={(await state(tenant, document))['title']!r}")

    banner("F. Two concurrent uploads of the same file")
    tenant, base = await new_tenant("probe-f")

    async def request(title: str):
        async with database.session() as session:
            service = KnowledgeService(session=session, tenant_id=tenant)
            await service.submit(knowledge_base_id=base, title=title, raw=PRICES_V1)
            await asyncio.sleep(0.3)  # the rest of the request before its commit

    results = await asyncio.gather(request("one"), request("two"), return_exceptions=True)
    print("  outcomes:", [type(r).__name__ if isinstance(r, BaseException) else "ok" for r in results])

    banner("G. Knowledge-base scoping in the agent tool")
    await redis.client.flushdb()
    tenant, sales = await new_tenant("probe-g")
    async with database.session() as session:
        hr = await KnowledgeService(session=session, tenant_id=tenant).create_knowledge_base(name="Internal HR")
        archive = await KnowledgeService(session=session, tenant_id=tenant).create_knowledge_base(name="Archive")
        hr_id, archive_id = hr.id, archive.id
    await api_submit(tenant, sales, "Price list", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    await api_submit(tenant, archive_id, "Price list", PRICES_V1)
    await worker.run_once(wait_seconds=1)
    await api_submit(tenant, hr_id, "Staff memo", HR_MEMO)
    await worker.run_once(wait_seconds=1)
    async with mock_http_client() as http:
        embeddings = EmbeddingsClient(
            http=http, api_key="placeholder", model="m", dimensions=EMBEDDING_DIMENSIONS
        )
        async with database.session() as session:
            output = await build_default_registry().run(
                name="search_knowledge",
                arguments={"query": QUERY},
                context=ToolContext(
                    tenant_id=tenant, conversation_id=uuid.uuid4(), session=session, embeddings=embeddings
                ),
            )
    for line in output.splitlines():
        if line.startswith("["):
            print("    ", line)

    banner("H. Is PROCESSING ever observable outside the worker?")
    await redis.client.flushdb()
    tenant, base = await new_tenant("probe-h")
    document, _ = await api_submit(tenant, base, "Prices", PRICES_V1)
    seen: list[str] = []

    async def observe():
        async with database.session() as session:
            row = await session.scalar(select(Document.status).where(Document.id == document))
            seen.append(row.value)

    MODE["observer"] = observe
    await worker.run_once(wait_seconds=1)
    MODE["observer"] = None
    print(f"  status seen by another connection while embedding was in progress: {seen}")

    await database.dispose()
    await redis.close()


asyncio.run(main())
```

</details>

<details>
<summary><b>Chunker, extractor and PDF probes</b> (the key calls behind E1–E7, F1–F2 and P1–P4)</summary>

```python
from app.services import chunking, extraction

# E1: the minimum size is checked after the overlap is added
p1 = ("Economy finishing covers plaster and paint. " * 23).strip()
p3 = ("Premium finishing adds imported fittings and air conditioning. " * 20).strip()
chunks = chunking.split(f"{p1}\n\nPage 2\n\n{p3}")      # a 51-char chunk ending "Page 2" is kept

# E2: a heading before a long paragraph becomes its own chunk
chunking.split("## Refund policy\n\n" + "A customer may return any undamaged item within fourteen days. " * 25)[0]

# E5: a hard cut lands inside a price
row = "Model X200 | price 1,500 EGP | in stock | "
pad = "x" * ((1000 - (row.index("1,500") + 2)) % len(row))
chunking._hard_split(chunking.normalise(pad + row * 40))[0][-14:]   # '200 | price 1,'

# E7: no Unicode normalisation
chunking.normalise("ﻣﺮﺣﺒﺎ")    # presentation forms survive

# F1: cp1256 decoded as UTF-16 (about 5% of short Arabic samples)
extraction.extract_text("في".encode("cp1256"))           # '\ueddd'

# P1: 45 pages in, 40 pages out (PDF generated with reportlab), document still READY
```

</details>

**The FastAPI ordering probe** (the basis of RAG-04), using a minimal app with a `yield` dependency:

```text
1. handler returns (enqueue already done)
2. response body sent to client
3. dependency teardown (commit)

With BackgroundTasks:
1. handler returns
2. background task (would ENQUEUE)
3. dependency teardown (COMMIT)
```

### Appendix B — Every tunable

| Constant | Value | Where | Note |
|---|---|---|---|
| `MAX_CHUNK_CHARACTERS` | 1,000 | `chunking.py:29` | Real maximum is about 1,152 with overlap |
| `CHUNK_OVERLAP_CHARACTERS` | 150 | `chunking.py:31` | Counted in characters, not words |
| `MIN_CHUNK_CHARACTERS` | 40 | `chunking.py:34` | Checked after the overlap, so ineffective |
| `MAX_PAGES` | 40 | `extraction.py:32` | Later pages are dropped silently |
| `TEXT_ENCODINGS` | utf-8, utf-16, cp1256, latin-1 | `extraction.py:39` | Media path only |
| `MAX_DOCUMENT_CHARACTERS` | 400,000 | `knowledge_service.py:47` | Applies to the base64 string for PDFs |
| `EMBEDDING_DIMENSIONS` | 1536 | `models/knowledge.py:42`, migration `0007:31` | |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | `config.py:75` | Not recorded on chunks |
| `MAX_BATCH` | 96 | `embeddings.py:42` | Batches are sent one after another |
| `MAX_ATTEMPTS` / `BACKOFF_SECONDS` | 3 / 1.0 s, linear | `embeddings.py:34-35` | |
| `REQUEST_TIMEOUT_SECONDS` | 60 | `embeddings.py:33` | `OPENAI_TIMEOUT_SECONDS` is ignored |
| `DEFAULT_TOP_K` / `MAX_TOP_K` | 4 / 10 | `retrieval_service.py:34-35` | |
| `MAX_DISTANCE` | 0.75 (cosine distance) | `retrieval_service.py:39` | Uncalibrated |
| `MAX_CONTEXT_CHARACTERS` | 6,000 | `retrieval_service.py:42` | About 5 passages at most |
| `MAX_ROUNDS` | 3 | `orchestrator.py:42` | Tool rounds per turn |
| `BLOCK_SECONDS` | 5 | `core/redis.py:24` | Reserve wait |
| `RETRY_DELAY_SECONDS` | 5 | `ingestion_worker.py:36` | After a failed reserve only |
| List `limit` | at most 100 | `api/v1/knowledge.py:39` | No pagination |

### Appendix C — Files reviewed

`app/api/v1/knowledge.py` · `app/api/dependencies.py` · `app/core/dependencies.py` · `app/core/config.py` · `app/core/limits.py` · `app/core/redis.py` · `app/schemas/knowledge.py` · `app/services/knowledge_service.py` · `app/services/extraction.py` · `app/services/chunking.py` · `app/services/retrieval_service.py` · `app/services/entitlement_service.py` · `app/services/usage_service.py` · `app/services/agent_service.py` · `app/services/media_reader.py` (shared extraction) · `app/integrations/openai/embeddings.py` · `app/integrations/openai/client.py` · `app/integrations/openai/types.py` · `app/repositories/knowledge_repository.py` · `app/repositories/base.py` · `app/db/models/knowledge.py` · `app/db/models/agent.py` · `app/db/models/usage.py` · `app/db/models/billing.py` · `app/db/base.py` · `app/db/session.py` · `app/workers/ingestion_queue.py` · `app/workers/ingestion_worker.py` · `app/workers/queue.py` · `app/workers/ai_worker.py` · `app/workers/runner.py` · `app/agents/registry.py` · `app/agents/orchestrator.py` · `app/agents/memory.py` · `alembic/versions/20260821_0001_enable_required_extensions.py` · `alembic/versions/20260821_0007_create_knowledge_tables.py` · `tests/integration/test_knowledge_rag.py` · `tests/integration/test_knowledge_endpoints.py` · `tests/unit/test_chunking.py` · `tests/unit/test_extraction.py` · `tests/fake_embeddings.py` · `docs/RAG.md` · `docs/API.md` · `docs/AI_AGENTS.md` · `ARCHITECTURE.md` · `DECISIONS.md` (ADR-008, 015, 018, 019) · `TASKS.md` · `claude.md` §20–21 · `Documentation_Protocol.md`

<div align="center">

*End of review. No application code was modified.*

</div>
