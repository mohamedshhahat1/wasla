# RAG Remediation — Part 1 of 3: Text, Chunking & Document Lifecycle

Remediation of `RAG_SYSTEM_REVIEW.md` (independent review, 2026-10-29, reviewed `7cdf863`), Phases 0 .. 8 of its plan plus this Part's Phases 13 .. 18. Branch `rag-remediation-p1-lifecycle-20261029`, worktree `E:\wasla-rag-p1`. **Nothing was pushed, merged or deployed. No real provider was contacted.**

All retrieval numbers below come from **fake embeddings** (`tests/fake_embeddings.py`, a hashed bag of words). They measure **structure** — whether the right passage exists intact and is findable by its words, served once and current — never semantic quality. Semantic quality with the real embedding model is external verification (§33).

---

## 1. Executive Summary

The review's first principle — fix what corrupts the index before tuning what ranks it — is what this Part delivered.

- **Arabic is stored as the letters a customer types.** Every PDF from three producers (Chromium, Microsoft Word, WeasyPrint) stores **0** presentation-form code points (Chromium AR-1 had 89 and AR-2 109 at the base head); a normally typed query shares 8 of 9 words with the stored Chromium passage (0 of 9 before, 5 of 9 with NFKC alone); `٤٥٠٠` typed matches `4500` stored.
- **An updated document is replaced, never duplicated.** The review's demonstration (search returning `7900 EGP` **and** `7200 EGP`) now returns `7200` until the swap and `7900` only after it, with no moment serving neither; a failed replacement keeps v1; a replacement needs no plan slot. The same file twice and the same text in two knowledge bases are refused with stable 409s.
- **The chunker never cuts a word, a number, a line or a heading from its section.** Across the evaluation corpus: numbers cut 9 → 0, words cut 68 → 0, headed chunks without their heading 9 → 0; answer at rank 1 152 → 193 of 261.
- **The agent's last round ends in words**, rounds are replayed in order, searches are capped at two per turn, and **new agents can read the knowledge base**; a workspace whose agent cannot is told (`knowledge_unreachable`, an alert, triage).
- **Operations:** pgvector < 0.8 is refused by name; stale re-index pages; the recovery sweep no longer floods its queue; back-fill, source-key back-fill and `reindex-all` were rehearsed on a database populated by the base-head code — every served document stayed served throughout and every RR invariant ended at 0.
- **Measurement for Parts 2 and 3:** a committed 37-document / 267-question corpus with a deterministic harness, retrieval and cost telemetry, and an isolation gate for every new read path.

32 mutants of this Part, all killed (one first written as an equivalent mutant; §23). Open: the Word producer's two-logical-pages-per-PDF-page layout keeps its running header in one fixture (RR-13, Low), and the items deferred to Parts 2 and 3.

**Verdict: RAG PART 1 (TEXT, CHUNKING & LIFECYCLE) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION** (§36).

## 2. Starting Repository State

| Item | Value |
| --- | --- |
| `RAG_P1_BASE_HEAD` | `7cdf863` (= `origin/worktree-billing-google-auth`; 0 ahead / 0 behind at start) |
| Branch / worktree | `rag-remediation-p1-lifecycle-20261029` / `E:\wasla-rag-p1` |
| First commit | `733af3c docs(rag): add RAG_SYSTEM_REVIEW.md` — SHA-256 `80d92b2b614d08b670a0ce502f3967286bc05de01bff214746a447a28d9bb0ee`, identical to the user's copy in `E:\wasla` |
| Alembic head at base | `0126` (single); now `0131` (single) |
| Last ADR at base | ADR-157; this Part adds ADR-158 .. ADR-167 |
| PostgreSQL | 16 (`wasla-rg1-pg`, pgvector **0.8.6**, the production-pinned digest); `wasla-rg1-pg-old` pgvector **0.7.4** for the guard test only |
| Redis / object store | `redis:7-alpine` (`wasla-rg1-redis`, DBs 3–6), MinIO (`wasla-rg1-minio`, bucket `wasla-rg1-media`) |
| `app.__file__` | `E:\wasla-rag-p1\app\__init__.py` in every lane (and `E:\wasla-rg1-base\…`, `E:\wasla-rg1-mut\…` in theirs) |
| `E:\wasla` working tree | its untracked documents untouched, not staged, not moved |

## 3. Review Revalidation

Every RV of the review, including those Parts 2 and 3 own. "Code reading" findings were reproduced by a test that fails at the base head before being fixed.

| ID | Sev. | Reproduced at `7cdf863`? | Evidence (before) | Part |
| --- | --- | --- | --- | --- |
| RV-01 | Critical | Yes | Chromium AR-1: 89 presentation-form code points, typed query 0/9 words (5/9 after NFKC alone); AR-2: 109 | 1 |
| RV-02 | Critical | Yes | P-R05: v1 7200 → v2 7900 same title → search returns both; re-index of v1 rebuilt 7200 | 1 |
| RV-03 | High | Yes | 0.7.4: `hnsw.iterative_scan` unknown, every search fails; 0.8.6 fine. The review's 12 integration failures do **not** appear on 0.8.6 (RAG unit 182 passed, RAG integration 133 passed) — an artefact of its 0.6.0 | 1 |
| RV-04 | High | Yes | Chromium AR-2 prices re-ordered next to the wrong words; Word and WeasyPrint differently | 1 |
| RV-05 | High | Yes | P-R02: `13` ‖ `,795 EGP`; Arabic prose cut inside `مدار` | 1 |
| RV-06 | High | Yes | A chunk beginning `o foxtrot golf.`; overlap glued with `\n\n` | 1 |
| RV-07 | High | Yes | P-R05 step 4: 7900, 7900, 7200 | 1 |
| RV-08 | Medium | Yes | `Page 3` became a chunk of overlap + `Page 3` | 1 |
| RV-09 | Medium | Yes | `## Refund policy` ended the Shipping chunk; Markdown as plain text | 1 |
| RV-10 | Medium | Yes (test) | Slow PDF (6 s child via test seam) held the plan advisory lock: second upload hit `lock_timeout` | 1 |
| RV-11 | Medium | Yes (existing test) | model change → every search empty, tool says "no information" | 3 |
| RV-12 | Medium | Yes (test) | `--limit 2` thrice over 5 stale documents → the same 2 each time | 1 |
| RV-13 | Medium | Yes (test) | > 100 documents: the oldest unreachable | 1 |
| RV-14 | Medium | Yes (test) | search, search, search on round 3 → empty turn, or "Let me check." sent as the reply | 1 |
| RV-15 | Medium | Yes (P-R06) | top_k 10 advertised; 4–5 passages fit the 6,000-character budget | 2 |
| RV-16 | Low | Yes | identical text, corrected title → `created=false`, title unchanged | 1 |
| RV-17 | Low | Yes (test) | PDF `byte_size` = base64 length | 1 |
| RV-18 | Low | Yes (test) | backlog of 6, batch 4: queue depth 4, 8, 12 over three sweeps | 1 |
| RV-19 | Low | Yes (P-R06) | a large rank-1 passage dropped rank 2 | 2 |
| RV-20 | Low | Yes (P-R07) | cp1252 English and French → CJK; cp1256 Arabic correct | 1 |
| RV-21 | Low | Yes (grep) | `SearchRequest` / `SearchResponse` / `PassageRead` used by no route | 2 |
| RV-22 | Low | Yes (test) | running header and page numbers indexed per page | 1 |
| RV-23 | Low | Yes (test) | query with tatweel / Arabic-Indic digits ≠ stored text | 1 |
| RV-24 | Low | Yes (read) | §8 rows: Markdown structure, page numbers, "read fresh", "run it again", pgvector | 1 (5 rows) / 2 |
| RV-25 | Info | Design | `hnsw.max_scan_tuples` at scale | 2 (docs) |
| RV-26 | Info | Yes (grep) | no distance in telemetry or logs | 1 (telemetry) |
| RV-27 | Low | Yes (test) | round-3 input: all preambles, then all tool exchanges | 1 |

