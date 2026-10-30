# RAG Remediation — Part 3 of 3: New Formats, Scale & Operations

Part 3 of the remediation of `RAG_SYSTEM_REVIEW.md`, on top of Part 1 (`RAG_REMEDIATION_PART1_LIFECYCLE.md`, `131bfa4`) and Part 2 (`RAG_REMEDIATION_PART2_RETRIEVAL.md`, `b9c91c2`). This report also carries the **final ledger and the final verdict for the whole review**.

Branch `rag-remediation-p3-scale-20261030`, worktree `E:\wasla-rag-p3`, from `b9c91c2`. Nothing was pushed, merged or deployed. No real provider was contacted. Every number from fake embeddings is **structural** (does the right chunk exist, whole and findable by its words), not semantic.

The work ran across two sessions. The first built Phases 0 .. 12c (commits `39491a2` .. `cdd4579`). The second found the gap report half-written and uncommitted, and the `wasla-rg3-*` containers stopped by a Docker restart. It recreated `wasla-rg3-redis` on port 58981, because Windows had reserved 56981. Then it finished Phases 12d .. 18. The second session also re-checked the first session's work rather than trusting it:
- two test defects turned up (RR-33, RR-34) and are fixed;
- the first full lanes found 13 regressions Part 3 had caused in older suites (RR-35); they are fixed and both lanes were run again;
- at the user's request, the date bomb in `test_financial_integrity.py`, deferred to audit #24 until then, is fixed too.

## 1. Executive Summary

**This Part.**

- **New formats.** A workspace can upload its price list as CSV or Excel, and every row becomes one passage that is found on its own: 119 of 120 catalogue rows are retrieved as the exact row (structural). Word and HTML documents go through the same sandbox, normaliser, chunker, generation, dedupe and replacement path as text and PDF. Multipart routes now exist beside the base64 ones.
- **Embedding-model switch (RV-11).** The model can be changed per workspace through shadow generations. A rehearsal on a migration-built database, populated through the API, ran 512 step searches and 98 searches during the drains, with **0 empty and 0 doubled** at every coverage step. A failed shadow blocks its workspace's flip and is listed.
- **Query-embedding cache.** A repeated question costs one embedding call instead of twenty. The cache is keyed by workspace and space, holds vectors only, and never serves a passage.
- **Ingestion.** Embedding batches run concurrently under one renewal, and each generation is written in one multi-row insert. A 399,000-character document now becomes searchable in **5.0 s instead of 6.7 s** (fake latency), with nothing held across a provider call.
- **Operations.** Duplicates, unreachable knowledge, stale embeddings, switch coverage, failed shadows and bidi warnings are gauges, with alerts.
- **Knowledge-gap report.** Built, off by default, carried to the Privacy audit.
- **Row-level security is deferred.** In production the runtime role cannot bypass RLS, but the cross-tenant sweeps share that role. Applying it needs a second role, so ADR-185 records the blocker and the design.

Every guarantee of the review and of Parts 1 and 2 still holds (§15). All 26 Part 3 and re-applied mutants are killed (M-R64 not applicable). RR-01 .. RR-14 are 0.

**The whole review after three Parts.** Every RV finding is closed, or carries one of these statuses: accepted residual (RV-04 pre-stage documents, RV-22 Word layout), scale-triggered (RV-25), or external verification (RV-03 production version, RV-26 calibration). Every FX and IMP item is delivered, decided, deferred as a product decision, or left to external verification. Self-score: **38 → 80 → 91 → 92.5** (§31).

**RAG PART 3 (NEW FORMATS, SCALE & OPERATIONS) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

**RAG SYSTEM FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

## 2. Starting Repository State (and Part 2's state confirmed)

