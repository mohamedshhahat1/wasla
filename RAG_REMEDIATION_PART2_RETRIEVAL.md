# RAG Remediation — Part 2 of 3: Retrieval Quality & Answer Correctness

Remediation of `RAG_SYSTEM_REVIEW.md`, Phases 9 .. 10 of its plan plus this Part's Phases 13 .. 18, on top of Part 1 (`RAG_REMEDIATION_PART1_LIFECYCLE.md`, `131bfa4`). Branch `rag-remediation-p2-retrieval-20261029`, worktree `E:\wasla-rag-p2`. **Nothing was pushed, merged or deployed. No real provider was contacted.**

Every retrieval number below comes from **fake embeddings** (`tests/fake_embeddings.py`, a hashed bag of words) or from `IdentifierBlindEmbeddings` (the same bag of words with every digit-bearing token removed - how an embedding model treats an identifier it has never seen). They measure **structure** - whether the right passage is findable, delivered whole, once and current - never semantic quality. Semantic quality with the real embedding model is external verification (§31).

---

## 1. Executive Summary

- **A search delivers what the tool promises.** The budget is estimated tokens (`3,300 = 6 x 540 + 60`), `max_results` is 1 to 6 and every advertised count is delivered for English, Arabic and multi-line passages of the largest size the chunker makes (before: `max_results: 10` delivered 4 in English, 7 in Arabic; overall 1,821 of 2,610 kept passages reached the model). A passage that does not fit is skipped, not the end of packing (RV-19). The model reads source numbers, not UUIDs.
- **Exact identifiers are found.** A second, exact-word channel (`search_text`, `search_tsv`, GIN; lexemes tokenised in Python because PostgreSQL's `'simple'` parser splits `BK-35` and `4,500`) runs beside the vector query under **one predicate builder**, fused by RRF with an identifier keep rule. A question that names an identifier asks that channel for its identifiers only. With an identifier-blind model, identifier questions answered at the real threshold: **24 → 57 of 57** (SKUs in a CSV of near-identical rows 0 → 18 of 18, Latin brand inside Arabic 0 → 3 of 3, model numbers 24 → 36 of 36). The exact-word channel alone returns the answer for 257 of 261 questions. Word-only questions use it only with `RAG_HYBRID_ALL_QUESTIONS=true` (off: +97 ms p95 at 12,000 chunks).
- **Answers that span chunks arrive whole.** Neighbours of one section are merged with the chunker's overlap removed and the top two passages are joined with ordinal ± 1: two-chunk answers at `max_results=1` 9 → 22 of 24. Near-repeats are dropped unless they carry a new number; a document's fourth passage is deferred, not dropped.
- **Follow-ups and the other language.** The tool now asks for a standalone query and an optional `alternate_query` in the documents' language, embedded in one batched request: scripted follow-ups 0 → 10 of 10, cross-lingual 0 → 6 of 8. Rank-1 answers 193 → 236 of 261.
- **Numbers are checked.** Prices and percentages in a reply are checked against the turn's passages, the customer's words and the agent's instructions (`observe` by default; `correct` and `strict` tested end to end). On 304 prices in correct replies built from the corpus (both digit scripts): **0 false positives**; 113 of 113 changed prices caught.
- **Admins can see the working.** `POST /knowledge/search` shows what the agent would retrieve, every candidate and what became of it, and the exact JSON the model reads. Per-agent knowledge scope, per-workspace switches, confidence tiers (off, inert until calibrated), optional re-rank, pre-retrieval and parallel sentiment (off) are built, tested, and documented with how to switch them on.
- **Latency, measured honestly** (25,200-chunk drill): the exact-word channel adds ≤ 8 ms p95 to this Part's search; the whole search is +9 to +25 ms p95 over the base, over the +15 ms target at 200 and 12,000 chunks because of always-on fusion, repeat checks and two neighbour lookups (RR-25, §8).
- **Part 1's guarantees hold**: 7900 only after a replacement, through the hybrid path too; an expired document never retrieved, through the lexical channel too; tenant isolation for every new path; every Part-2 mutant and Part 1's re-applied isolation and stale-version mutants killed (one equivalent, one redundant guard, each proved).

**Verdict: RAG PART 2 (RETRIEVAL QUALITY & ANSWER CORRECTNESS) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION** (§34).

## 2. Starting Repository State (and Part 1's state confirmed)

| Item | Value |
| --- | --- |
| Part 1 verdict | CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION — the precondition holds |
| `RAG_P2_BASE_HEAD` | `131bfa4` (Part 1's report commit, branch `rag-remediation-p1-lifecycle-20261029`; Part 1 is not merged) |
| `origin/worktree-billing-google-auth` | `7cdf863`, 0 ahead / 0 behind `E:\wasla`; Part 1 is 29 commits ahead of it; origin did not advance |
| Branch / worktree | `rag-remediation-p2-retrieval-20261029` / `E:\wasla-rag-p2` (from `131bfa4`) |
| Final code commit | `eda48d6` (this report is the commit after it) |
| Alembic head at base | `0131` (single); now `0134` (single) |
| ADRs | last at base ADR-167; this Part ADR-168 .. ADR-178 (ADR-178 amends ADR-024) |
| PostgreSQL / pgvector | 16.15 / **0.8.6** (`wasla-rg2-pg`, the production-pinned digest) |
| Redis / object store | `redis:7-alpine` 7.4.11 (`wasla-rg2-redis`, DBs 3-7) / MinIO (`wasla-rg2-minio`, bucket `wasla-rg2-media`) |
| Python / Docker | 3.12.7 / Engine 29.8.0, Compose v5.5.1 |
| `app.__file__` | `E:\wasla-rag-p2\app\__init__.py` in every lane; `E:\wasla-rg2-base\…`, `E:\wasla-rg2-mut\…`, `E:\wasla-rg2-eval\…` in theirs |
| Working tree at start | clean apart from the copied probes under `audit/` (untracked) |
| `E:\wasla` | its untracked documents untouched, not staged, not moved |

One tooling note recorded for the next stage: a probe script run as `python audit/rag/x.py` imports `app` from `E:\wasla` (the editable install) — every probe here sets `PYTHONPATH` to its worktree and prints `app.__file__`.

## 3. Part 2 Findings Revalidation

At `131bfa4`, before any change (the "before" column of §39):

| ID | Reproduced? | Evidence |
| --- | --- | --- |
| RV-15 | Yes | P-R06: `max_results` 4/6/8/10 with the chunker's largest pieces → EN 4/4/4/4, AR 4/6/7/7, multi-line 4/6/7/7; eval: 1,821 of 2,610 kept passages delivered at `max_results=10` |
| RV-19 | Yes | large, larger, small passage → only the first kept |
| RV-21 | Yes | `SearchRequest`, `SearchResponse`, `PassageRead` referenced by no route (grep: 0) |
| RV-26 | telemetry closed in Part 1 | distance histogram and drops recorded; calibration still external |
| RC-4 | Yes | identifier-blind workspace: SKU 0/18, Latin-in-Arabic 0/3, model numbers 24/36 at 0.75, vector only |
| RC-5 | Yes | scripted follow-ups as typed 0/10; Arabic questions about English lists 0/8 (no second query exists) |
| FX-3.13 | Yes | nothing inspects reply numbers (no code path) |
| FX-3.11 | Yes | two-chunk answers at `max_results=1`: 9/24 |
| P-R27 | measured | knowledge turn: 2 rounds; round 2 input 3,375 characters, 2,957 of them the tool output |
| RC-6 | Yes | `agent_service._refuse_unsupported_config` refuses every grant config |

Nothing was NOT REPRODUCED. Two findings of the harness itself: Part 1's corpus has no follow-up or cross-lingual questions despite its summary (RR-15), so `tests/fixtures/rag/eval/part2/contract.jsonl` adds 10 scripted follow-ups and 8 cross-lingual cases over the existing documents; and my first two-chunk measure compared JSON-escaped text and undercounted (fixed before any column was recorded).

## 4. Locked Remediation Decisions

RQ-12 .. RQ-20 applied as written, with these recorded deviations or interpretations:

- **RQ-12**: budget 3,300 estimated tokens, `MAX_TOP_K = 6`, per-source ceiling 540 (arithmetic in §6).
- **RQ-15**: `search_tsv` is built from Python lexemes with `array_to_tsvector`, not `to_tsvector('simple', …)`: the parser splits `bk-35` into `bk`/`-35` and `4,500` into `4`/`500` (RR-16, reproduced on the container). The keep rule's identifiers are whole tokens as typed (the parts of `bk-35` are indexed, but a bare `35` is not an identifier).
- **RQ-16**: "80 % token overlap" is measured on 3-word runs, and a near repeat is dropped only if it adds no number: as bags of words, two rows of a price table looked like repeats and the evaluation lost answers (§9). The per-document cap defers rather than drops (RR-19).
- **RQ-19**: the agent's instructions are a third grounding source, beside the passages and the customer's words — without it every fee a business writes into its prompt is a false positive. A `strict` turn ends `empty_response` with the handoff reason `AI_UNGROUNDED_NUMBER` (no new turn-outcome label: a native enum migration).
- **FX-3.16**: `prompt_cache_key` is **not** sent — `ResponsesClient` does not support it, and the prompt says not to guess the API (EXTERNAL VERIFICATION).

## 5. Product Decisions Applied

| Decision | Setting (default) | ADR |
| --- | --- | --- |
| Token budget, honest top-k, compact sources | constants | 168 |
| Confidence tiers | `RAG_CONFIDENCE_TIERS_ENABLED=false`, `RAG_CONFIDENCE_TIERS={}` | 169 |
| Contextual embedding | always on (input change, no schema bump) | 170 |
| Hybrid retrieval, RRF k=60, keep rule | `RAG_HYBRID_ENABLED=true` | 171 |
| Dedupe, merge, deferred cap, neighbours | always on | 172 |
| LLM re-rank, 1.5 s timeout | `RAG_RERANK_ENABLED=false`, `RAG_RERANK_MODEL=` | 172 |
| Query contract | tool schema | 173 |
| Per-agent scope | grant `config.knowledge_base_ids` | 174 |
| Admin search preview | route | 175 |
| Workspace overrides of the switches | `PATCH /knowledge/settings` | 176 |
| Number grounding | `RAG_NUMBER_GROUNDING=observe` | 177 |
| Pre-retrieval / parallel sentiment | `RAG_PRERETRIEVAL_ENABLED=false` / `RAG_PARALLEL_SENTIMENT_ENABLED=false` | 178 |
| Passage margin | `RAG_PASSAGE_MARGIN=` (inert) | 168/172 |

## 6. Context Budget and Top-k (RV-15, RV-19)

- **Arithmetic.** Largest chunk = `MAX_CHUNK_TOKENS + CHUNK_OVERLAP_TOKENS + MIN_CHUNK_TOKENS` = 250 + 40 + 10 = **300** estimated tokens. Worst single source, measured: that chunk as newline-heavy ASCII (escaping costs most), title and section at their 120-character clip in Arabic, `updated_at` and `match` → **521**; ceiling **540**. Envelope ≤ 60. `MAX_CONTEXT_TOKENS = 6 x 540 + 60 = 3,300`. Typical six-passage context ≈ 2,000 tokens.
- **Delivered per `max_results`** (P-R06 ported as `tests/unit/test_retrieval_budget.py`, largest corpus chunks): 1..6 requested → 1..6 delivered for EN, AR and multi-line (18 cases). Over the eval corpus at `max_results=6`: 1,161 of 1,161 kept passages delivered (before: 1,821 of 2,610 at 10).
- **Skip-and-continue**: rank-1 (≈2,500 tokens), rank-2 too large to fit beside it, rank-3 small → ranks 1 and 3 delivered (killed M-R30). A single oversized legacy passage is still shortened, not dropped.
- **Compact sources**: `{source, title, section, updated_at, [match], content}`; no `document_id`, no ordinal. P-R27: round-2 input 3,375 → **2,952** characters for the same four passages (tool output 2,957 → 2,567, −13 %).

## 7. Contextual Embedding and the Rebuild

Embedding input `for_matching("{title} › {sections}\n\n{chunk}")`; a meaningless title (empty, "untitled", `scan_003.pdf`, `IMG_2041.pdf`, "Document (2)") is replaced by a meaningful filename stem or left out; a heading the chunk opens with is not repeated (found by `test_the_nearest_passage_comes_first`, whose distance sat at exactly 0.750 and moved to 0.776 when the heading was doubled). Schema version unchanged.

**The rebuild rehearsal** (`audit/rag/p2_rehearsal_seed.py` / `p2_rehearsal_run.py`): `wasla_rg2_mig` migration-built at `0131` and populated **by `131bfa4`'s code** (worktree `E:\wasla-rg2-base`): 2 workspaces, 3 knowledge bases, 37 documents (12 PDFs, 17 text, 6 Markdown, a 60-row SKU catalogue, a price list), 43 chunks; generations active 36, pending 2 (a re-index and **a replacement in flight**), processing 1 (expired lease), failed 1, superseded 1. Then this branch:

| Step | Result |
| --- | --- |
| `alembic upgrade head` (0131 → 0134) | 3.0 s; row count + MD5 of every original column of 5 tables identical |
| RR-12 before | 43 (every chunk) |
| Part 1 back-fills | examined 37, updated 0, collisions 0 (Part 1's code had stored normalised text) |
| `reindex-all --rate 20` | 36 requested, 1 skipped (failed) and listed |
| drain + one recovery sweep | 36 jobs in 4.95 s (**7.3 documents/s**); the expired lease re-queued and published; **36 provider calls, 1.0 per document**; 43/43 embedding inputs carried the label |
| served throughout (hybrid **off** during the rebuild, as DEPLOYMENT now prescribes) | **true** — 35 served documents found by their own text before, between every request, every 50 ms of the drain, after; no gap, no search error with old and new vectors mixed |
| served after, hybrid on | 35 / 35 |
| RR-01 .. RR-08, RR-10, RR-11, RR-12 after | **all 0** |
| schema versions of active generations | `{1}` (unchanged) |
| the replacement in flight | published: only `7900 EGP` served |
| SKU after the rebuild | `WS-0035` found with channels `lexical, vector` |
| downgrade to 0131 afterwards | refused: `Refusing to downgrade 0132: it would discard the lexical text of 43 chunks.` |

**RR-17 (found by the rehearsal).** The same rebuild with **hybrid on** kept every document served (retrievable through `served_predicates`), but 7 of 35 documents briefly fell out of a top-6 search for their own first chunk during the drain: a chunk not yet rebuilt is in the vector list only, its rebuilt near-identical siblings in both, and RRF ranks them above it. Remedy, documented in DEPLOYMENT.md and rehearsed: run the first `reindex-all` with `RAG_HYBRID_ENABLED=false` and switch it on at RR-12 = 0 (0 gaps). The `request_seconds` (36.8 s) is dominated by the full served-set check run at every pause, not by the command.

## 8. Hybrid Retrieval (RC-4)

- **One predicate builder.** `DocumentChunkRepository.served_predicates` — tenant on chunk, generation and document; generation `active`; space (provider, model, dimensions, schema version); non-null embedding; validity window; scope — used by `search`, `lexical_search` and `neighbours`. M-R32 (tenant predicates filtered out of the lexical query only), M-R33 (active state), M-R46 (validity) are each killed by a test where only that predicate stops the row.
- **Channels per search**: vector top-20 and lexical top-20 per query (two queries with `alternate_query`); RRF k = 60; candidates per channel in `wasla_rag_retrieval_candidates_total{channel}`.
- **Keep rule** (unit-tested; integration: a far lexical hit on "walnut" is dropped, killed M-R48): kept passages beyond 0.75 in the tests were kept **for an identifier** — `BK-35` (distance 1.0, a digit token) and `Ultrabook` (Latin word inside an Arabic question).
- **Exact-identifier questions** at 0.75, identifier-blind model, 57 questions (36 English "model N", 18 CSV SKUs, 3 Latin-in-Arabic): vector only 24 (base) / 32 (this Part), hybrid **57 / 57** at the final commit. Before `f446b01` the exact-word query was an OR of every word of the question and SKUs stayed at 3–4/18: the CSV chunks hold ~34 near-identical rows and "price" pulled other documents' rows into both lists (RR-18). Querying only the identifiers of an identifier question puts the SKU's chunk first in the lexical list and fixes it.
- **Which questions use the channel.** A question naming an identifier queries its identifiers; a question without one uses the vector channel alone unless `RAG_HYBRID_ALL_QUESTIONS=true`. The cost of that switch is the stop condition of §21 of the prompt: an OR of common words matches most chunks of a large workspace.
- **Latency** (P-R26, `audit/rag/p_r26_latency.py`): a 25,200-chunk drill written straight into a migration-built database at head (one workspace of 12,000 chunks, one of 6,000, 24 of 200; templated price rows with SKUs; HNSW and GIN maintained), 60 searches per arm, fake embeddings (instant), so the time is SQL plus ranking. "Base" is `131bfa4`'s code on the same database.

| Workspace | Question | Base p50 / p95 | Part 2 vector only | Part 2 hybrid (default) | Part 2 hybrid, every question (as with `RAG_HYBRID_ALL_QUESTIONS=true`) | the same + re-rank (fake judge) |
| --- | --- | --- | --- | --- | --- | --- |
| 12,000 chunks | SKU | 66.1 / 78.8 ms | 81.6 / 101.7 | 85.9 / **98.3** | 85.5 / 91.8 | 87.8 / 94.6 |
| 12,000 chunks | words only | 64.7 / 73.1 | 80.6 / 84.3 | 81.3 / **95.0** | 155.8 / **170.2** | 157.1 / 167.9 |
| 6,000 chunks | SKU | 43.7 / 52.9 | 59.2 / 75.5 | 59.6 / **61.5** | 60.1 / 64.3 | 62.8 / 71.0 |
| 6,000 chunks | words only | 41.2 / 47.6 | 56.5 / 60.9 | 55.6 / **57.5** | 99.4 / 102.8 | 103.1 / 114.5 |
| 200 chunks | SKU | 12.7 / 14.9 | 29.2 / 35.1 | 33.1 / **35.1** | 33.6 / 55.8 | 35.3 / 40.9 |
| 200 chunks | words only | 12.4 / 13.3 | 28.5 / 30.2 | 30.2 / **38.0** | 42.9 / 45.6 | 44.4 / 71.8 |

  The exact-word channel's own increment over this Part's vector-only path is ≤ 8 ms p95 in every row. Against the base, the default search is +8.6 / +9.9 ms p95 at 6,000 chunks (within +15) and +19.5 to +24.7 ms at 12,000 and 200 chunks (over). The overhead is this Part's always-on work - 20 candidates fused instead of k, near-repeat shingles per candidate, and two neighbour lookups (two round trips) - not the lexical channel. **RR-25**, ACCEPTED RESIDUAL: < 100 ms p95 absolute in every row, against a model round of a second or more; batching the neighbour lookups into one round trip is the first optimisation (FX-4.7, Part 3). Re-rank with an instant fake judge costs ~2-3 ms of Python; a real judge adds its round trip up to the 1.5 s timeout (EXTERNAL VERIFICATION).

## 9. Dedupe, Merge, Re-rank, Neighbours

- **Repeats**: duplicate passages per search 0.0 over the corpus (unchanged from Part 1, where exact copies were already refused at upload). The first version measured near-repeats on bags of words and lowered three numbers (top-5 253 → 250, per-producer PDF answers, two-chunk); diagnosed by switching each rule off (`diag-9c-nocap`, `diag-9c-nonear`), then fixed: 3-word runs, and a near repeat carrying a new number is kept. After the fix every structural metric is at or above Part 1's except top-5 252 vs 253 after 9b (−1, the cost of dropping genuine near copies; rank-1 rose 203 → 208).
- **Deferred cap (RR-19)**: the first, hard cap of three passages per document made a one-document workspace receive three chunks whatever it asked for — found by `test_the_clamp_actually_bites_on_a_full_knowledge_base`; now a document's fourth passage waits behind other documents' (killed M-R49).
- **Merge + neighbours**: two-chunk answers at `max_results=1` 9 → **22/24**; at `max_results=5` 24/24 before and after (both chunks already arrived as separate passages). Neighbours only from the same served generation and section (killed M-R34), only while the context fits.
- **Re-rank (off)**: scripted provider at the transport — scores reorder (the model receives the re-ranked order); a **0.3 s timeout with a 5 s provider stops at the wall clock** (< 2 s measured) and the fused order is what the model receives (killed M-R37); a `400`, non-JSON, missing, duplicated, out-of-range and unknown sources each keep the fused order. Default production timeout 1.5 s. Metered as `knowledge_rerank`.

## 10. Query Contract (RC-5)

Tool schema (checked against the scripted provider; the real-provider check is external):

> `query`: "A short, standalone search query. Resolve references from the conversation ("the premium one" becomes the product the customer named before), name the specific product, service or policy, and leave out greetings. In the customer's language."
>
> `alternate_query` (optional): "the same query in the language the company's documents are written in, when that differs from the customer's - for example in English for an Arabic question about an English price list."

Both are embedded in **one** request (asserted on the bytes at the transport) and each searched by both channels. Structural results (scripted queries): follow-ups as typed 0–3/10 → standalone 10/10; cross-lingual 0–2/8 → 5–6/8 with `alternate_query` (the three misses are sentence-level spans the bag of words ranks below other warranty and FAQ passages). Killed M-R51.

## 11. Per-Agent Scope, Tiers, Search Preview (RC-6, RV-21)

- **Scope**: validated at grant time (empty list, unknown id, another workspace's id → the same `422`; other keys refused; every other tool's config still refused). Through the real round loop, the "HR" passage is the nearest one unscoped, and a "Sales"-scoped agent never receives it (killed M-R35). A hand-written non-list scope reads as "nothing".
- **Tiers**: inert unless enabled **and** calibrated for the current model (killed M-R53); with both, `match` appears in each source.
- **Preview**: passages with distance, channels, ordinals; decisions per candidate (`kept`, `threshold`, `duplicate`, `margin`, `top_k`); per-channel counts; drops; re-rank outcome; duplicate-title warning; the exact model JSON (source numbers, no ids); metered (one `rag_query`); a member gets 403 (killed M-R52); isolation fixture passes.

## 12. Number Grounding (FX-3.13)

`audit/rag/p_r24_grounding.py` over the eval corpus (replies built from the labelled spans, the span as the only source):

| | Count |
| --- | --- |
| replies built | 486 (243 as written, 243 in Arabic-Indic digits) |
| prices / percentages extracted and checked | 304 (152 + 152) |
| grounded | 304 |
| flagged ungrounded (false positives) | **0** — rate **0.0 %** (target < 2 %) |
| replies with one price changed by +100 | 113 |
| caught | 113 |

Synthetic replies quote spans more literally than a model would; the real false-positive rate is measured in `observe` mode in production (`AgentRepliesQuoteUngroundedNumbers`). End to end through the worker: `observe` sends `8,400 EGP` and counts it; `correct` sends the grounded correction after a corrective round with `tool_choice: "none"`; `strict` withholds it, sends the fallback line and hands over with `AI_UNGROUNDED_NUMBER`. Arithmetic on grounded numbers (150 m × 4,500 = 675,000, + 50 delivery) is grounded.

## 13. Pre-Retrieval, Parallel Sentiment, Prompt Prefix (FX-3.14 .. FX-3.16)

- **Pre-retrieval (off)**: with tiers calibrated in a test, a knowledge turn takes **1 round** instead of 2 (P-R27: 2,791 input characters), the search is a `tool_executions` row (`search_knowledge`, round 1, ordinal 0, succeeded, its provider call id the one in the input), and the passages sit only inside `function_call_output`. With tiers off: no row, no embedding request, no injected call. Killed M-R44.
- **Parallel sentiment (off)**: the classifier and round one provably overlap (each waits for the other to arrive); **no `AsyncSession` method is called while both are in flight** (instrumented: 0 inside the window, > 0 outside); an escalation arriving after round one answered sends nothing, runs no tool (the round asked for `record_lead_details`), hands over, ends `escalated` (killed M-R45). AI-04 and CRM-02 tests unchanged.
- **Prompt prefix**: instructions → tools → history, rounds chronological (Part 1); compact sources (§6); the passage margin exists, inert; `prompt_cache_key` not sent (§4). The tool descriptions grew (823 → 1,481 characters of tools per round) — stable across rounds, so a cacheable prefix.

## 14. Observability and Alerts

New, tenant-free: `wasla_rag_retrieval_candidates_total{channel}`, `wasla_rag_search_seconds{channel}`, `wasla_rag_context_tokens` (beside `wasla_rag_context_characters`, kept so dashboards do not blank), `wasla_rag_confidence_tier_total{tier}`, `wasla_rag_rerank_total{outcome}`, `wasla_rag_number_grounding_total{outcome}`, `wasla_rag_preretrieval_total{outcome}`; drop reasons now `threshold | duplicate | validity | budget | scope | margin | document_cap`; stage `rerank`. Alerts `KnowledgeRerankFailing` (warning) and `AgentRepliesQuoteUngroundedNumbers` (info), each with a promtool fire-and-clear test and a RUNBOOK entry. Logs carry counts and booleans (`alternate_query: true`), never queries.

## 15. Documentation Drift and RUNBOOK (RV-24)

| §8 row | Now |
| --- | --- |
| "A search returns between 1 and 10 passages" | the doc says 1 to 6, and the code delivers each (RV-15) |
| "Embeddings of unrelated text sit around 0.8 and above" | removed from the code comment; docs say the 0.75 cut-off is **not yet measured** for the production model, with the calibration step (RV-26) |
| knowledge bases "point different agents at different sets" | true for a scoped grant; docs and the model docstring say only the default agent answers today (RQ-18) |

With Part 1's five rows, **the review's §8 table is empty.** RUNBOOK: the search preview under "answers ignore a document", "Search is slow" (per channel, GIN validity, `ef_search` / `max_scan_tuples` with the scale trigger — RV-25), "switching re-rank, tiers, grounding, pre-retrieval, parallel sentiment on", and the two alerts. Updated: `docs/RAG.md`, `AI_AGENTS.md`, `API.md` (255 operations), `OBSERVABILITY.md`, `SECURITY.md`, `DEPLOYMENT.md`, `ARCHITECTURE.md`, `README.md` (0001–0134), `.env.example`, `DECISIONS.md` ADR-168 .. ADR-178.

## 16. Re-Verification

In both lanes (§23, §24): cross-tenant property tests and the isolation gate (now with lexical, fused, re-ranked, expanded, preview, scoped and alternate-query fixtures); re-ingesting twice does not double chunks; a re-index serves the old generation until the swap; a stale claim cannot publish; a delete during an embedding call commits at once; permanent → failed after one attempt, transient → `retry_exhausted` after five; a suspended workspace spends nothing; the PDF sandbox refuses and never truncates; a NaN / wrong-width vector is a failed tool call; a model change excludes old vectors; retrieved text only in `function_call_output` (also through pre-retrieval). Part 1's matrix rows a ranking change could disturb: **only 7900 after a replacement** through the hybrid path (`test_an_updated_document_serves_one_version_through_the_hybrid_search`, which also kills M-R12); the cross-KB legacy copy served once; an expired document never retrieved through the lexical channel; the three-search turn still ends in a reply.

## 17. Database Invariant Sweep

`scripts/rag_invariants.py` adds **RR-12** (an active chunk without `search_text` / `search_tsv`), proved non-vacuous by an injected violation in a rolled-back transaction. On the rehearsed database after the rebuild: RR-01 .. RR-08, RR-10, RR-11, **RR-12 = 0** (RR-12 was 43 before).

## 18. Migrations and Alembic Gates

`0132` `document_chunks.search_text`, `search_tsv` (nullable, catalogue-only; a plain `tsvector`, not a `STORED` generated column, which would rewrite the table on PostgreSQL 16); `0133` GIN `ix_document_chunks_search_tsv` built `CONCURRENTLY` (an invalid leftover dropped first), dropped inside the run's transaction on downgrade (RR-14's rule); `0134` `tenants.knowledge_settings` (JSONB) and the `knowledge_settings_updated` audit label (`ADD VALUE` in autocommit; labels cannot be dropped). The neighbour lookup uses the existing `uq_document_chunks_tenant_id_generation_id_ordinal`.

| Gate | Result |
| --- | --- |
| `alembic heads` | `0134 (head)`, single |
| `alembic check` | "No new upgrade operations detected." |
| fresh `0001 → head` | 8.3 s |
| catalog | 0 `NOT VALID` constraints, 0 invalid indexes |
| `db_preflight verify` | ok |
| populated base head (0131) → head → 0131 → head, on a template copy of the seed (generations pending, processing, active, superseded, failed; a replacement in flight; chunks without lexical text) | exit 0 each step; row count + MD5 of every original column of 5 tables identical after each step |
| refusals (`test_knowledge_migrations.py`) | 0134 refuses while a workspace has settings, then while an audit row records one; 0132 refuses while a chunk has lexical text; then 0131 and head again; Part 1's 0127 .. 0131 refusals unchanged |
| downgrade on the rebuilt database | refused at 0132 (43 chunks) |

## 19. API Compatibility

Additive (252 → 255 operations): `POST /knowledge/search`, `GET` and `PATCH /knowledge/settings`. `search_knowledge` schema: `query` re-described, optional `alternate_query`, `max_results` 1–6 (was 1–10: a model asking 7–10 now gets a validation message it can correct — the honest number). Grant `config` for `search_knowledge` accepts `knowledge_base_ids`. Tool output: source numbers instead of `document_id`/`chunk`; `match` only with tiers on. Stable refusals: scope config empty / unknown / foreign / other key → 422; preview or settings by a member → 403; unknown setting or value → 422; preview `top_k` > 6 → 422. Every existing route and field kept; `SearchRequest`/`SearchResponse`/`PassageRead` extended (they had no route).

## 20. Ported Probe Tests

| Probe | Test |
| --- | --- |
| P-R06 | `tests/unit/test_retrieval_budget.py` |
| P-R22 | `tests/integration/test_hybrid_retrieval.py` (+ `test_retrieval_ranking.py`) |
| P-R23 | `tests/integration/test_query_contract_and_scope.py` (the tool schema and the fused two-query search through the round loop) |
| P-R24 | `tests/unit/test_number_grounding.py` (incl. the < 2 % false-positive gate over the corpus) |
| P-R25 | `tests/integration/test_neighbour_expansion.py` |
| P-R27 | `tests/integration/test_answer_correctness_turns.py` (pre-retrieval in one round, parallel sentiment) |
| P-R19 / RV-21 | `tests/integration/test_knowledge_preview.py`, `test_knowledge_endpoints.py` |

All run in the existing CI jobs (unit; integration; migration-built integration); none is slow enough for a marker (the longest file, ~20 s). The probe scripts stay untracked under `audit/rag/` and are copied to `E:\wasla-rag-notes\probes-p2\`.

## 21. Mutation Matrix

Strict runner (`audit/rag/run_rag_mutations_p2.py` + `p2_mutants.py`, Part 1's protocol): exact anchors, named killers, a control pass first (**48 killer tests green**), every file restored and SHA-256 verified; in the disposable worktree `E:\wasla-rg2-mut` at `da0525f`.

| Mutant | Verdict | Killer |
| --- | --- | --- |
| M-R30 packing breaks at the first misfit | KILLED | `test_a_passage_that_does_not_fit_is_skipped_and_the_next_tried` |
| M-R31 MAX_TOP_K 10 on a 6-passage budget | KILLED | `test_the_budget_arithmetic_holds` |
| M-R32 lexical without tenant predicates | KILLED | `test_the_lexical_query_returns_nothing_foreign` |
| M-R33 lexical without active generation | KILLED | `test_a_superseded_generations_chunk_is_never_retrieved_by_its_exact_words` |
| M-R46 lexical without validity (vector keeps it) | KILLED | `test_an_expired_document_is_never_retrieved_by_its_exact_words` |
| M-R34 neighbours across generations | KILLED | `test_the_chunk_after_the_best_one_arrives_with_it` |
| M-R35 scope not applied | KILLED | `test_a_scoped_agent_never_receives_another_knowledge_bases_passages` |
| M-R37 re-rank timeout without fallback | KILLED | `test_a_re_rank_that_times_out_leaves_the_fused_order` |
| M-R38 no digit folding in the guard | **SURVIVED — equivalent** (below) | — |
| M-R38b guard reads ASCII digits only | KILLED | `test_a_number_in_arabic_indic_digits_is_the_same_number` |
| M-R44 pre-retrieval not in tool_executions | KILLED | `test_pre_retrieval_answers_in_one_round_and_is_a_recorded_execution` |
| M-R45 parallel escalation does not stop the turn | KILLED | `test_an_escalation_arriving_after_round_one_sends_nothing_and_runs_no_tool` |
| M-R47 RRF → raw score sum | KILLED | `test_fusion_is_by_rank_not_by_raw_score` |
| M-R48 keep rule admits every lexical hit | KILLED | `test_a_far_candidate_is_kept_only_for_a_lexical_identifier_hit` |
| M-R49 per-document cap removed | KILLED | `test_a_documents_fourth_passage_waits_behind_other_documents` |
| M-R50 contextual prefix omitted | KILLED | `test_indexing_embeds_the_labelled_chunk_and_stores_the_chunk_alone` |
| M-R51 alternate_query ignored | KILLED | `test_an_arabic_question_about_an_english_list_reaches_it_through_the_alternate_query` |
| M-R52 preview without the admin check | KILLED | `test_a_member_cannot_preview_a_search` |
| M-R53 tier cut-offs applied while disabled | KILLED | `test_tiers_are_off_unless_enabled_and_calibrated` |
| M-R12 claim reads `documents.content` | KILLED | `test_an_updated_document_serves_one_version_through_the_hybrid_search` |
| M-R18 cross-KB dedupe removed | KILLED | `test_the_same_text_in_another_knowledge_base_is_refused` |
| M-R19 dedupe lookup made global | KILLED | `test_source_keys_and_dedupe_look_only_inside_the_workspace` |
| M-R20 retrieval-time dedupe removed (exact + near) | KILLED | `test_a_legacy_copy_is_served_once` |
| M-R20b exact-hash check removed, near rule kept | **SURVIVED — redundant guard** (below) | — |
| M-R36 validity not applied (both channels) | KILLED | `test_a_document_outside_its_window_is_never_retrieved` |
| M-R54 identifier question asks for every word | KILLED | `test_a_question_naming_an_identifier_asks_the_exact_word_channel_for_it_alone` |

**24 killed, 2 survivors, 0 meaningful survivors** — run at `da0525f` (control: 48 killers green) and again in full at `f446b01` after the lexical-query change (control: 49 green): identical verdicts, M-R54 added.

- **M-R38, equivalent.** Removing `for_matching` from the guard changes nothing: Python's `\d` matches every Unicode decimal digit and `Decimal("٤٥٠٠") == 4500`, and the Arabic thousands and decimal separators are handled in `_value`. The regression the mutant meant — Arabic-Indic digits not read as the same number — is M-R38b (an ASCII-only digit class), killed.
- **M-R20b, redundant guard.** An exact copy has 100 % of its word runs in common and no number the kept passage lacks, so the near-repeat rule drops it too. Removing the whole safety net (M-R20) is killed. Kept for clarity and for speed (a hash comparison before the shingle test).

None of the never-acceptable mutants (M-R12, M-R19, M-R32, M-R33, M-R36, M-R46) survived. `git status` under `app/` in `E:\wasla-rg2-mut` (at `f446b01`): clean.

## 22. Test Non-Vacuity

- **Hybrid**: the SKU and the Latin brand are first shown **absent** from the vector channel alone (`policy=RetrievalPolicy(hybrid=False)`), and present with `channels` containing `lexical` and a distance past the cut-off.
- **Hybrid isolation**: the foreign workspace holds the same `7900` token and a marker only it has; the stale-version test seeds a superseded generation's chunk, the one row only the state predicate excludes.
- **Budget**: the counted sources are the decoded tool output; P-R27 measures the bytes handed to the fake Responses transport.
- **Re-rank**: the wall clock is asserted (< 2 s for a 0.3 s timeout against a 5 s provider) and the order compared is what the search returned to the tool.
- **Scope**: the unscoped search returns "Handbook" (HR) first; only the scope removes it.
- **Grounding**: what is asserted is the text that reached the fake Meta transport (`providers.sends`), not a helper's return.
- **Pre-retrieval**: the `tool_executions` row's provider call id equals the `call_id` in round one's input at the transport.
- **Parallel sentiment**: each HTTP call waits for the other to arrive (impossible serially); the session guard counts calls outside the window (> 0) and inside (0); the escalation is released only after round one answered.
- **Fails at the base or against its mutant**: every test closing a Part 2 finding is either written against an API that does not exist at `131bfa4` (`lexical_search`, `alternate_query`, `RetrievalPolicy`, the preview, the settings, the grounding module) or is the named killer of a mutant in §21; the eval columns of §28 show each structural metric at the base value before its phase.

## 23. Model-Built Test Results

Model-built schema, PostgreSQL 16.15 / pgvector **0.8.6**, `wasla_rg2_models`, Redis DB 3, at `da0525f`:

| collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- |
| 7,805 | 7,774 | 2 | 29 | 0 | 0 | 63.7 min |

- `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` — the known date bomb, **deferred by the user to audit #24**, not touched.
- `test_knowledge_repair.py::test_the_source_key_backfill_keys_unique_filenames_and_lists_the_rest` — a **Part 1** test that compared a collision group's order; the group is ordered by `(created_at, id)` and both documents share one transaction's timestamp, so it failed whenever the random ids fell the other way (RR-24). Fixed (`b539777`) to compare the group as a set; product code unchanged.

Skips (29), all pre-existing or opt-in, none new: 11 real-provider (no key), 4 schema parity (model lane), 5 Windows signal path, 4 OAuth concurrency (real-Redis variable), 2 kept-data sweeps (run separately, §25), 2 old-pgvector (opt-in), 1 tmpfs.

**After the lanes** two commits changed code: `b539777` (the test fix above, and the unused `_without_repeats` removed) and `f446b01` (identifier-only lexical query, incremental neighbour budget). Not re-run as full lanes; re-run in both schemas as the 816/820-test selection of every knowledge, RAG, agent, orchestrator, isolation, tool-security, deployment-config and documentation suite (model-built **816 passed, 1 skipped**; migration-built **820 passed, 1 skipped**, parity included), the eval, the kept-data sweep (§25) and the full mutation matrix (§21). `eda48d6` changed one test only.

## 24. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations`, a fresh `alembic upgrade head` to `0134` on a recreated `wasla_rg2_migrations`, PostgreSQL 16.15 / pgvector **0.8.6**, at `b539777`:

| collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- |
| 7,805 | 7,779 | 1 | 25 | 0 | 0 | 62.0 min |

- `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` — the known date bomb, deferred to audit #24.

Skips (25): 11 real-provider, 5 Windows signal path, 4 OAuth concurrency, 2 kept-data sweeps (run separately), 2 old-pgvector (opt-in), 1 tmpfs. Schema parity (4 tests) ran and passed.

## 25. CI Jobs Run Locally

| `ci.yml` job | Run locally | Result |
| --- | --- | --- |
| `quality` — Ruff, Black, MyPy | as in CI | clean (§26) |
| `tests` — unit, integration, e2e, model-built | the full lane | §23; RR-24 fixed; date bomb deferred |
| `migration-parity` — parity and every integration / e2e suite on the migrated schema | the full lane | §24; parity ran |
| `migration-parity` — AI and tool invariants over kept data | the exact step (`WASLA_TEST_KEEP_AI_DATA=1`, the `ai_harness` file selection, fail on any skip), migration-built | first run **1 failed, 325 passed**: Part 1's model-change test read a platform-wide stale listing of ten and saw the documents this Part's two new harness suites keep — fixed (`eda48d6`, pages and filters to its workspace); re-run **326 passed, 0 skipped** |
| `monitoring` — promtool config, rule tests | in `prom/prometheus:v3.1.0` | SUCCESS (§27) |
| `docker` — image build, liveness | not run | no Dockerfile or dependency change in this Part |
| `security.yml`, `deploy.yml` | not run | no dependency change; deploy out of scope |

Known deferred failures beside every lane: the date bomb in `test_financial_integrity.py` (canonical CI red since 2026-10-27), FRM-05, FRM-07 (`test_pdf_extraction_bounds`' 2.0 s bound — passed in both lanes here, not loosened, RQ-29), CPM-06 — all deferred to audit #24.

## 26. Ruff / Black / MyPy

Tracked tree at `eda48d6`: `ruff check . --exclude audit` → All checks passed; `black --check . --exclude audit` → 1,072 files unchanged; `mypy app tests` → no issues in 947 source files. No new `type: ignore` beyond one narrow `arg-type` in a unit test passing a fake embeddings client to `prepare`.

## 27. Prometheus / Alertmanager Gates

`promtool check config` (prom/prometheus:v3.1.0, the pinned digest): SUCCESS, **86 rules**. `promtool test rules tests/alerts_test.yml`: SUCCESS, including the two new alerts. Metric catalogue tests green. `alertmanager.yml` unchanged.

## 28. Evaluation Results

Fake embeddings — **structural**. Every column measured by the **final** harness against that phase's commit (disposable worktree `E:\wasla-rg2-eval`; `audit/rag/eval_columns.sh`), so columns are comparable. Part 1's after-P6 column equals "base".

| Metric (fake embeddings) | Part 1 after P6 (`131bfa4`) | After 9a (`7699d4c`) | After 9b (`3126159`) | After 9c (`859636a`) | After 9d (`d220daa`) | After 10 (`18b4aab`) | Final (`f446b01`) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Span in top 5 (threshold lifted) | 241 / 261 | 241 | 253 | 252 | 252 | 252 | 249 |
| Span at rank 1 (threshold lifted) | 193 / 261 | 193 | 203 | 208 | 208 | 208 | **236** |
| Spans retrievable by exact words (lexical alone, top 5) | n/a | n/a | 257 / 261 | 257 | 257 | 257 | 257 |
| Exact-identifier questions answered at 0.75 (identifier-blind; vector → hybrid) | 24 / 57 → n/a | 23 → n/a | 23 → 43 | 32 → 43 | 32 → 43 | 32 → 42 | 32 → **57** |
| … of which Latin brand inside Arabic | 0 / 3 | 0 | 3 | 3 | 3 | 3 | 3 |
| … of which CSV SKUs | 0 / 18 | 0 | 4 | 4 | 4 | 3 | **18** |
| Follow-up questions (as typed → standalone, scripted) | 0 → 9 / 10 | 0 → 9 | 2 → 10 | 0 → 10 | 3 → 10 | 2 → 10 | 0 → 10 |
| Cross-lingual questions (as typed → with `alternate_query`) | 0 → n/a / 8 | 0 → n/a | 1 → n/a | 1 → n/a | 1 → **5** | 1 → 5 | 2 → 6 |
| Questions with a stale version in the top results | 0 / 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Duplicate passages per search | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| Two-chunk answers complete, `max_results=1` | 9 / 24 | 8 | 9 | **22** | 22 | 22 | 22 |
| Two-chunk answers complete, `max_results=5` | 24 / 24 | 24 | 24 | 24 | 24 | 24 | 24 |
| Passages delivered vs kept at the largest `max_results` | 1,821 / 2,610 (10) | 1,566 / 1,566 (6) | 1,566 / 1,566 | 1,160 / 1,160 | 1,161 / 1,161 | 1,161 / 1,161 | 1,188 / 1,188 |
| Answerable-but-empty at 0.75 | 6 / 261 | 6 | 6 | 6 | 6 | 6 | 6 |
| Labelled spans intact / numbers cut / words cut / headings lost | 255 / 0 / 0 / 0 | same | same | same | same | same | same |
| Ungrounded numbers flagged / false positives (corpus replies) | not checked | — | — | — | — | 113 / 113 changed caught; **0 / 304** false positives | same |

Top-5 is 249 at the final commit, 3 below 9b's 252: word-only questions no longer use the exact-word channel by default (§8); rank-1 rose 208 → 236 because identifier questions now rank precisely. The vector-only column rises at 9c (23 → 32) because merged neighbours carry the row a model number names; ± 1 between runs on the identifier-blind subset comes from RRF ties broken by chunk id, which is random per ingest (RR-20). The structural determinism gate (each run measured twice) passes in every column.

**Real-provider metrics — EXTERNAL VERIFICATION, not run:**

| Metric | MSA | Egyptian | English | Cross-lingual |
| --- | --- | --- | --- | --- |
| recall@5 | | | | |
| MRR@10 | | | | |
| nDCG@5 | | | | |
| answerable-but-empty | | | | |

## 29. Findings Ledger

### Root causes

| ID | Status |
| --- | --- |
| RC-1, RC-2, RC-3 | CLOSED (Part 1) |
| RC-4 single-stage retrieval | CLOSED in code (Part 2: hybrid, RRF, keep rule, dedupe, merge, neighbours; re-rank available, off); semantic effect EXTERNAL VERIFICATION |
| RC-5 query formulation | CLOSED in code (Part 2: query contract, `alternate_query`); real-model adherence EXTERNAL VERIFICATION |
| RC-6 scope | DECISION APPLIED (Part 2: scoped grants; one answering agent today) |
| RC-7 | CLOSED (Part 1) |

### Findings

| ID | Status |
| --- | --- |
| RV-01, RV-02, RV-05 .. RV-10, RV-12, RV-13, RV-14, RV-16, RV-17, RV-18, RV-20, RV-23, RV-27 | CLOSED (Part 1) |
| RV-03 | CLOSED (Part 1); production version EXTERNAL VERIFICATION |
| RV-04 | CLOSED as detection (Part 1); repair of pre-stage documents ACCEPTED RESIDUAL |
| RV-11 | DEFERRED TO PART 3 |
| RV-15 | CLOSED (Part 2) |
| RV-19 | CLOSED (Part 2) |
| RV-21 | CLOSED (Part 2) |
| RV-22 | CLOSED for Chromium/WeasyPrint (Part 1); Word layout ACCEPTED RESIDUAL (RR-13) |
| RV-24 | CLOSED (Part 1 five rows, Part 2 three rows — §8 table empty) |
| RV-25 | SCALE-TRIGGERED (documented trigger, RUNBOOK "Search is slow") |
| RV-26 | CLOSED in code (Part 1 telemetry, Part 2 tiers); calibration EXTERNAL VERIFICATION |

### Fix plan

| ID | Status |
| --- | --- |
| FX-0.1 .. FX-0.5, FX-1.1 .. FX-1.13, FX-2.1 .. FX-2.7, FX-3.1, FX-3.10 | as Part 1's ledger (CLOSED; FX-0.1 and FX-3.1's real runs EXTERNAL VERIFICATION) |
| FX-1.11 | CLOSED (Part 1 + Part 2: §8 empty) |
| FX-3.2 contextual embedding | CLOSED |
| FX-3.3 hybrid | CLOSED |
| FX-3.4 | dedupe / merge / cap CLOSED; re-rank DECISION APPLIED, default off |
| FX-3.5 query contract | CLOSED |
| FX-3.6 per-agent scope | CLOSED |
| FX-3.7 embedding model trial | EXTERNAL VERIFICATION |
| FX-3.8 budget / top-k | CLOSED |
| FX-3.9 preview | CLOSED |
| FX-3.11 neighbours | CLOSED |
| FX-3.12 tiers | DECISION APPLIED, default off, inert until calibration |
| FX-3.13 grounding | DECISION APPLIED (observe); false-positive rate 0 / 304 on corpus replies |
| FX-3.14 pre-retrieval | DECISION APPLIED, default off, inert until tiers are calibrated |
| FX-3.15 parallel sentiment | DECISION APPLIED, default off |
| FX-3.16 cache-friendly prompts | CLOSED (stable prefix, chronological rounds, compact sources, margin inert); `prompt_cache_key` EXTERNAL VERIFICATION (client does not support it) |
| FX-3.17 | DEFERRED TO PART 3 |
| FX-4.1 .. FX-4.10 | DEFERRED TO PART 3 (FX-4.4's gauges and alerts built in Parts 1–2) |

### Improvements

| ID | Delivered by | Status | Setting (default) |
| --- | --- | --- | --- |
| IMP-01 | FX-3.1 | CLOSED (structural, extended here); real metrics EXTERNAL VERIFICATION | — |
| IMP-02, IMP-03, IMP-04, IMP-06, IMP-13, IMP-23 | Part 1 | CLOSED | as Part 1 |
| IMP-05 | FX-3.2 | CLOSED | always on |
| IMP-07 | FX-3.17 | DEFERRED TO PART 3 | — |
| IMP-08 | FX-3.3 | CLOSED | `RAG_HYBRID_ENABLED=true` |
| IMP-09 | FX-3.5 | CLOSED | tool schema |
| IMP-10 | FX-3.4 | dedupe/diversity CLOSED; re-rank DECISION APPLIED | `RAG_RERANK_ENABLED=false` |
| IMP-11 | FX-3.6, FX-2.7 | CLOSED (scope + validity) | grant config |
| IMP-12 | FX-3.7 | EXTERNAL VERIFICATION | — |
| IMP-14 | FX-3.13 | DECISION APPLIED | `RAG_NUMBER_GROUNDING=observe` |
| IMP-15 | FX-3.11 | CLOSED | always on |
| IMP-16 | FX-3.12 | DECISION APPLIED | `RAG_CONFIDENCE_TIERS_ENABLED=false` |
| IMP-17 | FX-3.14 | DECISION APPLIED | `RAG_PRERETRIEVAL_ENABLED=false` |
| IMP-18 | FX-3.15 | DECISION APPLIED | `RAG_PARALLEL_SENTIMENT_ENABLED=false` |
| IMP-19 .. IMP-21, IMP-24, IMP-25 | FX-4.6 .. 4.8, 4.9, 4.10 | DEFERRED TO PART 3 | — |
| IMP-22 | FX-3.16 | CLOSED (cache key EXTERNAL VERIFICATION) | `RAG_PASSAGE_MARGIN=` (inert) |
| IMP-26 | FX-3.9 | CLOSED | admin route |

### Decisions and remediation items

| ID | Status |
| --- | --- |
| RQ-01 .. RQ-11, RQ-23, RQ-24, RQ-29 | DECISION APPLIED (Part 1), unchanged |
| RQ-12 .. RQ-20 | DECISION APPLIED (Part 2; deviations in §4) |
| RQ-21, RQ-22, RQ-25 .. RQ-28 | DEFERRED TO PART 3 |
| Hosted / self-hosted re-rankers and embedding models | DEFERRED PRODUCT DECISION (DPA / hosting) |
| LLM-written chunk context | DEFERRED — LATER PRODUCT |
| Multi-agent routing | DEFERRED — LATER PRODUCT |
| FUT-RAG-05 citations, FUT-RAG-06 grounded-only mode | unchanged (DEFERRED — LATER PRODUCT; `strict` is per-workspace opt-in) |
| RR-01 .. RR-08, RR-10, RR-11 | CLOSED (Part 1), 0 after this Part's rehearsal |
| RR-09 | reserved for Part 3 |
| RR-12 active chunk without lexical text | CLOSED (check built; 43 → 0 in the rehearsal) |
| RR-13 Word two-logical-pages layout | ACCEPTED RESIDUAL (Part 1) |
| RR-14 | CLOSED (Part 1) |
| RR-15 Part 1's corpus had no follow-up / cross-lingual questions | CLOSED (`part2/contract.jsonl`) |
| RR-16 `'simple'` parser splits identifiers and grouped numbers | CLOSED BY DESIGN (Python lexemes, `array_to_tsvector`) |
| RR-17 with hybrid on, not-yet-rebuilt chunks rank below rebuilt twins during the first `reindex-all` | ACCEPTED RESIDUAL with a rehearsed procedure (rebuild with hybrid off; DEPLOYMENT.md) |
| RR-18 CSV SKUs stayed weak (3–4 / 18) while the exact-word query ORed every word of the question | CLOSED (`f446b01`: identifier questions query their identifiers; 18 / 18) |
| RR-19 the first per-document cap dropped rather than deferred | CLOSED (found by a test, fixed in `72fd108`) |
| RR-20 ± 1 between eval runs on the identifier-blind subset (RRF ties broken by random chunk ids) | ACCEPTED RESIDUAL (Info; each run is itself deterministic) |
| RR-21 `_without_repeats` left unused in `retrieval_service.py` after the 9c refactor | CLOSED (removed, `b539777`) |
| RR-24 a Part 1 test compared a collision group's order, which ties broke by random id | CLOSED (`b539777`; found by the model-built lane) |
| RR-25 the default search is +19.5 to +24.7 ms p95 over the base at 200 and 12,000 chunks (target +15): always-on fusion, repeat checks and two neighbour round trips | ACCEPTED RESIDUAL (< 100 ms absolute; batch the neighbour lookups, FX-4.7, Part 3) |
| RR-26 Part 1's model-change test read a platform-wide page of ten and broke CI's kept-data sweep once this Part's harness suites kept documents | CLOSED (`eda48d6`) |
| Word-only questions in the exact-word channel | DECISION APPLIED: `RAG_HYBRID_ALL_QUESTIONS=false` (+97 ms p95 at 12,000 chunks) |
| RR-22 `prompt_cache_key` unsupported by `ResponsesClient` | EXTERNAL VERIFICATION |
| RR-23 a `strict`-withheld turn ends `empty_response` and is not charged as composed | ACCEPTED RESIDUAL (ADR-177; distinguished by its handoff reason) |
| Known deferred failures (date bomb in `test_financial_integrity.py`, FRM-05, FRM-07, CPM-06) | DEFERRED to audit #24 by the user |

## 30. Handover to Part 3

- Branch from this Part's final commit on `rag-remediation-p2-retrieval-20261029` (`git log -1`, §34).
- Probes, rehearsal scripts, eval-column script and the mutation runner: `E:\wasla-rag-notes\probes-p2\` (copy of `audit/rag/`); eval tables in `…\eval-runs\` and `…\eval-runs\columns\` — keep the harness and append columns.
- Built for Part 3: `served_predicates` (a new query path uses it, and a cache key must include the space and scope it encodes); `search_text` / lexemes (the gap report and caches key on normalised queries); the per-workspace switches table (`tenants.knowledge_settings`) for any new switch; RR-12 for the shadow switch's coverage check; the isolation gate (add a fixture per path: caches, RLS).
- Open for Part 3: RV-11, FX-3.17, FX-4.1 .. FX-4.10 (FX-4.7 first for RR-25: one round trip for the neighbour lookups), RR-09, RR-13.

## 31. Remaining External Actions

Not performed:

1. Part 3 on top of this Part's final commit.
2. After Part 3: one canonical merge verification of the stacked branch (its own prompt) → merge into `worktree-billing-google-auth` → CI → push.
3. First deploy: `db_preflight` (pgvector ≥ 0.8); Part 1's back-fills; **`RAG_HYBRID_ENABLED=false`**, `reindex-all` (one pass builds contextual embeddings and lexical text), `rag_invariants verify` until RR-12 = 0, then `RAG_HYBRID_ENABLED=true`.
4. Real-provider evaluation (only when authorised): recall@5 / MRR@10 / nDCG@5 per language mix; calibrate `MAX_DISTANCE`, `RAG_CONFIDENCE_TIERS` and `RAG_PASSAGE_MARGIN` from the distance distribution; check the new tool schema (standalone queries, `alternate_query`) with the real model.
5. FX-3.7: `text-embedding-3-small` vs `-large` at 1,536 dimensions on the eval set; switch only through Part 3's shadow switch.
6. Per workspace, after calibration: tiers, pre-retrieval, re-rank (with `RAG_RERANK_MODEL`), grounding `correct`/`strict`, parallel sentiment.
7. Product decisions: hosted / self-hosted re-rankers and embedding models; multi-agent routing.
8. Keep this Part's containers and worktrees until the merge verification; then the infrastructure cleanup prompt.

**External verification items** (never CLOSED here): retrieval quality with the real model (every semantic metric); the 0.75 threshold and every calibrated cut-off; FX-3.7; the production pgvector version; customer PDFs beyond the three producers; `prompt_cache_key` and cached-token share in real traffic; the false-positive rate of grounding on real replies.

## 32. Infrastructure Created by This Part

| Kind | Name | State |
| --- | --- | --- |
| Worktree / branch | `E:\wasla-rag-p2` on `rag-remediation-p2-retrieval-20261029` | kept for review; `audit/` untracked |
| Worktree | `E:\wasla-rg2-base` (detached `131bfa4`) | kept: seeds rehearsals and P-R27/P-R26 "before" with Part 1's code |
| Worktree | `E:\wasla-rg2-mut` (detached `da0525f`) | kept; `git status` under `app/` clean |
| Worktree | `E:\wasla-rg2-eval` (detached `da0525f`) | kept: phase columns; clean |
| Containers | `wasla-rg2-pg` (127.0.0.1:55971, pgvector 0.8.6), `wasla-rg2-redis` (56971), `wasla-rg2-minio` (59971) | running |
| Databases | `wasla_rg2_models`, `wasla_rg2_migrations`, `wasla_rg2_probe`, `wasla_rg2_eval`, `wasla_rg2_mut`, `wasla_rg2_mig` (rehearsed, hybrid on), `wasla_rg2_mig2` (rehearsed, hybrid off), `wasla_rg2_mig_copy` (pristine seed, round trip), `wasla_rg2_fresh` (fresh 0001→head; latency drill, 25,200 chunks); per-test migration databases dropped by their tests | kept |
| Redis DBs | 3 (models lane), 4 (mutation), 5 (seed, eval), 6 (probes), 7 (rehearsal) | kept |
| Bucket | `wasla-rg2-media` on `wasla-rg2-minio` | kept |
| Scratch | `E:\wasla-rag-p2\audit\rag\` (untracked), copied to `E:\wasla-rag-notes\probes-p2\` | kept |

**Other stages' infrastructure found at the start, not touched:** containers `wasla-rg1-pg`, `wasla-rg1-pg-old`, `wasla-rg1-redis`, `wasla-rg1-minio`; worktrees `E:\wasla-rag-p1`, `-rg1-base`, `-rg1-mut`, `-campaign-features`, `-campaign-media`, `-campaigns-audit`, `-campaigns-remediation`, `-cpf-base`, `-cpf-lane`, `-cpf-lane2`, `-cpf-mut`, `-followups-realtime`, `-frm-report`, `-fur-base`, `-fur-lane`, `-fur-lane2`, `-fur-mut`, `-ntf-audit`. The developer stack was not touched; `E:\wasla` and its untracked documents not touched.

## 33. Score

A **self-score** on the scale Part 1 introduced; the review gave no number. Not an independent measurement.

| Area | Max | Part 1 | Part 2 | Deductions now |
| --- | --- | --- | --- | --- |
| Extraction and text fidelity | 8 | 6 | 6 | RV-04 repair (−1), RR-13 (−1) |
| Unicode and Arabic normalisation | 8 | 8 | 8 | — |
| Chunking | 10 | 9 | 9 | CSV/XLSX rows (Part 3, −1) |
| Embedding input and space handling | 5 | 4 | 4.5 | shadow switch (Part 3, −0.5) |
| Document lifecycle | 12 | 12 | 12 | — |
| Dedupe and identity | 5 | 5 | 5 | — |
| Ranking (hybrid, dedupe, merge, re-rank) | 8 | 4 | 7 | re-rank unmeasured on real traffic (−0.5), word-only questions without the exact-word channel by default (−0.5) |
| Query formulation | 5 | 2.5 | 4.5 | real-model adherence unmeasured (−0.5) |
| Scope and filters | 4 | 2 | 4 | — |
| Context assembly and budget | 5 | 2.5 | 5 | — |
| Tool loop and answer grounding | 7 | 5 | 6 | grounding FP only on synthetic replies (−0.5), RR-23 (−0.5) |
| Tenant isolation | 8 | 8 | 8 | — |
| Re-index and operations | 3 | 3 | 2.5 | RR-17 needs an operator procedure (−0.5) |
| *(latency)* | | | | RR-25 counted under Ranking's −0.5 above; not deducted twice |
| Observability | 3 | 3 | 3 | — |
| Testing and evaluation | 5 | 4.5 | 4.5 | semantic eval external (−0.5) |
| Documentation truth | 2 | 1.5 | 2 | — |
| Real-provider evidence | 2 | 0 | 0 | not authorised |
| **Total** | **100** | **80** | **91** | |

Deductions for this Part's own open items: RR-17 (−0.5), RR-23 (−0.5), word-only questions off the exact-word channel / RR-25 (−0.5), re-rank, query adherence and grounding unmeasured on real traffic (−1.5). Deductions for items DEFERRED TO PART 3: CSV rows (−1), shadow switch (−0.5). No deduction for deferred product decisions, later product, scale-triggered or closed-by-design items.

## 34. Verdict

### Runtime matrix

| Scenario | Before (review) | Before (this Part, `131bfa4`) | After | Verdict |
| --- | --- | --- | --- | --- |
| `max_results: 10` | 4–5 delivered | EN 4, AR 7 of 10; 1,821 / 2,610 kept delivered | max 6, every one delivered (1,188 / 1,188) | CLOSED |
| Large rank-1, small rank-2 passage | rank 2 dropped | dropped | kept (skip-and-continue) | CLOSED |
| SKU / model-number question | vector only | SKU 0/18, model 24/36 (identifier-blind) | 18/18, 36/36 — exact-word channel | CLOSED |
| Latin brand inside an Arabic question | vector only | 0/3 | 3/3 | CLOSED |
| Follow-up «والـ premium؟» | embedded as typed | 0/10 scripted follow-ups | standalone query 10/10 | CLOSED in code (real model external) |
| Arabic question, English price list | one query | 0/8 | `alternate_query` fused: 6/8 | CLOSED in code (real model external) |
| Duplicate / overlapping passages | take slots | 0.0 per search (Part 1) | 0.0; near repeats without a new number dropped | CLOSED |
| Two-chunk answer | half delivered | 9/24 at `max_results=1` | 22/24 | CLOSED |
| Admin search preview | does not exist | no route | exists, admin only, metered | CLOSED |
| Scoped agent ("Sales") | sees every KB | config refused | never "HR" (the nearest) | DECISION APPLIED |
| Price not in the passages | unnoticed | unnoticed | counted (observe); corrected / handed off when enabled | DECISION APPLIED |
| Rounds per knowledge turn | ≥ 2 | 2 | 2 by default; 1 with pre-retrieval on (test) | DECISION APPLIED |
| Sentiment before round 1 | always awaited | awaited | unchanged by default; parallel when on, never sends on escalation | DECISION APPLIED |
| Model-facing context | UUIDs, 13 % structure | UUIDs, ordinals | source numbers; round-2 input 3,375 → 2,952 chars | CLOSED |
| Updated document (Part 1) through hybrid + re-rank | n/a | 7900 only (vector) | 7200 until the swap, 7900 only after, through hybrid | holds |
| Expired document through the lexical channel | n/a | no lexical channel | never retrieved | holds |
| Cross-tenant, every new path | n/a | n/a | 0 foreign rows (lexical, fused, re-ranked, expanded, scoped, alternate, preview, pre-retrieval) | holds |

### Against the conditions (§45 of the prompt)

RV-15 / RV-19 ✓ (§6); RC-4 identifier questions through the exact-word channel, one predicate builder, isolation fixture, M-R32 / M-R33 / M-R46 killed ✓ (§8, §21); duplicates per search 0.0 ✓; RC-5 contract in the schema, follow-up and cross-lingual metrics recorded ✓ (§10); RV-21 admin-only preview ✓ (§11); FX-3.13 observe counts, false positives 0 / 304 ✓ (§12); FX-3.14 / FX-3.15 off by default, tests prove no side effect and no send on escalation ✓ (§13); the rebuild kept every served document served (hybrid off, the documented procedure) and RR-12 = 0 ✓ (§7); Part 1's matrix unchanged ✓ (§16); §8 table empty ✓ (§15); all meaningful mutants killed ✓ (§21); both lanes green apart from the named deferred failure, reported separately ✓ (§23, §24); RR-01 .. RR-08, RR-10, RR-11, RR-12 at 0 ✓ (§17); no real provider ✓; nothing pushed, merged or deployed ✓.

Two things the conditions do not demand but a reader should weigh: the default search is over the +15 ms latency target at two corpus sizes (RR-25, measured, accepted with a reason), and with hybrid on during the first rebuild, ranking briefly moves (RR-17, procedure documented).

**RAG PART 2 (RETRIEVAL QUALITY & ANSWER CORRECTNESS) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

What is left: Part 3; the product decisions (hosted / self-hosted re-rankers and embedding models; routing); the external verifications of §31 (every semantic number, the 0.75 threshold, tiers, margin, FX-3.7, `prompt_cache_key`, grounding on real replies); and the external actions of §31.

**The final question.** A customer asks «بكام موديل BK-35؟», then «والـ premium؟», about an English price list replaced last week. Wasla can now prove:

- **The SKU is found even if the embedding misses it.** `BK-35` is an identifier, so the exact-word channel asks for it alone. The hybrid tests show the row absent from the vector channel and present through the lexical one, past the cut-off, kept for the identifier (`test_a_sku_the_embedding_misses_is_found_by_its_exact_words`).
- **Both questions reach the current row, once.** The follow-up is resolved by the model's standalone query, and the English list by the `alternate_query`, embedded in the same request (`test_an_arabic_question_about_an_english_list_reaches_it_through_the_alternate_query`). Only the current version can be returned: the replacement serves 7900 only after its swap, through the hybrid path too (M-R12 killed). Exact and near repeats are removed before packing.
- **The reply's number is checked against what was retrieved.** With `observe` it is counted; with `correct` or `strict` it is corrected or handed to a colleague (`test_strict_mode_withholds_the_reply_and_hands_over`).
- **Nothing from another workspace could have been in the context.** The same `served_predicates` guard every query path, and each path's isolation fixture and tenant-predicate mutant fails without it.

What Wasla cannot yet prove is that the real model writes the standalone and alternate queries well, or how far the real embedding places the rows: that is the real-provider evaluation, not run.