Nothing was NOT REPRODUCED.

## 4. Locked Remediation Decisions

RQ-01 .. RQ-11, RQ-23, RQ-24 and RQ-29 applied as written in the prompt, with these measured outcomes: RQ-03 kept pypdf (pdfminer.six better on Arabic for 0 of 3 producers); RQ-02 held (no schema-version bump was needed anywhere); RQ-29: `test_pdf_extraction_bounds` was not loosened.

Deviations, each recorded: RQ-10's third-search refusal uses the existing `turn_call_limit` reason (a new reason would have been a native-enum migration for a label); RQ-11's flag lives on `AgentRead` only, because the API has no workspace readiness view; RQ-06's `replaced` is a text failure code, so D2 needed no enum change.

## 5. Product Decisions Applied

| Decision | Setting / default | ADR |
| --- | --- | --- |
| Text forms for storage, matching, lexical | always on (a correctness fix; RQ-02 keeps vectors comparable) | 158 |
| pypdf kept; furniture removed; re-ordered lines flagged | always on | 159 |
| Structure-aware chunking, 250/40 estimated tokens | `MAX_CHUNK_TOKENS`, `CHUNK_OVERLAP_TOKENS` constants | 160 |
| Replace through generation snapshot; no slot consumed | `PUT …/content`, `?replace=true` | 161 |
| Source keys; workspace-wide dedupe; resubmission updates metadata | 409 codes below | 162 |
| Extract before locking; real byte size | always on | 163 |
| Last round without tools; chronological input; 2 searches/turn | `MAX_KNOWLEDGE_SEARCHES_PER_TURN = 2` | 164 |
| Default `search_knowledge` grant for new agents | `AGENT_DEFAULT_KNOWLEDGE_GRANT=true` | 165 |
| Cursor listings, effective dates, recency, originals | always on | 166 |
| Telemetry, eval harness, pgvector floor, paged operations | always on | 167 |

G9 ("behind a setting"): only the default grant changes **what an agent may do** and carries a setting. The text, chunking and lifecycle changes are corrections of stored data, not alternative behaviours, and are not switchable; recorded here rather than silently.

## 6. pgvector Guard and Triage (FX-0.1 .. FX-0.5, FX-1.3)

- `scripts/db_preflight.py` (`prerequisites`, `verify`) and readiness refuse pgvector < 0.8.0, compared as numbers (`0.10.0` passes — M-R41). On `wasla-rg1-pg-old`: `extension too old: pgvector 0.7.4 is older than 0.8.0, which knowledge search needs (hnsw.iterative_scan)`, exit 1. On 0.8.6: `db_preflight verify: ok`. The refusal is followed by the generic "cannot install a required extension" advice block, which is accurate but wordy.
- `docker-compose.yml` pins the production digest (was the floating `pgvector/pgvector:pg16`).
- `python -m scripts.rag_triage` (read-only, counts and workspace ids only): duplicate titles, cross-KB copies, presentation-form documents, extraction warnings, unreachable knowledge. There is no production data; on the rehearsal database it reported the seeded duplicate and cross-KB copy.
- RUNBOOK: do not change `OPENAI_EMBEDDING_MODEL` outside the shadow switch (Part 3).
- `tests/integration/test_pgvector_version_guard.py` runs its refusal half only where `wasla-rg1-pg-old` is configured (`WASLA_TEST_OLD_PGVECTOR_URL`); CI has no old container, so CI runs the unit half (`tests/unit/test_pgvector_version.py`) and the passing half.

## 7. Evaluation Harness and Telemetry (FX-3.1, FX-1.13)

- Corpus `tests/fixtures/rag/eval/corpus/`: 37 documents (price lists in Arabic, English and mixed digits; a finishing catalogue; return, shipping and warranty policies; FAQs; a CSV catalogue; two versions of one price list; the Arabic PDFs from three producers; one prompt-injection document). `questions.jsonl`: 267 questions (MSA, Egyptian dialect, English, cross-lingual, follow-ups, identifiers, Arabic-Indic digits, unanswerable), labelled by **text span**. Synthetic; no personal data.
- `tests/integration/test_rag_eval.py` (CI, fake embeddings) asserts the structural properties; the report writer prints one table per run to `audit/rag/eval-runs/`. Three baseline runs are byte-identical.
- `tests/real_provider/test_rag_eval_real.py`: recall@5, MRR@10, nDCG@5, answerable-but-empty per language mix — **not run** (no authorisation).
- Telemetry (no tenant or text label): `wasla_rag_best_distance{model}`, `wasla_rag_passages_dropped_total{reason=threshold|duplicate}`, `wasla_rag_context_characters`, `wasla_ai_stage_seconds{stage=sentiment|round|embedding|sql}`, `wasla_ai_cached_input_tokens_total`; `TokenUsage` parses `cached_tokens` and `reasoning_tokens`, recorded on usage rows; the `knowledge.searched` log carries `best_distance` and drop counts, never text.

## 8. Isolation Gate (FX-1.12)

`tests/integration/test_knowledge_isolation_gate.py`: two workspaces with identical titles, texts, hashes and filenames. For vector search, the retrieval context, replacement with the other workspace's text, source-key and dedupe lookups, cross-KB listings / similar documents / pages, every new route given a foreign id (404), and originals' object keys: no foreign id, title, text, count or key in any result, refusal or listing; a duplicate refusal never reveals another workspace holds the text. The injection document comes back only inside `function_call_output`. Mutant M-R19 (dedupe lookup made global) is killed by it.

## 9. Text Normalisation and Extraction (RV-01, RV-04, RV-20, RV-22, RV-23)

`app/services/text_normalisation.py` (RQ-01), applied at submit, replacement, every extraction, the embedding input (`for_matching`) and the query (`for_matching`). Property tests: idempotence of each form, ASCII unchanged except whitespace, every invisible and bidi control removed, the digit map round-trips 0–9 in both scripts, the A.3 phrase found.

**Producer table** — pypdf (the default kept), base head → after:

| PDF | Presentation-form code points | Typed-query words shared | AR-2 price lines intact (6) | Warnings raised | MX-1: headers / footers / bare numbers left, page join |
| --- | --- | --- | --- | --- | --- |
| Chromium AR-1 | 89 → **0** | 0/9 (5/9 NFKC only) → **8/9** | — | `digits_glued_to_arabic` 1 | — |
| Chromium AR-2 | 109 → **0** | — | 4 of 6 (`Y-Y-YY`) | glued 4, leading zero 2 | — |
| Chromium MX-1 | 0 | — | — | none | 0 / 0 / 0, joined |
| Word AR-1 | 0 | 8/9 → 8/9 | — | none | — |
| Word AR-2 | 0 | — | 4 of 6 | leading zero 2 | — |
| Word MX-1 | 0 | — | — | none | **3 / 3 / 1, not joined** (RR-13) |
| WeasyPrint AR-1 | 0 | 8/9 → 8/9 | — | none | — |
| WeasyPrint AR-2 | 0 | — | **0 of 6** | leading zero 2 | — |
| WeasyPrint MX-1 | 0 | — | — | none | 0 / 0 / 0, joined |
| EN-1 (all three) | 0 | — | every row intact | none | — |