| Item | Value |
| --- | --- |
| `RAG_P3_BASE_HEAD` | `b9c91c2` (Part 2's report commit; Part 2's final code `eda48d6`) |
| Part 2 verdict | CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION (self-score 91) — the precondition holds |
| Part 1 verdict | CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION (`131bfa4`) |
| Branch / worktree | `rag-remediation-p3-scale-20261030`, `E:\wasla-rag-p3`; tracked tree clean before the first change; `audit/` untracked |
| Alembic head at base / at the end | `0134` / `0138` (single head) |
| ADRs at base / added | last ADR-178 / ADR-179 .. ADR-186, ADR-107 amended |
| Canonical `origin/worktree-billing-google-auth` | `7cdf863`, unchanged since Part 1 started; this branch is 60 commits ahead, 0 behind (Parts 1 + 2 + 3); Part 3 adds 17 commits |
| PostgreSQL / pgvector | 16.15 / **0.8.6** (the image the production compose file pins) |
| Redis / MinIO | 7.4.11 / `ghcr.io/mohamedshhahat1/minio:RELEASE.2025-04-22T22-12-26Z` |
| Docker / Compose / Python | 29.8.0 / 5.5.1 / 3.12.7 |
| `app.__file__` | `E:\wasla-rag-p3\app\__init__.py` (and each lane's own worktree, §22–§23) |
| Runtime database role (RQ-28) | production: a separate login role that owns nothing, `NOBYPASSRLS`, member of no role (`scripts/provision_runtime_db_role.py`); compose and CI: the owner `wasla` (§12) |
| `E:\wasla` | on `worktree-billing-google-auth` at `7cdf863` with the user's untracked documents — not touched |

## 3. Part 3 Findings Revalidation

Measured at `b9c91c2` on this Part's migration-built PostgreSQL before any change (`audit/rag/p3_probes_before.jsonl`; P-R31 .. P-R34 measured against the Part 2 tree in the second session, §8 and §12):

| Probe | At `b9c91c2` | Reproduces |
| --- | --- | --- |
| P-R17 | `OPENAI_EMBEDDING_MODEL` changed: 1 → **0** passages; the tool says "No information about this was found…" | yes (RV-11) |
| P-R28 | CSV, XLSX, DOCX, HTML: refused — `'csv' is not a valid DocumentSource` (and so on) | yes (FX-3.17) |
| P-R29 | 399,000 characters, 612 chunks, 7 provider calls, 0.2 s fake latency: **6.74 s** to ready | measured |
| P-R30 | the same question 20 times: **20** embedding requests | yes (FX-4.6) |
| P-R31 | 22,800-chunk corpus (26 workspaces: 12,000 / 6,000 / 24 × 200): 12,000-chunk workspace 80–86 ms p50, 92–115 ms p95; 200-chunk 31–37 ms p50 | measured |
| P-R32 | HNSW recall@10 vs exact, 12,000-chunk workspace: 0.942 | measured; the "lowered until autovacuum" premise did not reproduce (§8, RR-29) |
| P-R33 | as a runtime-shaped role, no tenant predicate: **302 rows of 2 workspaces** | yes |
| P-R34 | runtime role in production owns nothing and has `NOBYPASSRLS`; compose and CI connect as the owner; the sweeps share the runtime role | the RQ-28 blocker (§12) |

Eval harness "before" column: §27.

## 4. Locked Remediation Decisions

RQ-01 .. RQ-20, RQ-23, RQ-24 and RQ-29 from Parts 1 and 2 are in force and were not reopened. This Part applied:

| ID | Decision | Applied |
| --- | --- | --- |
| RQ-21 | new formats: CSV in-process; XLSX, DOCX and HTML in the sandbox; FAQ pairs; limits in `knowledge_limits.py`; refused, never truncated | as locked; OCR and larger limits DEFERRED PRODUCT DECISION |
| RQ-22 | multipart upload and replacement, additive | as locked |
| RQ-25 | shadow generations, per-workspace query space, flip at 100 %, throttled rebuild | as locked; no model switched anywhere |
| RQ-26 | query-embedding cache: Redis, `wasla:rag:qemb:{tenant}:{space}:{sha256(for_matching(query))}`, 24 h, on | as locked; result cache DEFERRED — LATER PRODUCT |
| RQ-27 | gap report: off, model-written query only, 30-day retention, purge, admin-only | as locked; carried to the Privacy audit (#18) |
| RQ-28 | RLS only if all three conditions hold | condition 3 fails: DEFERRED with the design (ADR-185) |

## 5. Product Decisions Applied

| Decision | Setting (default) | ADR |
| --- | --- | --- |
| CSV, XLSX, DOCX, HTML accepted; 1,000 rows, 50 columns, 2,000-character cells, 20 sheets; formulas stored as text | limits in code | ADR-179 |
| A 5,000-row catalogue is refused with the limit in the message and uploaded as five files (plan impact: a catalogue costs up to five documents of the plan's document limit) | — | ADR-179 |
| Multipart routes beside base64 JSON | — | ADR-180 |
| Embedding-model switch through shadows only | `OPENAI_EMBEDDING_MODEL_NEXT` (unset) | ADR-181 (amends ADR-107) |
| Query-embedding cache | `RAG_QUERY_EMBEDDING_CACHE=true`, `RAG_QUERY_EMBEDDING_CACHE_TTL_SECONDS=86400` | ADR-182 |
| Knowledge-gap report | `RAG_GAP_REPORT_ENABLED=false`, `RAG_GAP_RETENTION_DAYS=30` | ADR-183 |
| Concurrent embedding batches | `RAG_EMBEDDING_CONCURRENCY=3` (1 .. 4) | ADR-184 |
| Autovacuum and statistics for `document_chunks` | migration 0137 | ADR-184 |
| Row-level security deferred | — | ADR-185 |
| Partitioning trigger | — | ADR-186 |

## 6. New Formats and Multipart (FX-3.17, FX-4.5)

`document_source` gains `csv`, `xlsx`, `docx`, `html` (migration 0135, D9). XLSX, DOCX and HTML are parsed by `office_extract_child` in a separate interpreter. The child gets no secrets in its environment, `RLIMIT_AS` and `RLIMIT_CPU` where the platform has them, the 20 s wall clock, and the output-size cap. Before any parser reads XML, the child checks archive entries (1,000), inflated bytes (30 MB) and per-part compression ratio (200), and refuses any `DOCTYPE`/`ENTITY`. CSV is parsed in-process with the standard library, and UTF-16 without a BOM is recognised. openpyxl 3.1.5 (MIT) and python-docx 1.2.0 (MIT; brings lxml, BSD) are pinned in `pyproject.toml` and `requirements.lock`.

**Per format** (`audit/rag/p3_format_numbers.py`; real `extract_bounded`, the child's peak working set sampled with psutil):

| Fixture | Bytes | Outcome | Chunks | Parse | Child peak |
| --- | --- | --- | --- | --- | --- |
| CSV, 1,000 rows (in-process) | — | accepted | 1,000 (one per row) | 0.016 s | no child |
| XLSX, 1,000 rows | 23,863 | accepted | 1,000 (one per row) | 1.16 s | 45.4 MB |
| DOCX, 200-row table | 38,564 | accepted | 10 (DOCX is prose-chunked; tables are not row-chunked) | 0.42 s | 34.1 MB |
| DOCX, 6 FAQ pairs | 36,703 | accepted | 6 (one per pair) | 0.42 s | 33.5 MB |
| HTML, 6 FAQ pairs | 584 | accepted, script and style dropped | 6 (one per pair) | 0.17 s | 17.1 MB |
| XLSX with `=B2*2` and `=HYPERLINK(…)` | 4,964 | accepted, formulas kept as text, `formula_cells: 2` | 2 | 1.03 s | 44.4 MB |
| XLSX zip bomb (8 MB inflated) | 13,274 | **refused** in the child: "expands to more than can be read safely" | 0 | 0.16 s | 17.2 MB |
| DOCX with entity declarations | 36,712 | **refused** in the child: "declares XML entities" | 0 | 0.15 s | 17.1 MB |
| XLSX, 1,001 rows | 20,775 | **refused**: "more than 1000 rows. Split it…" | 0 | 0.96 s | 45.1 MB |
| 10,000-column CSV | — | **refused**: "The limit is 50" | 0 | — | — |
| BOM-less UTF-16 CSV (Arabic) | — | accepted: `Product: مكتبة model 35; Price: 4,500 EGP` | — | — | — |

A wall-clock overrun is killed: the child process has a return code after the refusal (`test_a_parse_past_the_wall_clock_is_killed_and_refused`). Office files are parsed only in the child, with no `OPENAI_API_KEY` or `DATABASE_URL` in its environment (`test_office_files_are_parsed_only_in_the_child`). On this Windows machine `RLIMIT_AS`/`RLIMIT_CPU` do not exist, which is the same as for PDFs since RAG-02. The archive checks and the wall clock are what bind here, and Linux containers add the resource limits (RR-30).

**Chunking.** A catalogue row is one chunk, `Column: value; …`, with the header repeated and the sheet as its breadcrumb (RR-14 counts violations). A question with its answer is one chunk in text, Markdown, DOCX, HTML and PDF. RR-27: Part 2's neighbour expansion and adjacent merge glued the next row or pair onto the matched one; now a row or pair is delivered whole and alone. This fix touched Part 2 code because it blocked a Part 3 item (`39491a2`).

**One pipeline.** Each format went through the generation path, `source_key`, dedupe and replacement. Per format, a replaced price list serves only its new price after the swap (`test_a_replaced_document_of_every_new_format_serves_only_its_new_version`, CSV / XLSX / DOCX / HTML). The same file in another knowledge base is refused, and the original is kept in the object store.

**Multipart (RQ-22).** `POST /knowledge/bases/{id}/documents/upload` and `PUT /knowledge/documents/{id}/content/upload` take the same limits and give the same answers as the JSON routes. A file past any document's limit gets `413 document_too_large`, and the route **stops reading at the limit**: a file five times the limit is refused after reading at most the limit plus one chunk (added in this session for M-R57, RR-34). The JSON routes are unchanged: 258 operations with the gap route.

## 7. Embedding-Model Switch (RV-11, FX-4.1) — the coverage rehearsal

**Mechanism (ADR-181, migration 0136, D11).**

- `workspace_embedding_spaces` gives each workspace that has started a switch its query space and its target.
- `document_generation_state` gains `shadow`: published in the target space and never read by retrieval. `active` and both partial unique indexes keep their meaning.
- A search embeds its query in its workspace's space. Claims embed a generation in that space, or a shadow in the target.
- The flip runs per workspace, only at 100 % coverage, in one transaction holding the workspace's space row. Publishes take that row shared first.
- A generation embedded in a space its workspace has left is put back, with its attempt refunded.
- `embedding-switch start|status|build|flip|retire|cancel`. `build` is the throttled background rebuild: `--rate` shadows per minute, one document per workspace per turn, resumable from the database.

**Rehearsal** (`audit/rag/p3_switch_rehearsal.py` → `p3_switch_rehearsal.json`). The database was recreated with `alembic upgrade head` (8.1 s). It was populated through the real routes, JSON and multipart: 2 workspaces × 32 documents = 64, all formats (text 20, Markdown 10, PDF 10, CSV 6, XLSX 6, DOCX 6, HTML 6), all ready. Every document was searched by its own text at each step:

| Step | Coverage (ws1 / ws2) | Searches | Empty | Doubled |
| --- | --- | --- | --- | --- |
| before | no switch | 64 | **0** | 0 |
| 0 % | 0/32 / 0/32 | 64 | **0** | 0 |
| ~50 % | 16/32 / 15/32 (1 blocked) | 64 | **0** | 0 |
| ~97 % | 31/32 / 30/32 (1 blocked) | 64 | **0** | 0 |
| ws1 100 %, ws2 blocked | 32/32 / 31/32 | 64 | **0** | 0 |
| ws1 flipped + retired (0.08 s), ws2 flip **refused** (exit 1, blocked document listed) | — | 64 | **0** | 0 |
| ws2 blocked shadow rebuilt (`--retry-blocked`) | 32/32 | 64 | **0** | 0 |
| both flipped and retired (ws2 0.06 s) | — | 64 | **0** | 0 |
| a searcher every 50 ms through every drain | — | 98 | **0** | 0 |

After the rehearsal: 64 active, 64 superseded, 1 failed (the deliberately failed shadow), and RR-01 .. RR-14 = 0. A replacement mid-switch retires the stale shadow, and only the new text flips (`test_a_document_replaced_mid_switch_gets_a_new_shadow_and_only_its_new_text_flips`). The query-embedding cache keys by space, so a flipped workspace never reads an old-space vector (`test_a_flipped_workspace_never_reads_a_vector_of_its_old_space`).

**What stays dangerous.** Changing `OPENAI_EMBEDDING_MODEL` directly still hides every unswitched document. P-R17 re-run at the head shows the same 1 → 0, by design. It is documented, counted by RR-09 and alerted by `KnowledgeStaleEmbeddings`. `test_a_model_change_excludes_old_vectors_until_they_are_reindexed` still holds.

## 8. Ingestion Concurrency, Search Micro-Optimisations, Autovacuum and Recall (FX-4.8, FX-4.7, FX-4.2)

**FX-4.8.** `prepare()` sends `RAG_EMBEDDING_CONCURRENCY` (3) batches as one wave, after one claim renewal in its own short unit of work. No transaction, lock or pooled connection is held across the provider. A renewal that fails stops further waves. Every batch that answered is metered, even when another batch in its wave failed, and vectors keep their chunks' order whatever order answers arrive in. Chunks are written in one `executemany` INSERT, with the lexemes turned into the tsvector in the statement.

| P-R29 (399,000 chars, 612 chunks, 7 calls, 0.2 s fake latency) | Before `b9c91c2` | After |
| --- | --- | --- |
| Time to ready | 6.74 s | **5.04 s** |
| Provider calls | 7 | 7 |

Batches overlapped in time at the fake transport, with one renewal per wave (`test_a_documents_batches_are_embedded_together_under_one_renewal_per_wave`, M-R60). Extraction stays in the request; moving it to the worker changes the API contract and is deferred.

**FX-4.7.** Neighbour expansion now asks for every passage's neighbours in one round trip, the first item Part 2's RR-25 named. `plan_cache_mode` and `hnsw.iterative_scan` stay `SET LOCAL` per search, in one round trip. The pool shares the runtime role with every subsystem, so setting them per role would change other subsystems' plans. `ef_search` is not tuned, because that needs the real-provider evaluation. The `tenant_id` statistics target is now 1,000.

P-R31: search p50 / p95 in ms, on the 22,800-chunk drill corpus. Before is the Part 2 tree (`E:\wasla-rg3-eval`, `b9c91c2`); after is this head. Same database, 60 searches per arm (`audit/rag/p_r31_before.json`, `p_r31_after.json`):

| Workspace / question / arm | Before | After |
| --- | --- | --- |
| 12,000 / SKU / hybrid (default) | 86.4 / 92.3 | 85.7 / 98.7 |
| 12,000 / words / hybrid | 80.7 / 101.6 | 82.3 / 102.1 |
| 12,000 / SKU / vector only | 79.9 / 105.8 | 84.5 / 109.1 |
| 6,000 / SKU / hybrid | 60.6 / 66.3 | 59.5 / 63.8 |
| 6,000 / words / hybrid | 56.5 / 59.0 | 57.6 / 62.6 |
| 200 / SKU / hybrid | 33.5 / 36.2 | 32.7 / 34.4 |
| 200 / words / hybrid | 30.9 / 35.0 | 29.2 / 34.7 |
| 200 / SKU / hybrid + re-rank (fake judge) | 36.8 / 41.9 | 38.5 / 48.5 |

There was no measurable change at any band: differences are within run-to-run noise in both directions. The one-round-trip neighbour lookup costs less than this noise at fake-embedding speed. RR-25 (Part 2's +20–25 ms p95 over the base) is therefore still open as an ACCEPTED RESIDUAL. The default search stays under 103 ms p95 at 12,000 chunks.

**FX-4.2.** Migration 0137 (D12) sets `document_chunks` to autovacuum and auto-analyse after 2 % of it changes (floor 1,000 rows), and to vacuum after 5 % is newly inserted. These are catalogue-only changes under `SHARE UPDATE EXCLUSIVE`, and reversible. P-R32 (`audit/rag/p_r32_recall.py`) measured 50 queries, top 10 from the HNSW index against exact search, after every one of the 22,800 chunks was deleted and re-inserted, as a mass re-index does:

| State | Dead tuples | Recall@10, 12,000-chunk workspace (min) | Plan the search chose | HNSW p50 / p95 | 200-chunk workspace |
| --- | --- | --- | --- | --- | --- |
| fresh index | 0 | 0.942 (0.8) | HNSW | 4.7 / 8.0 ms | 1.000, exact scan |
| after mass re-index, before vacuum (re-index 57.9 s) | 22,800 | 0.946 (0.8) | HNSW | 3.8 / 4.6 ms | 1.000 |
| after `VACUUM` (2.5 s) | 0 | 0.946 (0.8) | HNSW | 3.5 / 4.8 ms | 1.000 |

The review expected recall to drop until autovacuum ran. That did not reproduce here: with 22,800 dead entries the strict-order iterative scan stepped over them and recall held (RR-29). Production recall after the first mass re-index stays EXTERNAL VERIFICATION (RAG-14). `test_vector_index_at_scale.py` keeps the property in the ordinary integration lane on the 1,000-chunk clustered corpus. After every chunk is re-published, the approximate answer is still within 2 % of the exact one's distance and never names a deleted row, and 0137's parameters are in the catalogue.

## 9. Query-Embedding Cache (FX-4.6)

`QueryEmbeddingCache` (ADR-182) is read through in `RetrievalService` for the agent's tool. The key is `wasla:rag:qemb:{tenant}:{provider:model:dimensions:vN}:{sha256(for_matching(query))}`, with a TTL of 24 h. It holds vectors only, base64-packed, and validated as wide as the space on the way out.

| Measured (`test_query_embedding_cache.py`, real Redis, transport counted) | Result |
| --- | --- |
| P-R30, `RetrievalService` given the cache as the agent's tool gives it (`ToolContext.query_cache`): 20 identical questions | **1** embedding request (was 20); 19 saved; hit rate 19 / 20; meter: 1 embedding request, 20 searches |
| Two workspaces, the same words | 2 entries, 2 transport requests, 0 shared (M-R39) |
| A flipped workspace | misses into its new space; never reads an old-space vector (M-R39b) |
| A deleted or replaced document | never returned: the cache holds no passage, and the database decides every result |
| A hit vs the miss that filled it | identical passages and order (hybrid and fusion unchanged) |
| Redis unavailable | a miss, counted `error`; the search goes on |

The P-R30 probe re-run at the head builds `RetrievalService` without a cache, as the admin preview does, and still makes 20 requests. Only the agent's tool is given the cache (the AI worker builds it into `ToolContext`), by design (RR-31).

## 10. Knowledge-Gap Report (FX-4.10)

`knowledge_search_gaps` (migration 0138, D10) holds the query the model wrote (`for_storage`, 500 characters), its script (`ar`/`en`/`mixed`/`other`), the best distance and the time. Nothing is recorded while `RAG_GAP_REPORT_ENABLED` is false. Pre-retrieval never records anything, because its query is the customer's message (M-R68). `GET /knowledge/gaps?days=30` is for administrators only and returns 404 while the report is off. Rows past `RAG_GAP_RETENTION_DAYS` are deleted by the ingestion recovery pass whether or not the report is on (M-R62, RR-13), and the workspace purge erases them (M-R63). Tests, with the report enabled: an empty agent search is recorded with the model's query and a found search is not; another workspace's rows are invisible (M-R61). **Switching it on is carried to the Privacy audit (#18).**

## 11. Gauges and Alerts (FX-4.4); Partitioning Trigger (FX-4.3)

| Gauge | Source | Alert |
| --- | --- | --- |
| `wasla_knowledge_duplicate_documents` | Part 1 | `KnowledgeDuplicateDocuments` (info, 6 h) |
| `wasla_knowledge_unreachable_workspaces` | Part 1 | `KnowledgeUnreachable` (info, 1 h) |
| `wasla_documents_stale_embedding` (the prompt's "stale embeddings"; judged against each workspace's query space, so a switch never raises it) | Part 1, re-scoped here | **`KnowledgeStaleEmbeddings`** (warning, 30 min) |
| `wasla_rag_space_coverage_ratio` | this Part | **`EmbeddingSwitchStalled`** (info: below 1 and unchanged for 6 h, for 1 h) |
| `wasla_rag_shadow_generations{state}` | this Part | **`EmbeddingSwitchShadowsFailed`** (info, 1 h) |
| `wasla_rag_query_embedding_cache_total{outcome}` | this Part | **`QueryEmbeddingCacheErrors`** (warning, > 20 % for 15 min) |
| `wasla_knowledge_bidi_warning_documents` | this Part | **`KnowledgeBidiWarnings`** (info, 24 h) |

**FX-4.3.** `document_chunks` is not partitioned. ADR-186 sets three triggers: one workspace above 250,000 chunks, p95 above 300 ms on the HNSW path for the largest workspace, or 20 million rows. The largest drill workspace (12,000 chunks) is at about 100 ms p95, so RV-25 stays SCALE-TRIGGERED.

## 12. Row-Level Security (FX-4.9) — the blocker and the design

**P-R34.** Measured on `wasla_rg3_mig` with the real provisioning script creating a runtime role `rg3_runtime`:

| Role | superuser | `BYPASSRLS` | owns `document_chunks` |
| --- | --- | --- | --- |
| `wasla` (migrations; compose and CI runtime) | yes | yes | yes |
| `rg3_runtime` (production runtime shape) | no | no | no |

**P-R33.** As `rg3_runtime`, with no tenant predicate, the query returned **302 rows of 2 workspaces**. Then, in a rolled-back transaction, I enabled RLS on `document_chunks` with `USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)`:

- without the setting: **0 rows**;
- with one workspace set: **151 rows, 1 workspace**;
- the cross-tenant sweep shape, with the setting empty: **0 rows**.

**Blocker (RQ-28 condition 3).** The sweeps read across workspaces on the same runtime role, in the API and worker processes:

- `IndexingSweep.claim_due` and `list_stale`;
- the metrics service's knowledge gauges;
- the embedding switch's `switching_workspaces` and `build`;
- the gap retention pass;
- the workspace purge;
- the platform-staff views, `rag_triage` and `rag_invariants`.

Under the policy each of these would silently read nothing: documents never claimed, gauges at zero, a purge deleting nothing. Giving them a `BYPASSRLS` role needs a second privileged login and a second engine. That changes the database role model, which the provisioning script exists to forbid: it refuses any role membership and forces `NOBYPASSRLS`. Per the stop condition, **FX-4.9 is DEFERRED** with the design in ADR-185:

1. a `MAINTENANCE_DATABASE_URL` role with `BYPASSRLS` for the cross-tenant readers;
2. `ENABLE` + `FORCE ROW LEVEL SECURITY` on the six knowledge tables;
3. an `after_begin` listener applying `app.tenant_id` to every transaction of a workspace-bound session, including the one after `released()`;
4. P-R33 re-run to 0 rows, and M-R64.

Isolation remains enforced by the repository predicates, the composite foreign keys and the isolation gate, which now covers every Part 3 path (§15). The `rg3_runtime` role exists only in the disposable `wasla-rg3-pg`.

## 13. Observability and Alerts

Metrics added (no workspace and no text labels, G7):

- `wasla_knowledge_ingested_total{source}` (generations published, by format);
- `wasla_knowledge_format_refusals_total{source,reason}`;
- `wasla_knowledge_time_to_ready_seconds`;
- `wasla_rag_space_coverage_ratio`;
- `wasla_rag_shadow_generations{state}`;
- `wasla_rag_query_embedding_cache_total{outcome}`;
- `wasla_knowledge_gap_searches_total`;
- `wasla_knowledge_bidi_warning_documents`.

Five alerts were added (§11), each with a firing and a quiet promtool case. `docs/OBSERVABILITY.md` was updated.

## 14. RUNBOOK

The §8 docs-drift table stays empty. New sections in `docs/RUNBOOK.md`:

- "Changing the embedding model": the shadow switch, coverage, what blocks a flip, retirement, finishing;
- "KnowledgeStaleEmbeddings";
- "EmbeddingSwitchStalled / EmbeddingSwitchShadowsFailed";
- "Uploading catalogues": the formats, the refusal table, formulas;
- "Re-index everything (Part 3)": the throttled build, concurrency, autovacuum;
- "Search is slow (Part 3)": the cache, size bands, `ef_search`, statistics, the partitioning trigger;
- "QueryEmbeddingCacheErrors";
- "KnowledgeBidiWarnings";
- "Knowledge gaps": what is stored, retention, who reads it, switching it on and erasing it;
- "Row-level security": the deferral.

The old "Documents are served from an old embedding model" section now points to the switch.

## 15. Re-Verification (the review's guarantees and the matrices of Parts 1 and 2)

The review's "done well" properties (§9 of the review) still hold. Each test below passed in both lanes (§22–§23):

- cross-tenant search returns nothing foreign;
- the same-document re-index replaces its chunks (`test_ingesting_twice_does_not_double_the_chunks`, `test_a_reindex_serves_the_old_generation_until_an_atomic_swap`);
- a stale claim cannot publish, and a delete during an embedding call commits at once;
- a permanent failure becomes `failed` after one attempt, and a transient one `retry_exhausted` after five;
- a suspended workspace gets no claim and no provider call; with concurrency 3 it stops at the next wave, at most 3 requests;
- the PDF sandbox refuses more than 40 pages and over-limit or slow files, and never truncates;
- a NaN or wrong-width vector is a failed tool call and the turn continues;
- `test_a_model_change_excludes_old_vectors_until_they_are_reindexed`;
- retrieved text appears only in `function_call_output`.

Parts 1 and 2's decisive tests through every new path:

- a replaced CSV, XLSX, DOCX or HTML document serves only its new version;
- a cross-knowledge-base duplicate is refused in every format;
- a cache hit returns the same passages as a miss;
- every row of Part 1's and Part 2's runtime matrices.

M-R12, M-R18, M-R19, M-R20, M-R32, M-R33, M-R36 and M-R46 were re-applied and killed (§20).

**Isolation gate** (`test_knowledge_isolation_gate.py`): 13 tests. Two twin workspaces hold the same titles and rows, the foreign one also holds a marker row, and no foreign id, title, text or chunk may appear. This Part adds three tests:

- catalogues of every format, and their replacement: the foreign twin's identical documents are untouched;
- a switch in one workspace builds, covers and flips nothing in the other, and each is queried in its own model;
- the throttled rebuild touches only workspaces mid-switch, and a workspace whose switch has finished is neither rebuilt nor reported.

The cache (two workspaces → two entries), the gap report (admin of one workspace), the multipart routes (404 for a foreign base or document) and the purge have their own two-workspace tests. Each path has a mutant that removes its predicate (M-R39, M-R61, M-R66, M-R67), and each is killed.

## 16. Database Invariant Sweep

`scripts/rag_invariants.py` now includes RR-09 (active generation outside its workspace's query space), RR-13 (invariant: gap rows past the retention) and RR-14 (invariant: a catalogue chunk holding more than one row). Each one fails when exactly one violation is injected in a rolled-back transaction (`test_rag_invariants_sweep.py`, 13 cases). RR-13 and RR-14 here are the invariant checks the prompt named; Part 1 had used those numbers for findings (RR-28).

| Database | RR-01 .. RR-09, RR-12, RR-13, RR-14 (and RR-07's Python half) | RR-10 (originals sampled) |
| --- | --- | --- |
| `wasla_rg3_mig` (rehearsed: 64 documents, every format, both workspaces flipped and retired; upgraded to 0138) | all **0** | **0** of 64 |
| `wasla_rg3_rt` (round trip, generations in five states) | all **0** | — |

RR-11 (no expired document is returned) is a query property, proved by its test in both lanes.

## 17. Migrations and Alembic Gates

| Migration | What | Online | Downgrade |
| --- | --- | --- | --- |
| 0135 (D9) | `document_source` + `csv`, `xlsx`, `docx`, `html` (autocommit block) | yes | refuses while a document or generation uses them; enum labels cannot be dropped |
| 0136 (D11) | `document_generation_state` + `shadow`; `workspace_embedding_spaces`; `shadow_has_identity` (NOT VALID, then validated); audit label | yes | refuses while a shadow, a query-space row or a switch audit row exists |
| 0137 (D12) | `document_chunks` autovacuum storage parameters; `tenant_id` statistics 1,000 | yes (`SHARE UPDATE EXCLUSIVE`, `lock_timeout` 15 s) | resets to defaults |
| 0138 (D10) | `knowledge_search_gaps` | yes (new table) | refuses while it holds a row |
| D13 | RLS | not built (ADR-185) | — |

| Gate | Result |
| --- | --- |
| `alembic heads` | `0138 (head)`, single |
| `alembic check` | "No new upgrade operations detected." |
| fresh 0001 → head | in the migration-built lane (§23) and the rehearsal (8.1 s) |
| **Populated round trip**: seeded at 0134 **by the Part 2 code** (`E:\wasla-rg3-eval`; 2 workspaces, 24 documents, generations active 24, superseded 2, pending 2 with a replacement snapshot, processing 2, failed 2) → head (3.7 s) → 0134 (3.4 s) → head | each knowledge table identical in all four states: `knowledge_bases` 2 / `464468f5…`, `documents` 24 / `41b4cec9…`, `document_index_generations` 32 / `b94b8966…`, `document_chunks` 24 / `3d465a16…` (md5 over every row's 0134 columns; `audit/rag/p3_roundtrip.jsonl`) |
| Refusals | `test_part_three_migrations_refuse_to_discard_what_only_they_hold`: 0138 (a gap row) → 0136 (owed shadow, then query-space row, then audit row, each refused alone) → 0135 (a CSV document, then a generation snapshotting a CSV alone) → 0134 → head |
| Catalogue | 0 `NOT VALID` constraints, 0 invalid indexes |
| `db_preflight verify` | ok (pgvector 0.8.6) |

## 18. API Compatibility

Additive only:

- `POST /knowledge/bases/{id}/documents/upload` and `PUT /knowledge/documents/{id}/content/upload` (multipart);
- `source` values `csv`, `xlsx`, `docx`, `html` on every route that takes a source;
- `GET /knowledge/gaps` (admin; 404 while disabled);
- operator commands `embedding-switch start|status|build|flip|retire|cancel`.

Stable refusals:

| Refusal | Code |
| --- | --- |
| a file past a format limit | `422 document_limit_exceeded` (never truncated) |
| bytes that are not the declared format | `422 document_format_mismatch` |
| a file past any document's size | `413 document_too_large` |
| the gap report while it is off | `404` |

Every existing route and field is unchanged; the base64 JSON upload still works (`test_the_json_route_still_takes_base64_and_answers_the_same`). `docs/API.md` and OpenAPI list 258 operations.

## 19. Ported Probe Tests

| Probe | Test | Lane |
| --- | --- | --- |
| P-R17 | `tests/integration/test_embedding_space_switch.py` (8 tests) | both; ordinary integration job |
| P-R28 | `tests/integration/test_knowledge_formats.py` (+ hostile fixtures), `test_knowledge_upload_routes.py` | both |
| P-R29 | `tests/integration/test_ingestion_concurrency.py` | both |
| P-R30 | `tests/integration/test_query_embedding_cache.py` | both (real Redis: CI's service) |
| P-R31, P-R32 | `tests/integration/test_vector_index_at_scale.py` (1,000-chunk corpus, fast enough for the ordinary lane; the 22,800-chunk drill stays a local script, `audit/rag/p_r31_latency.py`, `p_r32_recall.py`, because it seeds 398 MB in two minutes); the 0137-parameters test skips on the model-built schema by design | both |
| P-R33 | not ported: RLS deferred | — |

The project has no slow marker. Every ported test runs in CI's ordinary `tests` and `migration-parity` jobs.

## 20. Mutation Matrix

This Part's own runner (`audit/rag/run_rag_mutations_p3.py` + `p3_mutants.py`) uses the strict protocol of the earlier stages. Every anchor must be found exactly once. A mutant counts as killed only when one of its named killer tests fails. A control run goes first, and every file is restored byte for byte (SHA-256 checked). It ran in its own worktree, `E:\wasla-rg3-mut`, on `wasla_rg3_mut`. Control: 43 killer tests green. Re-run after the two strengthened tests: control 4 green.

| Mutant | Killed by |
| --- | --- |
| M-R39 cache key without the workspace | `test_two_workspaces_asking_the_same_words_share_nothing` |
| M-R39b cache key without the space | `test_a_flipped_workspace_never_reads_a_vector_of_its_old_space` |
| M-R40 flip check removed | `test_a_permanently_failed_shadow_blocks_the_flip_and_is_listed` |
| M-R55 "complete" means half covered | same |
| **M-R65** (the prompt's M-R54) shadow published as active | `test_a_shadow_is_never_served_before_the_flip` |
| M-R56 query embedded in the target space for an un-flipped workspace | `test_each_workspace_is_queried_in_its_own_space` |
| M-R66 switch coverage without the tenant predicate | gate `test_a_switch_in_one_workspace_builds_and_flips_nothing_in_the_other` |
| M-R67 throttled rebuild over every workspace | gate `test_the_throttled_rebuild_touches_only_workspaces_that_started_a_switch` (survived the first run; test strengthened, RR-34) |
| M-R43 archive inspection skipped in the sandbox | `test_a_zip_bomb_workbook_is_refused_inside_the_sandbox` |
| M-R43b sandbox wall clock removed | `test_a_parse_past_the_wall_clock_is_killed_and_refused` |
| M-R57 multipart route skips its size limit | `test_an_oversized_upload_is_refused_before_it_is_read_whole` (survived the first run; test added, RR-34) |
| M-R58 catalogue rows packed | `test_a_catalogue_is_one_chunk_per_row_under_its_sheet` |
| M-R59 formula read as its cached value | `test_a_formula_is_stored_as_its_text_and_never_as_a_cached_result` |
| M-R60 waves without claim renewal | `test_a_documents_batches_are_embedded_together_under_one_renewal_per_wave` |
| M-R61 gap rows read without the workspace predicate | `test_an_admin_reads_only_their_own_workspaces_gaps` |
| M-R62 gap retention removed | `test_rows_past_the_retention_are_deleted_by_the_recovery_sweep` |
| M-R63 gap rows not purged | `test_the_workspace_purge_erases_the_report` |
| M-R68 pre-retrieval records the customer's message | `test_the_pre_retrieval_search_with_the_customers_message_is_never_recorded` |
| M-R12 (re-applied) claim reads `documents.content` | `test_an_updated_document_serves_one_version_through_the_hybrid_search` |
| M-R18 cross-KB dedupe removed | `test_the_same_text_in_another_knowledge_base_is_refused` |
| M-R19 dedupe made global | `test_source_keys_and_dedupe_look_only_inside_the_workspace` |
| M-R20 retrieval-time repeats kept | `test_a_legacy_copy_is_served_once` |
| M-R32 lexical without the tenant predicates | `test_the_lexical_query_returns_nothing_foreign` |
| M-R33 lexical without the active-generation predicate | `test_a_superseded_generations_chunk_is_never_retrieved_by_its_exact_words` |
| M-R36 validity window removed | `test_a_document_outside_its_window_is_never_retrieved` |
| M-R46 lexical without the validity window | `test_an_expired_document_is_never_retrieved_by_its_exact_words` |
| M-R64 RLS policy dropped | **NOT APPLICABLE** — RLS deferred (ADR-185) |

**26 killed, 0 survivors, 0 meaningful survivors.** All source files were restored, and `git status --short app/` in `E:\wasla-rg3-mut` is empty.

## 21. Test Non-Vacuity

| Test | Proven to reach its boundary | Fails at `b9c91c2` / its mutant |
| --- | --- | --- |
| formats | the child ran (`create_subprocess_exec` recorded, `office_extract_child.py`, no secrets); the hostile fixtures refused inside it with their reason ("expands to more than…", "declares XML entities"); the killed child has a return code | at base: no such source; M-R43, M-R43b, M-R58, M-R59 |
| switch | every coverage step is a real `RetrievalService` search through worker-built shadows; the transport records the model each query was embedded in | at base: P-R17 returns 0; M-R40, M-R55, M-R56, M-R65 |
| cache | the second request did not reach the fake transport (requests counted); two workspaces produce two transport requests | at base: 20 requests; M-R39, M-R39b |
| concurrency | timestamps at the fake transport overlap; renewals counted per wave | M-R60 |
| gap report (enabled in the test) | rows written for empty searches only; erased by the real `WorkspacePurgeService` | M-R61, M-R62, M-R63, M-R68 |
| isolation gate, switch fixtures | the viewer's own shadow and space rows are counted by the same query that finds the foreign ones absent | M-R66, M-R67 |
| multipart limit | the route read something, and at most limit + one chunk | M-R57 |
| at-scale index | the plan names the HNSW index; more than 900 rows really deleted and re-inserted | — |
| RLS | not applicable | — |

## 22. Model-Built Test Results

Model-built schema on `wasla_rg3_models`, PostgreSQL 16.15 / pgvector **0.8.6**, Redis DB 3, run from `E:\wasla-rg3-lane` (`app.__file__` = `E:\wasla-rg3-lane\app\__init__.py`). Each run is the whole suite.

| Run | Commit | collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `7cd1b9e` | 7,905 | 7,861 | **14** | 30 | 0 | 0 | 60.2 min |
| 2 | `2f5791b` | 7,905 | 7,874 | **2** | 29 | 0 | 0 | 61.4 min |
| **3 (final)** | `8afa99e` | 7,905 | **7,876** | **0** | 29 | 0 | 0 | 64.5 min |

- **Run 1:** the date bomb plus the 13 Part 3 regressions of RR-35. The regressions were fixed in `745080b` and `b4644db`, and the date bomb in `2f5791b`.
- **Run 2:** `test_workspace_purge.py::test_the_purge_is_audited_as_a_system_act` and `::test_purging_twice_is_a_no_op`. Both read every `workspace_purged` audit entry, and Part 3's gap-report purge test, run earlier on the committing harness, left one for its own workspace (RR-39). Fixed in `8afa99e`: the assertions read their own workspace's entry, and the gap test removes the trail it commits. Both files then passed, in that order, in both schemas.

Skips in run 2 (29), all opt-in or environmental and none added by Part 3: 11 real-provider (no key), 4 schema parity (model lane by design), 5 Windows signal path, 4 OAuth concurrency (real-Redis variable), 2 kept-data sweeps (run separately, §24), 2 old-pgvector (opt-in), 1 tmpfs.

## 23. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations`, a fresh `alembic upgrade head` to `0138` on `wasla_rg3_migrations`, PostgreSQL 16.15 / pgvector **0.8.6**:

| Run | Commit | collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `7cd1b9e` | 7,905 | 7,865 | **15** | 25 | 0 | 0 | 59.6 min |
| 2 | `2f5791b` | 7,905 | 7,878 | **2** | 25 | 0 | 0 | 62.8 min |
| **3 (final)** | `8afa99e` | 7,905 | **7,880** | **0** | 25 | 0 | 0 | 65.9 min |

- **Run 1:** the model-built 14, plus `test_schema_parity.py::test_the_models_and_the_migrations_build_the_same_catalog` (0137's storage was missing from the model; RR-35).
- **Run 2:** the two purge-audit tests of RR-39.

Skips (25): 11 real-provider, 5 Windows signal path, 4 OAuth concurrency, 2 kept-data sweeps, 2 old-pgvector, 1 tmpfs. Schema parity ran and passed in run 2.

## 24. CI Jobs Run Locally (with the known deferred failures)

| Workflow / job | Run locally | Result |
| --- | --- | --- |
| `ci.yml` `quality` — Ruff, Black, MyPy | as in CI | clean (§25) |
| `ci.yml` `tests` — model-built suite, then app start and migrations apply / revert / reapply | full lane (§22); migration gates (§17) | §22 |
| `ci.yml` `migration-parity` — enums, parity, every integration and e2e suite migrated | full lane (§23) | §23; parity ran |
| `ci.yml` `migration-parity` — AI and tool invariants over kept data | the exact step (`WASLA_TEST_KEEP_AI_DATA=1`, the `ai_harness` selection, fail on any skip), migration-built, at `8afa99e` | **347 passed, 0 skipped** |
| `ci.yml` `monitoring` | promtool in `prom/prometheus:v3.1.0` | SUCCESS (§26) |
| `ci.yml` `docker` — build the `runtime` target, container serves liveness | `docker build --target runtime` (6.4 min), then run as `staging` with a generated signing key on a free port | built; `/health/live` → `{"status":"alive"}`. Inside the image: uid 1001, openpyxl 3.1.5 and python-docx import, `RLIMIT_AS` available (RR-30) |
| `security.yml` dependency audit | `pip-audit -r requirements.lock --no-deps` | 17 advisories in 5 pre-existing pins, none in Part 3's dependencies (RR-38) |
| `security.yml` auth suite, secret scan, container scan; `deploy.yml` | not run | auth suite inside the lanes; no secrets added; deploy out of scope |
| Race / concurrency suites, 3 consecutive runs each | `test_upload_concurrency`, `test_lifecycle_concurrency`, `test_ingestion_concurrency`, `test_rag_ingestion_lifecycle`, `test_embedding_space_switch` | **3 / 3 green each** (the switch suite on its three isolated runs; RR-37) |

**Known failures.** The date bomb is fixed (`2f5791b`). FRM-05, FRM-07 and CPM-06 did not fail in any Part 3 run and were not changed; they remain open for audit #24.

## 25. Ruff / Black / MyPy

At `8afa99e`:
- `ruff check . --exclude audit`: all checks passed.
- `black --check . --exclude audit`: 1,090 files unchanged.
- `mypy app tests`: no issues in 961 source files.

The new `# type: ignore` comments are narrow ones on SQLAlchemy's untyped `DDL(...)` call, where the existing `fillfactor` DDL already carries the same ignore.

## 26. Prometheus / Alertmanager Gates

`promtool check config` (prom/prometheus:v3.1.0): SUCCESS, **91 rules**. `promtool test rules tests/alerts_test.yml`: SUCCESS, including the five new alerts, each with a firing case and a quiet case. `alertmanager.yml` is unchanged (`amtool check-config` in §24).

## 27. Evaluation Results (every column of the three Parts)

Fake embeddings — **structural**. Each column is the final harness (`tests/integration/test_rag_eval.py` + `rag_eval_part3.py`, `audit/rag/eval_p3.sh`) run against that phase's commit, so columns are comparable. The final column was run three times at `b4644db`. Part 3's table was identical in all three runs. Part 2's table moved by ± 1 only on the identifier-blind, vector-only subset and on passages delivered (RR-20: RRF ties broken by random chunk ids).

**Part 3 metrics:**

| Metric (fake embeddings) | Part 2 after 10 / base `b9c91c2` | After 11a (`39491a2`) | After 12a (`2975613`) | Final ×3 (`b4644db`) |
| --- | --- | --- | --- | --- |
| Catalogue rows retrieved as the exact row (CSV / XLSX) | 0 / 120 (refused) | 119 / 120 | 119 / 120 | **119 / 120** |
| FAQ questions matched to their own answer (text, Markdown, DOCX, HTML, PDF × 6) | 0 / 30 (DOCX, HTML refused) | 29 / 30 | 29 / 30 | **29 / 30** |
| Labelled spans intact in some chunk (all formats) | 6 / 126 | 126 / 126 | 126 / 126 | **126 / 126** |
| Questions with a stale version in the top results (replaced catalogue) | n/a (CSV refused) | 0 / 60 | 0 / 60 | **0 / 60** |
| Duplicate passages per search | 0.0 | 0.0 | 0.0 | **0.0** |
| Answerable-but-empty during a model switch | 64 / 64 after a direct change (P-R17) | n/a | 0 / 512 + 0 / 98 (rehearsal, §7) | **0** (switch tests, both lanes) |

The two misses are the same in every run since 11a: the catalogue row for SKU `X-0135`, and the text-format FAQ question "How long does delivery t…". Both rows are intact in their chunks (126 / 126). Why the fake model ranks them below the cut is not investigated: fake vectors measure structure, and a semantic miss is for the real-provider run (RR-36).

**Parts 1 and 2 metrics at the final head** (unchanged by Part 3 apart from RR-20's ± 1):

| Metric | Part 2 final (`f446b01`) | Part 3 final |
| --- | --- | --- |
| Labelled spans intact / of 261 | 255 | 255 |
| Span in top 5 / at rank 1 (threshold lifted) | 249 / 236 | 249 / **237** |
| Answerable-but-empty at 0.75 | 6 / 261 | 6 / 261 |
| Stale version in the top results | 0 / 1 | 0 / 1 |
| Spans retrievable by exact words alone | 257 / 261 | 257 / 261 |
| Exact-identifier questions (vector only → hybrid) | 32 → 57 / 57 | 32–33 → **57 / 57** |
| Follow-up questions (as typed → standalone) | 0 → 10 / 10 | 0 → 10 / 10 |
| Cross-lingual (as typed → with `alternate_query`) | 2 → 6 / 8 | 2 → **7 / 8** |
| Two-chunk answers complete (`max_results` 1 / 5) | 22 / 24, 24 / 24 | 22 / 24, 24 / 24 |
| Passages delivered vs kept at the largest `max_results` | 1,188 / 1,188 | 1,255–1,257, all delivered (the corpus gained Part 3's documents) |
| Duplicate passages per search | 0.0 | 0.0 |

**Real-provider metrics — EXTERNAL VERIFICATION, not run:**

| Metric | MSA | Egyptian | English | Cross-lingual | Catalogue rows | FAQ pairs |
| --- | --- | --- | --- | --- | --- | --- |
| recall@5 | | | | | | |
| MRR@10 | | | | | | |
| nDCG@5 | | | | | | |
| answerable-but-empty | | | | | | |

## 28. Final Findings Ledger (every review ID, with the Part that closed it)

This is the final ledger of the whole review. Nothing remains "DEFERRED TO PART n".

### Root causes

| ID | Status | Closed by |
| --- | --- | --- |
| RC-1 normalisation | CLOSED | Part 1 (RV-01, RV-23, FX-1.1) |
| RC-2 lifecycle | CLOSED | Part 1 (RV-02, RV-07, FX-2.1 .. FX-2.7) |
| RC-3 chunking | CLOSED | Part 1 (RV-05, RV-06, RV-08, RV-09, FX-1.2); Part 3 (catalogue rows, FAQ pairs in every format, FX-3.17) |
| RC-4 single-stage retrieval | CLOSED in code; semantic effect EXTERNAL VERIFICATION | Part 2 (FX-3.3, FX-3.4, FX-3.11) |
| RC-5 query formulation | CLOSED in code; real-model adherence EXTERNAL VERIFICATION | Part 2 (FX-3.5) |
| RC-6 scope | DECISION APPLIED | Part 2 (FX-3.6, RQ-18) |
| RC-7 fragile triggers | CLOSED | Part 1 (default grant, `knowledge_unreachable`); Part 3 adds no trigger |

### Findings

| ID | Status | Part |
| --- | --- | --- |
| RV-01 | CLOSED | 1 |
| RV-02 | CLOSED | 1 |
| RV-03 | CLOSED (guard); production version EXTERNAL VERIFICATION | 1 |
| RV-04 | CLOSED as detection and warning; repair of pre-stage documents ACCEPTED RESIDUAL | 1 (gauge and alert, Part 3) |
| RV-05, RV-06, RV-07, RV-08, RV-09, RV-10 | CLOSED | 1 |
| RV-11 | **CLOSED** by the shadow switch (the direct setting change stays dangerous: documented, RR-09, alerted) | **3** |
| RV-12, RV-13, RV-14 | CLOSED | 1 |
| RV-15 | CLOSED | 2 |
| RV-16, RV-17, RV-18 | CLOSED | 1 |
| RV-19 | CLOSED | 2 |
| RV-20 | CLOSED | 1 |
| RV-21 | CLOSED | 2 |
| RV-22 | CLOSED for Chromium and WeasyPrint; Word two-logical-pages layout ACCEPTED RESIDUAL | 1 |
| RV-23 | CLOSED | 1 |
| RV-24 (§8 docs table) | CLOSED — table empty | 1 + 2; kept empty in 3 |
| RV-25 | SCALE-TRIGGERED (ADR-186; re-checked at 22,800 chunks, §8) | 2, 3 |
| RV-26 | CLOSED in code (telemetry, tiers); calibration EXTERNAL VERIFICATION | 1, 2 |
| RV-27 | CLOSED | 1 |

### Fix plan

| ID | Status | Part |
| --- | --- | --- |
| FX-0.1 | CLOSED (image pinned); production version EXTERNAL VERIFICATION | 1 |
| FX-0.2, FX-0.3, FX-0.4 | CLOSED as the triage command | 1 |
| FX-0.5 | CLOSED | 1 |
| FX-1.1 .. FX-1.10, FX-1.12, FX-1.13 | CLOSED | 1 |
| FX-1.11 | CLOSED (§8 empty) | 1 + 2 |
| FX-2.1 .. FX-2.7 | CLOSED | 1 |
| FX-3.1 | CLOSED (structural harness, extended in each Part); real-provider run EXTERNAL VERIFICATION | 1, 2, 3 |
| FX-3.2, FX-3.3, FX-3.5, FX-3.6, FX-3.8, FX-3.9, FX-3.11 | CLOSED | 2 |
| FX-3.4 | dedupe, merge and cap CLOSED; re-rank DECISION APPLIED (off) | 2 |
| FX-3.7 | EXTERNAL VERIFICATION (switch only through FX-4.1) | — |
| FX-3.10 | CLOSED | 1 |
| FX-3.12, FX-3.14, FX-3.15 | DECISION APPLIED (off) | 2 |
| FX-3.13 | DECISION APPLIED (observe) | 2 |
| FX-3.16 | CLOSED; `prompt_cache_key` EXTERNAL VERIFICATION | 1, 2 |
| FX-3.17 | **CLOSED**; OCR and larger upload limits DEFERRED PRODUCT DECISION | **3** |
| FX-4.1 | **CLOSED** | **3** |
| FX-4.2 | **CLOSED** (recall measured locally); production recall EXTERNAL VERIFICATION | **3** |
| FX-4.3 | SCALE-TRIGGERED (ADR-186) | **3** |
| FX-4.4 | **CLOSED** (duplicate and unreachable gauges in Part 1; stale, coverage, shadows, cache, bidi and their alerts here) | 1, **3** |
| FX-4.5 | **CLOSED** (multipart) | **3** |
| FX-4.6 | **CLOSED** (embedding cache); result cache DEFERRED — LATER PRODUCT | **3** |
| FX-4.7 | **CLOSED** for the safe changes (one-round-trip neighbours, statistics target); no measurable p95 change — RR-25 ACCEPTED RESIDUAL; `ef_search` waits for calibration | **3** |
| FX-4.8 | concurrency and multi-row insert **CLOSED**; extraction in the worker DEFERRED PRODUCT DECISION (API contract) | **3** |
| FX-4.9 | DEFERRED — LATER PRODUCT: the stop condition of RQ-28; blocker and design in ADR-185 | **3** |
| FX-4.10 | DECISION APPLIED, default off, carried to the Privacy audit (#18) | **3** |

### Improvements

| ID | Delivered by | Part | Status | Setting (default) |
| --- | --- | --- | --- | --- |
| IMP-01 | FX-3.1 | 1–3 | CLOSED (structural); real metrics EXTERNAL VERIFICATION | — |
| IMP-02 | FX-1.13 | 1 | CLOSED | — |
| IMP-03 | FX-1.1 | 1 | CLOSED | always on |
| IMP-04 | FX-1.2 | 1 | CLOSED | chunk constants |
| IMP-05 | FX-3.2 | 2 | CLOSED | always on |
| IMP-06 | FX-2.1 .. FX-2.3 | 1 | CLOSED | — |
| IMP-07 | FX-3.17, FX-4.5 | 1 (FAQ in text, Markdown), **3** | CLOSED | always on; limits in `knowledge_limits.py` |
| IMP-08 | FX-3.3 | 2 | CLOSED | `RAG_HYBRID_ENABLED=true` |
| IMP-09 | FX-3.5 | 2 | CLOSED | tool schema |
| IMP-10 | FX-3.4 | 2 | dedupe and diversity CLOSED; re-rank DECISION APPLIED | `RAG_RERANK_ENABLED=false` |
| IMP-11 | FX-3.6, FX-2.7 | 1, 2 | CLOSED | grant config |
| IMP-12 | FX-3.7 | — | EXTERNAL VERIFICATION | `OPENAI_EMBEDDING_MODEL_NEXT` (the switch) |
| IMP-13 | FX-1.10 | 1 | CLOSED | `MAX_KNOWLEDGE_SEARCHES_PER_TURN=2` |
| IMP-14 | FX-3.13 | 2 | DECISION APPLIED | `RAG_NUMBER_GROUNDING=observe` |
| IMP-15 | FX-3.11 | 2 | CLOSED | always on |
| IMP-16 | FX-3.12 | 2 | DECISION APPLIED | `RAG_CONFIDENCE_TIERS_ENABLED=false` |
| IMP-17 | FX-3.14 | 2 | DECISION APPLIED | `RAG_PRERETRIEVAL_ENABLED=false` |
| IMP-18 | FX-3.15 | 2 | DECISION APPLIED | `RAG_PARALLEL_SENTIMENT_ENABLED=false` |
| IMP-19 | FX-4.6 | **3** | CLOSED (embedding cache); result cache DEFERRED — LATER PRODUCT | `RAG_QUERY_EMBEDDING_CACHE=true` |
| IMP-20 | FX-4.8 | **3** | concurrency CLOSED; extraction in the worker DEFERRED PRODUCT DECISION | `RAG_EMBEDDING_CONCURRENCY=3` |
| IMP-21 | FX-4.7 | **3** | CLOSED (safe changes; RR-25 residual) | — |
| IMP-22 | FX-3.16 | 1, 2 | CLOSED; cache key EXTERNAL VERIFICATION | `RAG_PASSAGE_MARGIN=` (inert) |
| IMP-23 | FX-1.12 | 1 (extended in 2, 3) | CLOSED | — |
| IMP-24 | FX-4.9 | **3** | DEFERRED — LATER PRODUCT (ADR-185) | — |
| IMP-25 | FX-4.10 | **3** | DECISION APPLIED, off | `RAG_GAP_REPORT_ENABLED=false`, `RAG_GAP_RETENTION_DAYS=30` |
| IMP-26 | FX-3.9 | 2 | CLOSED | admin route |

### Decisions and remediation items

| ID | Status |
| --- | --- |
| RQ-01 .. RQ-11, RQ-23, RQ-24, RQ-29 | DECISION APPLIED (Part 1) |
| RQ-12 .. RQ-20 | DECISION APPLIED (Part 2) |
| RQ-21, RQ-22, RQ-25, RQ-26, RQ-27 | DECISION APPLIED (Part 3) |
| RQ-28 | DECISION APPLIED as its own stop condition: RLS DEFERRED — LATER PRODUCT with the design (ADR-185) |
| OCR, PyMuPDF (AGPL), larger upload limits, the catalogue row limit's plan impact, extraction in the worker, hosted / self-hosted re-rankers and embedding models | DEFERRED PRODUCT DECISION |
| Result cache, LLM-written chunk context, multi-agent routing, FUT-RAG-05 citations, FUT-RAG-06 grounded-only mode | DEFERRED — LATER PRODUCT |
| RR-01 .. RR-08, RR-10, RR-11 | CLOSED (Part 1); 0 on this Part's populated databases |
| RR-09 active generation outside the workspace's query space | CLOSED (Part 3; check built, proved non-vacuous, 0) |
| RR-12 active chunk without lexical text | CLOSED (Part 2), 0 |
| RR-13 (Part 1 finding) Word export, two logical pages per PDF page | ACCEPTED RESIDUAL |
| RR-13 (invariant, Part 3) gap rows past the retention | CLOSED (check built, non-vacuous, 0) |
| RR-14 (Part 1 finding) downgrades committing mid-run | CLOSED (Part 1) |
| RR-14 (invariant, Part 3) a catalogue chunk holding more than one row | CLOSED (check built, non-vacuous, 0) |
| RR-15, RR-16, RR-18, RR-19, RR-21, RR-24, RR-26 | as Part 2 (CLOSED / CLOSED BY DESIGN) |
| RR-17 ranking moves during the first `reindex-all` with hybrid on | ACCEPTED RESIDUAL with the procedure (Part 2) |
| RR-20 ± 1 between eval runs on the identifier-blind subset | ACCEPTED RESIDUAL (Info) |
| RR-22 `prompt_cache_key` unsupported by the client | EXTERNAL VERIFICATION |
| RR-23 a `strict`-withheld turn ends `empty_response` | ACCEPTED RESIDUAL |
| RR-25 default search +20–25 ms p95 over the base | ACCEPTED RESIDUAL (FX-4.7's safe changes measured no gain; < 103 ms at 12,000 chunks) |
| RR-27 Part 2's neighbour expansion and merge glued the next catalogue row / FAQ pair on | CLOSED (Part 3, `39491a2`) |
| RR-28 ID collisions: the prompt's M-R54 was Part 2's identifier mutant (renamed M-R65); the prompt's RR-13 / RR-14 invariants share numbers with Part 1's findings | CLOSED by qualification (both kept, labelled) |
| RR-29 "recall lowered until autovacuum" did not reproduce at 22,800 chunks (0.942 → 0.946 → 0.946) | NOT REPRODUCED locally; production recall EXTERNAL VERIFICATION |
| RR-30 no `RLIMIT_AS` / `RLIMIT_CPU` on Windows for the office child (as for PDFs) | ACCEPTED RESIDUAL (development machines only; the archive checks and the wall clock bind everywhere; Linux images apply both limits) |
| RR-31 the admin preview and direct service calls do not use the query cache | CLOSED BY DESIGN |
| RR-32 a direct change of `OPENAI_EMBEDDING_MODEL` still hides every unswitched document (P-R17 at the head) | ACCEPTED RESIDUAL — the switch is the supported path; RR-09 and `KnowledgeStaleEmbeddings` detect it |
| RR-33 two new isolation fixtures counted shadows by `trigger = 'shadow'` (the trigger is `embedding_switch`): vacuous | CLOSED (`c6046fa`, with the viewer's own rows as non-vacuity) |
| RR-34 M-R57 and M-R67 survived the first run: the 413 test did not prove the route stops reading, and the rebuild fixture had no workspace with a finished switch | CLOSED (`7cd1b9e`; both killed on re-run) |
| RR-35 the first full lanes found 13 Part 3 regressions in older suites: NUL accepted by the multipart form text (3), `workspace_embedding_spaces` not classified for the purge (3 purge tests), 0137's storage absent from the model (schema parity), the multipart file fields unregistered in the bounds sweep (2), HTML not a recognised store type, the ToolContext allowlist, the alternate-query batching test meeting the cache, the README range and the API count (2) | CLOSED (`745080b` product fixes, `b4644db` test and docs updates); re-run in both schemas (566 / 570 passed), then the full lanes again (§22–§23) |
| RR-39 two older purge-audit tests read every workspace's `workspace_purged` entry; Part 3's gap-report purge test committed one for its own workspace and broke them in the full lanes | CLOSED (`8afa99e`: scoped to their own workspace; the gap test removes its trail) |
| RR-36 two structural eval misses (SKU `X-0135`; one text FAQ question) | ACCEPTED RESIDUAL (Info; the rows are intact; semantic ranking is for the real-provider run) |
| RR-37 two race-suite runs errored because I ran another suite on the same database at the same time | CLOSED (not a product defect; three isolated runs green, §24). `build()` reads every workspace mid-switch, so its tests need the database to themselves, as CI runs them |
| RR-38 `pip-audit` of `requirements.lock`: 17 advisories in certifi, dnspython, opentelemetry-api, pyjwt, pypdf — all pinned before this remediation; none in Part 3's openpyxl, python-docx, lxml, et-xmlfile | EXTERNAL ACTION (dependency hygiene, outside the RAG stage; pypdf bumps have broken CI before, CPM-05) |
| Date bomb `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` (red since 2026-10-27; deferred to audit #24 until the user asked for it here) | CLOSED (`2f5791b`): the fixture's purchase expired on 2026-10-27, so the expiry CHECK (23514) refused the grant before the unpaid-invoice trigger (23000) under test; the period is now anchored to the real clock and the trigger refuses again |
| FRM-05, FRM-07, CPM-06 (load-sensitive timing tests) | did not fail in either Part 3 lane; not changed — loosening a passing timing bound would weaken it. Still open for audit #24 |

## 29. Remaining External Actions (for the whole review)

Not performed:

1. Review all three Parts. Then **one canonical merge verification** of the stacked branch (`rag-remediation-p3-scale-20261030`, which contains Parts 1 and 2), in its own prompt. Then merge into `worktree-billing-google-auth`, run CI, push.
2. **First deploy:**
   - `db_preflight` confirms pgvector ≥ 0.8;
   - Part 1's back-fills (`backfill-normalised-content`, `backfill-source-keys`), acting on the listed collisions;
   - `RAG_HYBRID_ENABLED=false`, then `reindex-all` at a chosen rate. One pass builds normalised text, the new chunks, contextual embeddings and lexical text;
   - `rag_invariants verify` until RR-01 .. RR-14 are 0, then `RAG_HYBRID_ENABLED=true`.

   No RLS role is needed (ADR-185).
3. **Real-provider evaluation**, only when authorised: the opt-in harness with the real embedding model. Calibrate `MAX_DISTANCE`, the confidence tiers and the passage margin. Measure recall@5, MRR@10 and nDCG@5 per language mix.
4. **FX-3.7:** compare `text-embedding-3-small` with `-large` at 1,536 dimensions on the eval set. Switch only through `embedding-switch`, and only after the numbers.
5. Per workspace, after calibration: tiers, pre-retrieval, re-rank, grounding `correct`/`strict`, parallel sentiment.
6. Real customer files: Arabic PDFs from Word, Google Docs and Canva; real Excel catalogues; real Word documents.
7. Production HNSW recall after the first mass re-index (RAG-14).
8. **Product decisions:** OCR; PyMuPDF licence; hosted or self-hosted re-rankers and embedding models; larger upload limits; extraction in the worker; the catalogue row limit's plan impact.
9. **Privacy audit (#18):** the gap report before it is switched on; originals retention.
10. Row-level security as part of a stage that changes the database role model (ADR-185's design).
11. Multi-agent routing as its own feature stage.
12. After the merge: the infrastructure cleanup prompt (§30).

**External verification items** (never CLOSED here): retrieval quality with the real model (every semantic metric); the 0.75 threshold and every calibrated cut-off; FX-3.7; the production pgvector version; real customer PDFs beyond the producers this machine has; `prompt_cache_key` and the cached-token share in real traffic; grounding's false-positive rate on real replies; production HNSW recall.

## 30. Infrastructure Created by the Three Parts

| Kind | Part | Name | State | After the merge |
| --- | --- | --- | --- | --- |
| Worktree / branch | 1 | `E:\wasla-rag-p1` on `rag-remediation-p1-lifecycle-20261029` | kept | remove |
| Worktrees | 1 | `E:\wasla-rg1-base` (`7cdf863`), `E:\wasla-rg1-mut` (`ba34fe1`) | kept, clean | remove |
| Containers | 1 | `wasla-rg1-pg` (55961), `wasla-rg1-pg-old` (55962), `wasla-rg1-redis` (56961), `wasla-rg1-minio` (59961) | exited, not touched | remove with volumes |
| Worktree / branch | 2 | `E:\wasla-rag-p2` on `rag-remediation-p2-retrieval-20261029` | kept | remove |
| Worktrees | 2 | `E:\wasla-rg2-base` (`131bfa4`), `E:\wasla-rg2-mut` (`f446b01`), `E:\wasla-rg2-eval` (`da0525f`) | kept, clean | remove |
| Containers | 2 | `wasla-rg2-pg` (55971), `wasla-rg2-redis` (56971), `wasla-rg2-minio` (59971) | exited, not touched | remove with volumes |
| Worktree / branch | **3** | `E:\wasla-rag-p3` on `rag-remediation-p3-scale-20261030` | kept for review; `audit/` untracked | keep until merged, then remove |
| Worktree | **3** | `E:\wasla-rg3-eval` (detached `b9c91c2`) | kept: the "before" tree for P-R31 and the round-trip seed; clean | remove |
| Worktree | **3** | `E:\wasla-rg3-mut` (detached `7cd1b9e`) | kept; `git status --short app/` empty — no mutant applied | remove |
| Worktree | **3** | `E:\wasla-rg3-lane` (detached `7cd1b9e`) | kept: both full lanes ran from it; clean | remove |
| Containers | **3** | `wasla-rg3-pg` (127.0.0.1:55981, PostgreSQL 16.15, pgvector 0.8.6), `wasla-rg3-minio` (59981), `wasla-rg3-redis` (**58981**; recreated in session 2 from the same pinned image after Windows reserved 56981) | running | remove with volumes |
| Databases | **3** | `wasla_rg3_models`, `wasla_rg3_migrations`, `wasla_rg3_probe`, `wasla_rg3_eval`, `wasla_rg3_mut`, `wasla_rg3_mig` (rehearsed, at 0138), `wasla_rg3_scale` (22,800 chunks, 398 MB), `wasla_rg3_rt` (round trip); per-test migration databases dropped by their tests | kept | drop with the container |
| Role | **3** | `rg3_runtime` on `wasla-rg3-pg` (P-R33/P-R34) | kept | drop with the container |
| Redis DBs | **3** | 3 (lanes, cache tests), 4 (mutation), 5 (eval), 7 (rehearsal) on `wasla-rg3-redis` | kept | — |
| Bucket / local store | **3** | `wasla-rg3-media` on `wasla-rg3-minio`; `%TEMP%\wasla-rg3-media` (rehearsal originals) | kept | remove |
| Scratch | 1–3 | `E:\wasla-rag-p3\audit\rag\` (untracked); `E:\wasla-rag-notes\probes\`, `\probes-p2\` | kept | copy `audit/rag/` to `E:\wasla-rag-notes\probes-p3\`, then remove with the worktrees |

**Other stages' infrastructure found at the start, not touched:** worktrees `E:\wasla-campaign-features`, `-campaign-media`, `-campaigns-audit`, `-campaigns-remediation`, `-cpf-base`, `-cpf-lane`, `-cpf-lane2`, `-cpf-mut`, `-followups-realtime`, `-frm-report`, `-fur-base`, `-fur-lane`, `-fur-lane2`, `-fur-mut`, `-ntf-audit`. The developer stack and `E:\wasla` with its untracked documents were not touched.

## 31. Score (after each Part, per dimension)

A **self-score** by these stages, on the scale Part 1 introduced; the review gave no number. Every figure is a self-assessment from these stages, **not an independent measurement**.

| Area | Max | Base `7cdf863` | Part 1 | Part 2 | **Part 3** | Deductions now |
| --- | --- | --- | --- | --- | --- | --- |
| Extraction and text fidelity | 8 | 3 | 6 | 6 | 6 | RV-04 repair only by re-upload (−1), RR-13 Word layout (−1) |
| Unicode and Arabic normalisation | 8 | 1 | 8 | 8 | 8 | — |
| Chunking | 10 | 3 | 9 | 9 | **10** | — (catalogue rows, FAQ pairs in every format) |
| Embedding input and space handling | 5 | 4 | 4 | 4.5 | **5** | — (shadow switch; the direct-change hazard is detected, RR-32) |
| Document lifecycle | 12 | 3 | 12 | 12 | 12 | — |
| Dedupe and identity | 5 | 1 | 5 | 5 | 5 | — |
| Ranking (hybrid, dedupe, merge, re-rank) | 8 | 3 | 4 | 7 | 7 | re-rank unmeasured on real traffic (−0.5); word-only questions off the exact-word channel by default / RR-25 (−0.5) |
| Query formulation | 5 | 2 | 2.5 | 4.5 | 4.5 | real-model adherence unmeasured (−0.5) |
| Scope and filters | 4 | 1 | 2 | 4 | 4 | — |
| Context assembly and budget | 5 | 2 | 2.5 | 5 | 5 | — |
| Tool loop and answer grounding | 7 | 3 | 5 | 6 | 6 | grounding FP only on synthetic replies (−0.5), RR-23 (−0.5) |
| Tenant isolation | 8 | 7 | 8 | 8 | 8 | — (RLS deferred by a stop condition: no deduction) |
| Re-index and operations | 3 | 1 | 3 | 2.5 | 2.5 | RR-17 needs an operator procedure (−0.5) |
| Observability | 3 | 1 | 3 | 3 | 3 | — |
| Testing and evaluation | 5 | 2 | 4.5 | 4.5 | 4.5 | semantic eval external (−0.5) |
| Documentation truth | 2 | 1 | 1.5 | 2 | 2 | — |
| Real-provider evidence | 2 | 0 | 0 | 0 | 0 | not authorised |
| **Total** | **100** | **38** | **80** | **91** | **92.5** | |

Part 3 earned +1.5 (chunking +1, space handling +0.5) and lost nothing new. Its own open items are RR-30 (dev-only), RR-25 (already deducted under Ranking) and RR-32 (detected and documented), and none is deducted twice. There is no deduction for DEFERRED PRODUCT DECISION, LATER PRODUCT, SCALE-TRIGGERED or CLOSED BY DESIGN items (RLS, OCR, result cache, partitioning).

## 32. Part 3 Verdict and Final Verdict for the Review

### Runtime matrix

| Scenario | Before (review) | Before (this Part, `b9c91c2`) | After | Verdict |
| --- | --- | --- | --- | --- |
| Embedding-model change | every document hidden | P-R17: 1 → 0 passages, "no information" | shadow switch: 0 empty of 512 step searches and 98 drain searches; a direct change still hides and is alerted | CLOSED |
| Flip with a failed shadow | n/a | no switch | refused (exit 1), the blocked document listed; flips after `--retry-blocked` | CLOSED |
| CSV catalogue | not accepted | refused (`not a valid DocumentSource`) | one row per chunk; 119 / 120 rows retrieved as the exact row; a replaced row never served | CLOSED |
| XLSX / DOCX / HTML | not accepted | refused | accepted, parsed in the sandbox child, FAQ pairs whole (29 / 30) | CLOSED |
| Zip bomb / entity expansion / formula cell | n/a | n/a | refused in the child in about 0.15 s; formula stored as its text, value never read | CLOSED |
| Multipart upload | base64 JSON only | JSON only | both; multipart stops reading at its limit | CLOSED |
| 400,000-character document time to ready | sequential batches | 6.74 s, 7 calls | 5.04 s, 7 calls, 3 batches per wave under one renewal | CLOSED |
| Same query 20 times | 20 embedding requests | 20 | 1 | CLOSED |
| Same text, two workspaces, cache | n/a | n/a | 2 entries, 2 transport requests, 0 shared | CLOSED |
| Mid-size workspace search | exact scan ~42 ms (docs/RAG.md) | 12,000 chunks ≈ 80–86 / 92–106 ms (p50 / p95, hybrid) | unchanged within noise; HNSW chosen at 12,000 | CLOSED (no regression; RR-25 residual) |
| Recall after a mass re-index | lowered until autovacuum | 0.942 | 0.946 before and after vacuum, with 22,800 dead tuples | CLOSED locally; production EXTERNAL |
| Query without a tenant predicate | returns rows | 302 rows of 2 workspaces | deferred: RLS needs a second database role (ADR-185) | DEFERRED with design |
| Unanswered questions | never learned | no record | gap report, off by default, model's query only, 30 days, admin-only | DECISION APPLIED |
| Duplicate / unreachable / stale / bidi | no gauge | duplicate and unreachable only | all gauged, with alerts and promtool cases | CLOSED |
| Cross-tenant, every new path | n/a | n/a | 0 foreign rows: formats and replacement, switch, rebuild, cache, gaps, multipart; predicate mutants killed | CLOSED |

### Against the conditions (§43 of the prompt)

- **RV-11:** never empty at any coverage step; a failed shadow blocks the flip; M-R65 (the prompt's M-R54), M-R55 and M-R56 killed ✓ (§7, §20).
- **Formats:** every new format goes through generation, dedupe and replacement; hostile fixtures refused in the sandbox; M-R43, M-R57, M-R58 and M-R59 killed ✓ (§6).
- **Cache:** tenant- and space-scoped, never serves content; M-R39 killed ✓ (§9).
- **Concurrency:** the claim is renewed and nothing is held across a provider call; M-R60 killed ✓ (§8).
- **Gap report:** off by default, tenant-scoped, retention and purge proved; M-R61, M-R62 and M-R63 killed ✓ (§10).
- **RLS:** deferred, with the blocker and the design ✓ (§12).
- The review's "done well" guarantees and the matrices of Parts 1 and 2 are unchanged ✓ (§15).
- All meaningful mutants are killed ✓ (§20).
- Both lanes fully green at `8afa99e`: model-built 7,876 passed and 0 failed, migration-built 7,880 passed and 0 failed, the date bomb included; reported separately ✓ (§22, §23).
- RR-01 .. RR-14 are 0 ✓ (§16).
- No real provider was contacted, and nothing was pushed, merged or deployed ✓.

What remains open is exactly the product decisions, later-product items, scale triggers, external verifications and external actions of §28–§29.

**RAG PART 3 (NEW FORMATS, SCALE & OPERATIONS) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

**RAG SYSTEM FINDINGS CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

The review's verdict is no better than the worst of the three Parts, and each Part is CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION.

### The final question

A workspace uploads its catalogue as an Excel file and replaces it a week later. The operator then starts an embedding-model switch while customers keep asking, in Arabic, about one product's price. Can Wasla prove the following?

- **Every search during the switch found the current row.** The rehearsal searched every document of both workspaces by its own text at 0 %, ~50 %, ~97 % and 100 % coverage, after a refused flip, after the blocked shadow was rebuilt and after both retirements. That was 512 searches, plus 98 more while the shadows drained, and none was empty or doubled. A workspace is queried in one space until the transaction that flips it, and only once every ready document has a shadow (M-R55, M-R56, M-R65).
- **No search was empty for a document that exists.** Proven as above. What remains is the *direct* setting change, which still hides documents; it is detected (RR-09, `KnowledgeStaleEmbeddings`) and documented as the thing not to do.
- **The old row was never served after the replacement.** A replaced XLSX serves only its new price once the swap commits, in every format (`test_a_replaced_document_of_every_new_format_serves_only_its_new_version`, M-R12). A replacement made mid-switch retires the stale shadow, and only the new text flips. Each row is its own chunk, never glued to its neighbour (RR-14, RR-27).
- **Nothing from another workspace was in the context or any cache.** Every query path, the switch's coverage and rebuild included, carries the workspace predicate, and each has an isolation fixture and a predicate mutant that fails without it (M-R32, M-R39, M-R61, M-R66, M-R67). The cache key carries the workspace and the space, and the cache holds only the question's vector, never a passage.

What Wasla cannot yet prove is how the *real* embedding model ranks an Arabic price question against that row. That is the real-provider evaluation, which was not run.