pdfminer.six on the same files: better Arabic order for 0 of 3 producers, so RQ-03 kept pypdf. Bidi warnings: no false positive on any correct line in the fixtures (`test_correct_lines_raise_no_warning`, EN-1 and AR-1 of Word/WeasyPrint raise none). AR-2 lines that two producers reverse or drop are flagged, not repaired (RQ-04): 6 corpus questions remain unanswerable for that reason.

**RR-13 (new, Low).** The Word export of MX-1 has two PDF pages of which the first holds two *logical* pages (header … footer, `1`, header … footer, `2`). Furniture removal works at PDF page edges, so the inner header, footer and `1` stay. Documented, not guessed at: a rule that strips repeated lines anywhere in a page would also strip legitimate repeated rows. ACCEPTED RESIDUAL for Part 1; candidate for Part 3's format work.

RV-20 (`extract_text` for customer files): UTF-16 is tried only with a BOM; Latin-range bytes decode as cp1252, Arabic as cp1256 (`tests/unit/test_text_decoding.py`: cp1252 English and French correct, cp1256 Arabic correct).

## 10. Chunking (RV-05, RV-06, RV-08, RV-09)

ADR-160. Over the eval corpus, base → after: chunks 116 → 97 (after P6; 101 after P4), numbers cut **9 → 0**, words cut **68 → 0**, headed chunks without their heading **9 → 0**, chunks holding presentation forms 2 → 0. Size: at most 250 estimated tokens plus 40 of overlap (`test_token_estimates_are_positive_and_sizes_comparable_across_scripts`). Property tests over every corpus document: no boundary or overlap start inside a word or number, every number intact in some chunk, every headed chunk carries its heading, no chunk is overlap alone, the pieces reproduce the normalised text. A.1 and A.2 probes ported and passing. Two tests were strengthened by the mutation matrix (M-R07, M-R09; §23).

## 11. Data Repair and `reindex-all` — the rehearsal

Database `wasla_rg1_mig`, migration-built at `0126` **by the base-head code** (worktree `E:\wasla-rg1-base` at `7cdf863`, `app.__file__` asserted), populated through the base `KnowledgeService`, worker and PDF sandbox (fake embeddings at the transport): 2 workspaces, 3 knowledge bases, **40 documents** (12 PDFs from 3 producers — one byte-identical extraction became a repeat at the base — 17 text, 6 Markdown, a v1/v2 price-list pair, a cross-KB copy, a glyph/letters collision pair, one permanently failing document), 49 chunks, generations `active` 39, `pending` 1, `processing` 1 (expired lease), `failed` 1, `superseded` 1. Then, with this branch (`audit/rag/rehearsal_run.py`):

| Step | Result |
| --- | --- |
| `alembic upgrade head` (0126 → 0131) | 3.05 s; row count and MD5 of every original column of `knowledge_bases`, `documents`, `document_index_generations`, `document_chunks` identical before and after |
| Invariants before repair | RR-04 1, RR-06 6, RR-07 (not normalised) 3, all else 0 |
| Served before | 39 of 40 (all but the failing document) |
| `backfill-normalised-content` | 0.26 s; examined 40, updated 2, **1 collision** (glyph-older keep / letters-newer delete — exactly as seeded), **4 listed for re-upload** (AR-2 PDFs) |
| `backfill-source-keys` | keyed 34; **1 collision** listed (v1 and v2 both `pricing.txt`) — nothing merged |
| Operator deletes | the collision's newer document and the cross-KB copy; back-fill again: 1 updated, 0 collisions |
| `reindex-all --rate 20` | 37 requested, 1 skipped (failed) and listed; 14.8 s to request because every pause ran a full served-set check (37 searches) — 2.5 requests/s with the watcher, not a throughput limit of the command |
| Drain + one recovery sweep | 37 jobs; the expired-lease generation re-queued by the sweep and published |
| **Served throughout** | **true** — the 37 kept documents found by their own text before, between every request, every 50 ms of the drain, and after; 0 gaps |
| Invariants after | **RR-01 .. RR-08 all 0** (incl. both halves of RR-07) |
| Documents not on exactly one active generation | only the failed one (0 active, by design) |
| Active generations outside the current space | 0 |
| Downgrade to 0126 afterwards | refused: `Refusing to downgrade 0129: it would discard 34 document source keys.` |

The back-fill is an operator command, not a migration (no long transaction on `documents`). `EMBEDDING_SCHEMA_VERSION` unchanged.

## 12. Document Replacement (RV-02) — the 7200 / 7900 proof

`tests/integration/test_document_replacement.py`, real pipeline on migration-built PostgreSQL, real repository search, fake embeddings at the transport:

| P-R05 step | Base head | After |
| --- | --- | --- |
| 1. v1 (7200) indexed | 1 document, 1 active generation, search: 7200 | same |
| 2. v2 (7900) uploaded with the same title | 2 documents, search: **7900 and 7200** | v2 sent by `PUT …/content`: 1 document; search **7200 only** while v2 builds (provider held at a gate), **7900 only** after publish |
| 3. re-index v1 | rebuilt the 7200 text | re-index rebuilds the current text (7900) |
| 4. v2 copied to a second KB | 7900, 7900, 7200 | `409 document_exists_in_other_knowledge_base`; a legacy copy seeded past the refusal is served **once** |
| 5. identical v2, corrected title | `created=false`, title unchanged | title updated, `metadata_updated: true`, audited |

Also proven: a failed v2 (permanent and transient) keeps v1 serving and shows the failed attempt; two PUTs in a row leave one attempt in flight and the newest content wins; concurrent PUTs coalesce without deadlock; a worker already embedding the old attempt is fenced out; a same-hash PUT is metadata only with no provider call; another document's hash → 409; a workspace at its document limit can replace; a delete during a replacement build removes the document at once and the late worker publishes nothing. `pricing.pdf` twice → `409 document_source_exists`, or replaced with `?replace=true`. Each test fails at `7cdf863` (the route or behaviour does not exist there) and is guarded by M-R12 .. M-R18.

## 13. Source Keys, Dedupe, Listing, Recency, Originals, Effective Dates (RV-07, RV-13, RV-16, RV-17, RV-10)

- Source keys (RQ-07), workspace dedupe (RQ-08) and the retrieval-time safety net: §12; isolation: §8.
- Listing (RV-13): 250 documents listed by cursor exactly once each, oldest last; filters; similar documents; KB rename and delete (`?cascade=true`); KB pages by cursor. The cursor walk is bounded, so an ignored cursor fails rather than hangs (found by M-R21).
- Effective dates: a document outside `valid_from` / `valid_until` is never retrieved; each source in the tool output carries `updated_at` and its section.
- Originals (RQ-23): kept under the workspace's prefix in MinIO, owed a delete with their document, erased by the workspace purge (`test_a_purge_deletes_the_workspaces_knowledge_originals`, against MinIO).
- RV-16: resubmission updates the title (audited). RV-17: a PDF's `byte_size` is its file size.
- RV-10: two uploads to one workspace succeed while a 6-second PDF parses (the second returns before the first finishes); **0 pooled connections checked out** mid-parse; a burst larger than the pool does not exhaust it.

## 14. Worker and Commands (RV-12, RV-18)

- `reindex-stale-embeddings`: three runs at `--limit 2` over 5 stale documents select 2, 2, 1 disjoint documents (base: 2, 2, 2 — the same two); `--after <cursor>` continues; a document whose re-index failed is not selected again.
- Recovery sweep: 6 pending, batch 4, three sweeps → queue depth **4, 6, 6** (base: 4, 8, 12); after the visibility timeout an unclaimed job is sent again (no attempt is lost). `last_enqueued_at` (D8, migration 0128) is cleared by the claim.

## 15. Agent Loop and Default Grant (RV-14, RV-27, RC-7)

`tests/unit/test_orchestrator_final_round.py` (all five fail at `7cdf863`):

- a model that would search on every round still ends the turn in a reply: the last round is asked with `tool_choice: "none"` and a closing instruction, with the same tools described;
- "Let me check." alongside a call followed by an empty answer → no reply (`empty_response`), never the preamble;
- a terminal handoff on the last round still hands off, under `allowed_tools` naming only the handoff;
- a third `search_knowledge` call is refused with a fixed message; another tool in the same response runs;
- the **bytes handed to the transport** (real `ResponsesClient` over `httpx.MockTransport`): round 3's input is `user, assistant "Checking.", call c1, output c1, assistant "Still looking.", call c2, output c2, system`, and each round's input begins with the previous round's.

One existing test asserted RV-14 itself (`test_the_round_limit_stops_the_loop_but_keeps_the_text` expected "Still checking." as the reply); it now asserts the reversal. Every other round-loop, tool, handoff and CRM integration suite passed unchanged.

Default grant: a new agent holds exactly `search_knowledge` (enabled, audited); `knowledge_unreachable` true for P-R18's workspace and false after the grant; false for a workspace with no documents of its own; no migration touches existing agents.

## 16. Observability and Alerts

Metrics in §7 plus `wasla_knowledge_extraction_warnings_total{code}`, `wasla_knowledge_replacements_total{outcome}`, gauges `wasla_knowledge_duplicate_documents`, `wasla_knowledge_unreachable_workspaces` — all tenant-free, all in the metric catalogue test. Alerts `KnowledgeUnreachable` (info, 1 h), `KnowledgeDuplicateDocuments` (info, 6 h), `KnowledgeReplacementsFailing` (warning, > 50 % for 30 min), each with a promtool fire-and-clear test and a RUNBOOK entry.

## 17. Documentation Drift and RUNBOOK (RV-24)

| §8 row | Now |
| --- | --- |
| Markdown "keeps its punctuation, because headings are structure the chunker uses" (RV-09) | the code does it: headings are boundaries and breadcrumbs |
| The page-number promise (RV-08) | the code does it: page numbers removed at page edges, short pieces merged |
| "The document's text is read fresh … a resubmission may have replaced it" (RV-02) | the doc says what the code does: a replacement is a generation snapshot; a re-index reads the current text |
| `reindex-stale-embeddings` "run it again for the next batch" (RV-12) | the code does it: disjoint pages and a printed cursor |
| "PostgreSQL with pgvector" (RV-03) | the doc says 0.8.0 or newer, and the code refuses older |

DEFERRED TO PART 2: "1 to 10 passages" (RV-15), "unrelated text around 0.8" (RV-26 calibration), knowledge bases "point different agents at different sets" (RQ-18 / RC-6).

RUNBOOK sections added: an updated document shows old facts; Arabic PDF not found; re-index everything; pgvector version; answers ignore a document; `KnowledgeReplacementsFailing`. Also updated: `docs/RAG.md`, `AI_AGENTS.md`, `API.md` (252 operations), `ARCHITECTURE.md`, `DEPLOYMENT.md`, `OBSERVABILITY.md`, `SECURITY.md`, `MEDIA.md`, `README.md` (migrations 0001–0131), `DECISIONS.md` ADR-158 .. ADR-167 (ADR-161 amends ADR-106).

## 18. Re-Verification of What the Review Proved

All still pass in both lanes: cross-tenant search returns nothing foreign (existing property tests + §8); re-ingesting twice does not double chunks; a re-index serves the old generation until an atomic swap; a stale claim cannot publish and a delete during an embedding call commits at once; permanent → failed after one attempt, transient → `retry_exhausted` after five; a suspended workspace spends nothing; the PDF sandbox refuses > 40 pages and over-limit text and is killed at the deadline, never truncating; a NaN or wrong-width vector is a failed tool call and the turn continues; a model change excludes old vectors until re-indexed; retrieved text only in `function_call_output`.

## 19. Database Invariant Sweep

`scripts/rag_invariants.py` — run alone (`python -m scripts.rag_invariants verify`) and inside `python -m scripts.omnichannel_invariants verify`, in one read-only transaction.

| ID | Check | Non-vacuity (injected in a rolled-back transaction) | Rehearsal after |
| --- | --- | --- | --- |
| RR-01 | > 1 active generation per document | drop the partial unique index, activate a superseded generation | 0 |
| RR-02 | ready document not served by one active generation with chunks | mark an unindexed document ready | 0 |
| RR-03 | chunk of a non-active generation | supersede an active generation | 0 |
| RR-04 | the same ready text twice in a workspace | drop the KB-hash constraint, copy a hash | 0 |
| RR-05 | a source key twice in a KB | drop the partial unique index, duplicate a key | 0 |
| RR-06 | presentation forms in documents or chunks | write glyphs into a chunk | 0 |
| RR-07 | hash ≠ SHA-256(stored text) (SQL) / stored text not `for_storage` (Python) | zero the hash / store a zero-width space with a correct hash | 0 / 0 |
| RR-08 | chunk disagreeing with its parents | `session_replication_role=replica`, re-tenant a chunk | 0 |
| RR-10 | original named but not in the store (sampled `exists()`, never a bucket listing — ADR-087) | name a key the store lacks | n/a (no originals in pre-stage data) |
| RR-11 | document past `valid_until` returned | a query property, proved by `test_a_document_outside_its_window_is_never_retrieved` | — |

RR-09 (Part 3) and RR-12 (Part 2) are reserved. RR-13 is the Word residual (§9). The object-without-document half of RR-10 is not checked by design (no bucket listing); the upload path deletes an original whose document was not committed.

## 20. Migrations and Alembic Gates

`0127` extraction warnings and heading path; `0128` generation snapshot columns and `last_enqueued_at`; `0129` `documents.source_key` + partial unique index built `CONCURRENTLY`; `0130` effective dates and supersession (FK and CHECK `NOT VALID`, then validated); `0131` audit action labels (`ADD VALUE` in autocommit; labels cannot be dropped). All nullable-first, no table rewrite, `SET LOCAL lock_timeout` on every DDL that locks these tables.

| Gate | Result |
| --- | --- |
| `alembic heads` | `0131 (head)`, single |
| `alembic check` | "No new upgrade operations detected." |
| fresh `0001 → head` | 9.2 s, `0131 (head)` |
| catalog | 0 `NOT VALID` constraints, 0 invalid indexes |
| `db_preflight verify` | ok (fresh and rehearsal) |
| populated base head → head → 0126 → head (template copy of the rehearsal seed) | exit 0 each step; row count + MD5 of every original column identical after every step |
| refusals (`tests/integration/test_knowledge_migrations.py`) | 0131 (lifecycle audit rows), 0130 (effective dates), 0129 (source keys), 0128 (in-flight replacement snapshot), 0127 (extraction warnings) each refuse naming what it would discard; then 0126, then head again |
| downgrade on the rehearsed database | refused at 0129 (34 source keys) |
| a refusal below 0126 rolls the whole run back (atomicity suite) | failed in the full lane (RR-14), fixed, passing |

## 21. API Compatibility

Additive only (247 → 252 operations): `PUT /knowledge/documents/{id}/content`, `GET /knowledge/documents`, `GET /knowledge/documents/{id}/similar`, `PATCH` and `DELETE /knowledge/bases/{id}`; `?replace=true` on upload; cursor + filters on the existing lists (`X-Next-Cursor`). `DocumentRead` adds `source_key`, `extraction_warnings`, `valid_from`, `valid_until`, `supersedes_document_id`, `original_available`, `metadata_updated`; `AgentRead` adds `knowledge_unreachable`; tool output adds `updated_at` and section per source. Stable refusals: `document_source_exists`, `document_exists_in_other_knowledge_base`, `document_content_exists` (409) and the fixed third-search tool message. The base64 JSON upload is unchanged; every existing field and route kept. OpenAPI regenerated by FastAPI; `docs/API.md` updated.

## 22. Ported Probe Tests

| Probe | Test |
| --- | --- |
| P-R01, P-R02 | `tests/unit/test_chunking.py` (+ property tests) |
| P-R03 | `tests/integration/test_arabic_extraction.py` (fixtures from three producers, the real child process asserted) |
| P-R04 | `tests/integration/test_pgvector_version_guard.py` (refusal half only with the old container) + `tests/unit/test_pgvector_version.py` |
| P-R05 | `tests/integration/test_document_replacement.py` |
| P-R07 | `tests/unit/test_text_decoding.py` |
| P-R08 | `tests/integration/test_upload_concurrency.py` |
| P-R09, P-R14 | `tests/integration/test_reindex_commands.py` |
| P-R10 | `tests/integration/test_knowledge_pagination.py` |
| P-R11, P-R12 | `tests/unit/test_orchestrator_final_round.py` |
| P-R15, P-R16 | `tests/unit/test_text_normalisation.py`, `tests/unit/test_pdf_page_structure.py` |
| P-R18 | `tests/integration/test_knowledge_reachability.py` |

They run in the existing jobs (unit, integration, migration-built integration); the slow-PDF and concurrency tests take ~10 s each and carry no special marker. P-R06 is ported in Part 2, P-R17 in Part 3. The rebuilt scripts stay untracked under `audit/rag/` and are copied to `E:\wasla-rag-notes\probes\`.

## 23. Mutation Matrix

Strict runner (`audit/rag/run_rag_mutations.py`, the protocol of earlier stages): exact anchors, named killer tests, control pass first (101 then 148 killer tests green), every file restored and SHA-256 verified; run in the disposable worktree `E:\wasla-rg1-mut`.

| Mutant | Verdict | Killer |
| --- | --- | --- |
| M-R01 for_storage without NFKC | KILLED | `test_presentation_forms_are_stored_as_the_letters_a_customer_types` |
| M-R02 query not through for_matching | KILLED | `test_a_query_typed_in_arabic_indic_digits_finds_ascii_digits` |
| M-R03 Arabic-Indic digit map removed | KILLED | `test_the_digit_map_round_trips_every_digit_in_both_scripts` |
| M-R04 bidi controls kept | KILLED | `test_storage_removes_every_invisible_control_and_nul` |
| M-R05 hash over the raw upload | KILLED | `test_an_upload_stores_normalised_text_hashed_as_stored` (added: no test covered it) |
| M-R06 newline not a boundary | KILLED | `test_a_price_list_without_full_stops_keeps_every_price_whole` |
| M-R07 cut inside a digit run | KILLED after a test fix | `test_a_single_unbroken_token_is_cut_but_never_inside_a_digit_run[1700]` |
| M-R08 overlap joined with `\n\n` | KILLED | `test_every_chunker_property_holds[ar-policy-0.txt]` |
| M-R09 minimum not applied to the piece | KILLED after a test fix | `test_no_piece_shorter_than_the_minimum_stands_alone[False-19]` |
| M-R10 breadcrumb dropped | KILLED | `test_markdown_headings_are_structure_with_breadcrumbs` |
| M-R11 furniture removal disabled | KILLED | `test_running_headers_footers_and_page_numbers_are_dropped` |
| M-R12 claim reads `documents.content` | KILLED | `test_a_replacement_serves_v1_until_the_swap_and_only_v2_after` |
| M-R13 publish does not copy the snapshot | KILLED | same |
| M-R14 older attempt not cancelled | KILLED | `test_two_replacements_in_a_row_leave_one_attempt_and_the_newest_wins` |
| M-R15 replacement consumes a slot | KILLED | `test_a_replacement_needs_no_free_plan_slot` |
| M-R16 same-hash PUT makes a generation | KILLED | `test_the_same_text_updates_metadata_only` |
| M-R17 source key not checked | KILLED | `test_the_same_file_uploaded_twice_is_refused_or_replaced` |
| M-R18 cross-KB dedupe removed | KILLED | `test_the_same_text_in_another_knowledge_base_is_refused` |
| M-R19 dedupe lookup made global | KILLED | `test_source_keys_and_dedupe_look_only_inside_the_workspace` |
| M-R20 retrieval dedupe removed | KILLED | `test_a_legacy_copy_is_served_once` |
| M-R21 list cursor ignored | KILLED after a test fix | `test_250_documents_are_all_listed_by_cursor_once_each` |
| M-R22 lock taken before extraction and held across it | KILLED | `test_two_uploads_to_one_workspace_succeed_while_a_slow_pdf_parses` |
| M-R23 resubmission ignores the title | KILLED | `test_resubmitting_identical_text_updates_its_title` |
| M-R24 `list_stale` without its exclusion | KILLED | `test_reindex_stale_embeddings_twice_selects_disjoint_documents` |
| M-R25 sweep re-enqueues within the window | KILLED | `test_the_recovery_sweep_does_not_grow_the_queue_during_a_backlog` |
| M-R26 last round offers tools | KILLED | `test_a_turn_that_keeps_searching_still_ends_in_a_reply` |
| M-R27 tool results after all turns | KILLED | `test_each_round_replays_the_previous_round_in_order` |
| M-R28 search cap removed | KILLED | `test_a_third_search_in_one_turn_is_refused_and_other_tools_run` |
| M-R29 new agents without the grant | KILLED | `test_a_new_agent_can_search_knowledge_and_nothing_else` |
| M-R36 validity window not applied | KILLED | `test_a_document_outside_its_window_is_never_retrieved` |
| M-R41 version compared as strings | KILLED | `test_a_supported_pgvector_passes[0.10.0]` |
| M-R42 purge keeps originals | KILLED | `test_a_purge_deletes_the_workspaces_knowledge_originals` |

**32 / 32 killed; meaningful survivors 0.** What the matrix found and fixed: M-R07 survived because the digit run sat exactly where a cut by length lands (the test now runs at three leads); M-R09 survived because no test forced a short piece (a short section before an over-long one now does); M-R05 had no killer (test added); M-R21 hung instead of failing (the cursor walk is now bounded — the first run was stopped, its pytest child killed, and the mutant file restored with `git checkout` in the disposable worktree). M-R22 as first written (lock taken early only) **survived as an equivalent mutant**: the upload's connection release before parsing commits, which releases that advisory lock; the mutant was rewritten to the real regression (lock taken and the connection kept across the parse) and is killed. M-R04 and M-R14 were first INVALID (anchor written with literal zero-width characters; an anchor present twice) and were corrected. M-R30 .. M-R35, M-R37, M-R38, M-R44, M-R45 run in Part 2; M-R39, M-R40, M-R43 in Part 3. `git status` under `app/` in `E:\wasla-rg1-mut` is clean.

## 24. Test Non-Vacuity

- RV-01: the asserted text is `documents.content` read back from PostgreSQL after submit, and the test asserts the extraction child process ran.
- RV-02: passages come from the real repository search with both versions having been ready; the test fails at `7cdf863` (no `PUT` route; the upload path makes two documents).
- RV-05/06: `test_the_chunker_under_test_is_this_worktrees` asserts `__file__`.
- RV-10: the slow extraction is real time inside the real child (test seam `slow_pdf_child.py`), the second upload concurrent; the pool's checked-out count is sampled mid-parse.
- RV-14/27: the order asserted is the JSON handed to `httpx.MockTransport`.
- RQ-08: the duplicate lives in another KB of the same workspace **and** an identical document exists in another workspace, which triggers nothing (`test_an_identical_document_in_another_workspace_triggers_nothing`).
- Every Phase 7 and 8 test was run against the stashed fix (base code) and failed: RV-12 (2, 2, 2), RV-18 ([4, 8, 12]), RV-14 (preamble sent / empty turn), RV-27 (prefix broken), RC-7 (no grant / no method).

## 25. Model-Built Test Results

Model-built schema, PostgreSQL 16 / pgvector **0.8.6**, `TEST_DATABASE_URL=…/wasla_rg1_models`, Redis DB 3, at `9395dc9`:

| collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- |
| 7,698 | 7,661 | 8 | 29 | 0 | 0 | 56 min |

The 8 failures:

- `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` — the known canonical date bomb, **deferred by the user to audit #24**, not touched.
- 7 migration downgrade tests (`test_migration_downgrade_atomicity.py` ×4, `test_entitlement_migrations.py` ×2, `test_plan_price_migration.py` ×1) — **caused by this Part, and fixed** (RR-14): a downgrade refused below 0126 left the database stamped at 0126. Migration 0126 (an earlier stage) and this Part's 0129 dropped an index `CONCURRENTLY` in an autocommit block on downgrade, committing every step above them. 0126's was latent while it was head. Both now drop inside the run's transaction (`ba34fe1`). Re-run of those five migration suites plus `test_knowledge_migrations.py` and `test_migration_recovery.py`: **11 passed, 0 failed**. My own refusal test had not caught it because it never refused *below* 0126.

Skips (29), each pre-existing or opt-in: 11 real-provider contract tests (no key), 4 schema-parity (model lane), 5 Windows signal-path tests (Linux CI), 4 Google OAuth concurrency (real-Redis URL variable), 2 invariants sweeps that need kept data, 1 tmpfs, and **2 new**: `test_pgvector_version_guard.py`'s old-pgvector half (opt-in `WASLA_TEST_OLD_PGVECTOR_URL`; run separately against `wasla-rg1-pg-old`: refused with the 0.8.0 message).

## 26. Migration-Built Test Results

`WASLA_TEST_SCHEMA=migrations`, a fresh `alembic upgrade head` to `0131` on `wasla_rg1_migrations`, PostgreSQL 16 / pgvector **0.8.6**, at `ba34fe1` (after the RR-14 fix):

| collected | passed | failed | skipped | xfailed | xpassed | time |
| --- | --- | --- | --- | --- | --- | --- |
| 7,698 | 7,671 | 2 | 25 | 0 | 0 | 55 min |

- `test_financial_integrity.py::test_a_topup_is_not_granted_on_an_unpaid_invoice` — the known date bomb, deferred to audit #24.
- `test_upload_concurrency.py::test_a_burst_of_slow_uploads_does_not_exhaust_the_pool` — this Part's test, intermittent: it sampled the pool from the first instant, when every upload of the burst briefly holds a connection for its own setup reads (peak 7 of 5; no upload was refused). It now samples the parse window, the property it claims (`bfe1682`). Six runs before the change (3 per schema) and three after: all green. M-R22 still killed by the suite.

Skips (25): 11 real-provider, 5 Windows signal path, 4 OAuth concurrency (real-Redis variable), 2 kept-data sweeps (run separately below), 1 tmpfs, and the 2 opt-in old-pgvector tests (run against `wasla-rg1-pg-old`: 3 passed). Schema parity (4 tests) ran and passed here.

## 27. CI Jobs Run Locally

| `ci.yml` job | Run locally | Result |
| --- | --- | --- |
| `quality` — Ruff, Black, MyPy | as in CI, on the tracked tree | clean (§28) |
| `tests` — unit, integration, e2e on the model-built schema | the full model-built lane | §25; the 7 RR-14 failures fixed and re-run green; date bomb deferred |
| `migration-parity` — enums agree, parity not skipped, every integration and e2e suite on the migrated schema | the full migration-built lane | §26; parity ran |
| `migration-parity` — AI and tool invariants over kept data | the exact step (`WASLA_TEST_KEEP_AI_DATA=1`, the harness-file selection, fail on any skip) | first run **305 passed, 2 skipped** — the skip would have failed CI (this Part's guard test named the AI harness); fixed (`2b03a5b`), re-run **304 passed, 0 skipped** |
| `monitoring` — promtool config, rule tests, alertmanager | promtool config and rule tests in `prom/prometheus:v3.1.0` | SUCCESS (§29); alertmanager config unchanged, not re-run |
| `docker` — image build, liveness | not run | no Dockerfile or dependency change in this Part (only `docker-compose.yml`'s dev pgvector pin) |
| `security.yml`, `deploy.yml` | not run | no dependency change; deploy is out of scope |

Known deferred failures named beside every lane: the date bomb in `test_financial_integrity.py` (canonical CI red since 2026-10-27), FRM-05, FRM-07 (`test_pdf_extraction_bounds`' 2.0 s bound — passed in both lanes here and was not loosened, RQ-29), CPM-06 — all deferred to audit #24.

## 28. Ruff / Black / MyPy

Tracked tree: `ruff check . --exclude audit` → All checks passed; `black --check . --exclude audit` → 1051 files unchanged; `mypy app tests` → no issues in 929 source files. (The untracked probe scripts under `audit/rag/` are not held to the gates.)

## 29. Prometheus / Alertmanager Gates

`promtool check config` (prometheus:v3.1.0): SUCCESS, 84 rules. `promtool test rules tests/alerts_test.yml`: SUCCESS, including the three new knowledge alerts. Metric catalogue test: green.

## 30. Evaluation Results

Fake embeddings — **structural**, 261 answerable questions of 267. Baseline: three identical runs.

| Metric (fake embeddings) | Baseline | After P4 | After P6 |
| --- | --- | --- | --- |
| Documents ingested / refused | 37 / 0 | 37 / 0 | 36 / 1 (the cross-KB copy, now refused) |
| Chunks | 116 | 101 | 97 |
| Labelled spans intact in some chunk | 250 / 261 | 255 / 261 | 255 / 261 |
| Span in top 5 (threshold lifted) | 225 / 261 | 241 / 261 | 241 / 261 |
| Span at rank 1 (threshold lifted) | 152 / 261 | 193 / 261 | 193 / 261 |
| Questions with a stale version in the top results | 1 / 1 | 1 / 1 | **0 / 1** |
| Duplicate passages per search | 0.094 | 0.007 | **0** |
| Answerable-but-empty at 0.75 (structural) | 6 / 261 | 6 / 261 | 6 / 261 |
| Arabic PDF questions answered, Chromium / WeasyPrint / Word | 0/3, 1/3, 2/3 | 2/3, 1/3, 2/3 | 2/3, 1/3, 2/3 |
| Numbers cut across the corpus | 9 | 0 | 0 |
| Words cut across the corpus | 68 | 0 | 0 |
| Headed sections without their heading | 9 | 0 | 0 |
| Chunks holding Arabic presentation forms | 2 | 0 | 0 |

The 6 answerable-but-empty questions are the AR-2 prices two producers' PDFs reverse or drop (§9). Phases 7 and 8 change no ingested text. The final runs at the Part's last code commit (`final-run1..3`) are byte-identical to each other and to the after-P6 table.

**Real-provider metrics — EXTERNAL VERIFICATION, not run:**

| Metric | MSA | Egyptian | English | Cross-lingual |
| --- | --- | --- | --- | --- |
| recall@5 | | | | |
| MRR@10 | | | | |
| nDCG@5 | | | | |
| answerable-but-empty | | | | |

## 31. Findings Ledger

### Root causes

| ID | Status |
| --- | --- |
| RC-1 normalisation | CLOSED (Part 1) |
| RC-2 lifecycle | CLOSED (Part 1) |
| RC-3 chunking | CLOSED (Part 1) |
| RC-4 single-stage retrieval | DEFERRED TO PART 2 |
| RC-5 query formulation | DEFERRED TO PART 2 |
| RC-6 scope | DEFERRED TO PART 2 |
| RC-7 fragile triggers | CLOSED (default grant + `knowledge_unreachable`) |

### Findings

| ID | Status |
| --- | --- |
| RV-01 | CLOSED — new uploads; stored documents repaired by back-fill + `reindex-all` (rehearsed, RR-06 = 0) |
| RV-02 | CLOSED |
| RV-03 | CLOSED (guard); production version EXTERNAL VERIFICATION at first deploy |
| RV-04 | CLOSED as detection + warning; repair of documents uploaded before originals were kept — ACCEPTED RESIDUAL (re-upload list from the back-fill) |
| RV-05 .. RV-10 | CLOSED |
| RV-11 | DEFERRED TO PART 3 |
| RV-12, RV-13, RV-14 | CLOSED |
| RV-15 | DEFERRED TO PART 2 |
| RV-16, RV-17, RV-18 | CLOSED |
| RV-19 | DEFERRED TO PART 2 |
| RV-20 | CLOSED |
| RV-21 | DEFERRED TO PART 2 |
| RV-22 | CLOSED for Chromium and WeasyPrint; Word two-logical-pages layout — ACCEPTED RESIDUAL (RR-13) |
| RV-23 | CLOSED |
| RV-24 | the five rows of this Part CLOSED; the rest DEFERRED TO PART 2 |
| RV-25 | DEFERRED TO PART 2 (documentation; scale behaviour SCALE-TRIGGERED) |
| RV-26 | telemetry CLOSED; calibration EXTERNAL VERIFICATION |
| RV-27 | CLOSED |

### Fix plan

| ID | Status |
| --- | --- |
| FX-0.1 | CLOSED (dev image pinned; production version EXTERNAL VERIFICATION) |
| FX-0.2 .. FX-0.4 | CLOSED as the triage command (no production data exists) |
| FX-0.5 | CLOSED (RUNBOOK) |
| FX-1.1 .. FX-1.13 | CLOSED |
| FX-2.1 .. FX-2.7 | CLOSED |
| FX-3.1 | CLOSED (fake-embedding harness); real-provider run EXTERNAL VERIFICATION |
| FX-3.2 .. FX-3.6, FX-3.8, FX-3.9, FX-3.11 .. FX-3.16 | DEFERRED TO PART 2 |
| FX-3.7 | EXTERNAL VERIFICATION (embedding model trial) |
| FX-3.10 | CLOSED |
| FX-3.17 | DEFERRED TO PART 3 |
| FX-4.1 .. FX-4.10 | DEFERRED TO PART 3 (FX-4.4's duplicate and unreachable gauges and alerts already built here) |

### Improvements

| ID | Delivered by | Status | Setting (default) |
| --- | --- | --- | --- |
| IMP-01 | FX-3.1 | CLOSED (structural); real metrics EXTERNAL VERIFICATION | — |
| IMP-02 | FX-1.13 | CLOSED | — |
| IMP-03 | FX-1.1 | CLOSED | always on |
| IMP-04 | FX-1.2 | CLOSED | chunk constants |
| IMP-05 | FX-3.2 | DEFERRED TO PART 2 (`heading_path` stored for it) | — |
| IMP-06 | FX-2.1 .. FX-2.3 | CLOSED | — |
| IMP-07 | FX-3.17 | DEFERRED TO PART 3 (FAQ pairs for text and Markdown already kept whole here) | — |
| IMP-08, IMP-09, IMP-10, IMP-11 | FX-3.3, 3.5, 3.4, 3.6 | DEFERRED TO PART 2 | — |
| IMP-12 | FX-3.7 | EXTERNAL VERIFICATION | — |
| IMP-13 | FX-1.10 | CLOSED | `MAX_KNOWLEDGE_SEARCHES_PER_TURN=2` |
| IMP-14 .. IMP-18 | FX-3.13, 3.11, 3.12, 3.14, 3.15 | DEFERRED TO PART 2 | — |
| IMP-19 .. IMP-21 | FX-4.6 .. 4.8 | DEFERRED TO PART 3 | — |
| IMP-22 | FX-3.16 | chronological rounds and stable prefix CLOSED here; compact context DEFERRED TO PART 2 | — |
| IMP-23 | FX-1.12 | CLOSED | — |
| IMP-24, IMP-25 | FX-4.9, 4.10 | DEFERRED TO PART 3 | — |
| IMP-26 | FX-3.9 | DEFERRED TO PART 2 | — |

### Decisions and remediation items

| ID | Status |
| --- | --- |
| RQ-01 .. RQ-11, RQ-23, RQ-24, RQ-29 | DECISION APPLIED |
| RQ-12 .. RQ-20 | DEFERRED TO PART 2 |
| RQ-21, RQ-22, RQ-25 .. RQ-28 | DEFERRED TO PART 3 |
| OCR, PyMuPDF (AGPL), larger upload limits | DEFERRED PRODUCT DECISION |
| Multi-agent routing | DEFERRED — LATER PRODUCT |
| RR-01 .. RR-08, RR-10, RR-11 | CLOSED (checks built, 0 on the rehearsed database) |
| RR-09 / RR-12 | reserved for Part 3 / Part 2 |
| RR-13 Word export puts two logical pages in one PDF page; inner furniture kept | ACCEPTED RESIDUAL (Low) |
| RR-14 0126's and 0129's downgrades committed mid-run (`CONCURRENTLY` in an autocommit block), stranding the stamp when a lower migration refused | CLOSED (`ba34fe1`; found by the full lane) |
| Known deferred failures (date bomb in `test_financial_integrity.py`, FRM-05, FRM-07, CPM-06) | DEFERRED to audit #24 by the user |

## 32. Handover to Part 2

- Branch from this Part's final commit on `rag-remediation-p1-lifecycle-20261029` (`git log -1` in §34).
- Probes: `E:\wasla-rag-notes\probes\` (copy of `audit/rag/`, including the rehearsal scripts and the mutation runner); eval baselines in `…\probes\eval-runs\` — keep the harness table format and append columns.
- Built for Part 2: `for_lexical` (lexical channel), `heading_path` on chunks (contextual prefix), distance histogram (tiers), `wasla_rag_passages_dropped_total` reasons to extend, the isolation-gate harness (one fixture per new path).
- Open for Part 2: RV-15, RV-19, RV-21, RV-25 docs, RC-4/5/6, FX-3.2 .. FX-3.16 (not 3.7/3.10), the §8 rows listed in §17.

## 33. Remaining External Actions

Not performed:

1. Part 2, then Part 3, each from the previous Part's final commit.
2. After Part 3: canonical merge verification of the stacked branch (its own prompt) → merge into `worktree-billing-google-auth` → CI → push.
3. First deploy: `db_preflight` confirms pgvector ≥ 0.8; `backfill-normalised-content`; `backfill-source-keys`; act on the listed collisions; `reindex-all` at a chosen rate; `rag_invariants verify`.
4. Real Arabic PDFs from the user's own tools (Word, Google Docs, Canva, printers) through the producer table.
5. Real-provider evaluation, when authorised, after Part 2.
6. Product decisions: OCR, PyMuPDF licence, larger upload limits; multi-agent routing as its own stage.
7. Keep this Part's containers and worktrees until the merge verification; then the infrastructure cleanup prompt.

External verification items (never CLOSED here): retrieval quality with the real model; the 0.75 threshold and every calibrated cut-off; FX-3.7; the production pgvector version; customer PDFs beyond these three producers; `prompt_cache_key` behaviour and cached-token share in real traffic.

## 34. Infrastructure Created by This Part

**Created by this Part, state at the end:**

| Kind | Name | State |
| --- | --- | --- |
| Worktree / branch | `E:\wasla-rag-p1` on `rag-remediation-p1-lifecycle-20261029` | kept for review; tracked tree clean apart from this report until it is committed; `audit/` untracked |
| Worktree | `E:\wasla-rg1-base` (detached at `7cdf863`) | kept: seeds the rehearsal with base-head code |
| Worktree | `E:\wasla-rg1-mut` (detached at `ba34fe1`) | kept; `git status` clean (no mutant applied) |
| Containers | `wasla-rg1-pg` (55961, pgvector 0.8.6), `wasla-rg1-pg-old` (55962, 0.7.4), `wasla-rg1-redis` (56961), `wasla-rg1-minio` (59961) | running |
| Databases on `wasla-rg1-pg` | `wasla_rg1_models`, `wasla_rg1_migrations`, `wasla_rg1_mut`, `wasla_rg1_fresh`, `wasla_rg1_mig` (rehearsed), `wasla_rg1_mig_copy` (pristine seed, template), `wasla_rg1_rt` (round trip); per-test migration databases are dropped by their tests | kept |
| Database on `wasla-rg1-pg-old` | `wasla_rg1_old` | kept |
| Redis DBs | 3 (models), 4 (mutation), 5 (seed), 6 (rehearsal) of `wasla-rg1-redis` | kept |
| Bucket | `wasla-rg1-media` on `wasla-rg1-minio` | kept |
| Scratch | `E:\wasla-rag-p1\audit\rag\` (untracked), copied to `E:\wasla-rag-notes\probes\` | kept |

**Other stages' infrastructure found:** worktrees `E:\wasla-campaign-features`, `-campaign-media`, `-campaigns-audit`, `-campaigns-remediation`, `-cpf-base`, `-cpf-lane`, `-cpf-lane2`, `-cpf-mut`, `-followups-realtime`, `-frm-report`, `-fur-base`, `-fur-lane`, `-fur-lane2`, `-fur-mut`, `-ntf-audit` — not touched. No other stage's container was running. `E:\wasla` and its untracked documents not touched.

## 35. Score

A **self-score** by this stage, on a scale first used here; the review gave no number. Not an independent measurement.

| Area | Max | Base `7cdf863` | Part 1 | Part 1 deductions |
| --- | --- | --- | --- | --- |
| Extraction and text fidelity | 8 | 3 | 6 | RV-04 repair only by re-upload (−1), RR-13 (−1) |
| Unicode and Arabic normalisation | 8 | 1 | 8 | — |
| Chunking | 10 | 3 | 9 | CSV/XLSX rows still generic (Part 3, −1) |
| Embedding input and space handling | 5 | 4 | 4 | contextual prefix (Part 2), shadow switch (Part 3) |
| Document lifecycle | 12 | 3 | 12 | — |
| Dedupe and identity | 5 | 1 | 5 | — |
| Ranking | 8 | 3 | 4 | hybrid, re-rank, neighbours (Part 2) |
| Query formulation | 5 | 2 | 2.5 | standalone/bilingual queries (Part 2) |
| Scope and filters | 4 | 1 | 2 | per-agent scope (Part 2) |
| Context assembly and budget | 5 | 2 | 2.5 | RV-15, RV-19 (Part 2) |
| Tool loop and answer grounding | 7 | 3 | 5 | grounding guard, tiers (Part 2) |
| Tenant isolation | 8 | 7 | 8 | — |
| Re-index and operations | 3 | 1 | 3 | — |
| Observability | 3 | 1 | 3 | — |
| Testing and evaluation | 5 | 2 | 4.5 | semantic eval is external (−0.5) |
| Documentation truth | 2 | 1 | 1.5 | Part 2 rows of §8 (−0.5) |
| Real-provider evidence | 2 | 0 | 0 | not authorised |
| **Total** | **100** | **38** | **80** | |

Deductions for this Part's own open items: RV-04 repair (−1), RR-13 (−1) — **2 points**. All other deductions are items DEFERRED TO PART 2 / PART 3 or external verification.

## 36. Verdict

**RAG PART 1 (TEXT, CHUNKING & LIFECYCLE) CLOSED WITH REQUIRED PRODUCT DECISIONS AND EXTERNAL VERIFICATION**

Against §50 of the prompt: RV-01 (0 presentation forms for every producer tested, the phrase found by a typed query, `٤٥٠٠` = `4500`, RR-06 = 0 after the rehearsal) ✓; RV-02 (one version at every moment; failed replacement keeps v1; P-R05 returns 7900 only after the swap) ✓; RV-07 ✓; RV-05/06/08/09 properties over the corpus, A.1 and A.2 ✓; RV-03 refusal ✓; RV-10, RV-12, RV-13, RV-14 each with a test that failed at the base ✓; RC-7 ✓; isolation gate passed, M-R18 and M-R19 killed ✓; the review's and RAG_FINDINGS_REMEDIATION's guarantees unchanged (§18) ✓; all meaningful mutants killed ✓; both lanes green apart from the named deferred failures (§25, §26) ✓; RR invariants 0 ✓; harness deterministic with a recorded baseline ✓; no real provider ✓; nothing pushed, merged or deployed ✓.

What is left: Parts 2 and 3, the product decisions (OCR, PyMuPDF, upload limits), the external verifications of §33, and two Low residuals of this Part (RV-04 repair of pre-stage documents by re-upload; RR-13).

**The final question.** A workspace uploads an Arabic price list exported from Word, then the same list with one price changed. Wasla can now prove that the passage was stored as ordinary Arabic letters (RR-06 = 0; 0 presentation-form code points stored for Word's AR-1/AR-2), that no number was cut (numbers cut = 0 across the corpus, a property test), that only the old price was retrievable until the swap and only the new one after it (the replacement test at the provider gate, RR-01), and that nothing from another workspace could have been in the context (the triple tenant predicate, the isolation gate and M-R19). One honest limit: for Word's AR-2 export, 2 of 6 price lines come out of pypdf re-ordered — Wasla flags them on the document rather than serving them silently as correct, and the fix is re-uploading from a different export.
