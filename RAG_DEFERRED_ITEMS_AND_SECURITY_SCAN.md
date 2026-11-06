# RAG Deferred Items, Advisories and the Scan from the Lock

Stage: RGM-16 (index jobs after commit), RGM-13 (re-ordered Arabic line detector), RGM-21 (security.yml from the lock),
the lock's advisories (DH), RGM-17 / RGM-25 (tie-break), the hardened tests (RGM-19, RGM-18, FRM-07, CPM-06, RGM-02).
ADR-188. Date: 2026-11-06 (UTC; Cairo = UTC+2). Evidence: `E:\wasla-rdi-notes\` (logs, evidence, proof, mutation, e2e, upgrade).

## Phase status

| Phase | State | UTC | Commit / push / dispatch |
|---|---|---|---|
| A0 state, guard, wall, canary, infrastructure | done | 05:13–05:50 | branch rag-deferred-items-20261106 from b5aa170 |
| A1–A9 reproductions, fixes, bumps, tests, docs | done | 05:50–10:40 | ba45b54 … 277b716 (13 commits) |
| A10 diff guard and Part A gates | done, green | 10:40 | **RDI_HEAD = 277b716** |
| B: populated upgrade, D-1..D-12, CI jobs locally (both workflows), Linux, eval ×3, races, probes, APP-E2E K-1..K-12 | done; see §8 / §21 for the two recorded reds | 10:47–14:36 | — |
| B: time-shifted lane (+100 days, five moments) | done, every failure classified | 12:32–14:36 | — |
| B: kept-data ×3; both full lanes together inside quiet hours; hour selection inside | done, green | 12:52–21:28 | — |
| B: mutation spot-check after the lanes | done: 79 killed, 0 hung | 21:30–21:46 | — |
| B: merge | done, tree equal | 21:47 | **1ff78e7** (parents b5aa170, 277b716) |
| B: gates on the final tree | done, green (RGM-23 recorded) | 21:48–22:56 | — |
| B: report commit, push, ci.yml, security.yml dispatch, addendum | see §30–31 | | |
| C: cleanup | runs only after the addendum's run is green | | |

## 1. Executive Summary

- **RGM-16 closed.** Indexing jobs are now published only after the transaction that created the generation commits (`app/db/after_commit.py`: `defer()` releases on the outermost commit, a rollback drops it). The existing IngestionRecoveryWorker (300 s grace) re-publishes a job lost between the commit and the publish, once.
  - At b5aa170, 13 of the 16 new tests fail in every one of 10 runs. At RDI_HEAD they pass 16 / 16 in every run.
  - D-11, K-12 and the races show 0 "not found" failures.
- **RGM-13 closed.** The detector flags a digit touching an Arabic letter directly or through a full stop. That catches the Chromium price line under pypdf 6.19.0 and 6.21.4, with 0 false positives on the corpus.
- **RGM-21 closed locally.** security.yml now:
  - installs `requirements.lock` + `requirements-build.lock` by hash;
  - runs the drift guard and pip check;
  - audits the lock with pip-audit 2.10.1 in its own environment.
  OLD vs NEW are proven different: OLD resolves 36 of 64 packages away from the lock, sees 1 advisory and misses the lock's 24.
- **Advisories: 24 → 0 on the lock.** Bumped: certifi 2026.10.23, dnspython 2.9.0, pyjwt 2.15.1, opentelemetry 1.45.1 / 0.66b1, pypdf 6.21.4. Each bump was a lock-only commit made with the lock's own tool.
  - pypdf 6.21.4 changes Arabic extraction, so an identical Arabic PDF uploaded before the bump is no longer recognised as a repeat (**RGM-22**). The owner accepted this (2026-11-06) as an external action before the first real deploy.
- **RGM-17 / RGM-25 closed.** RRF ties and equidistant vector rows are ordered by a key stable across ingests. Non-tied orders are unchanged.
  - Evaluation Part 1 and Part 3 are identical ×3. Part 2 keeps ±1 on one cell (RK-03 PARTIAL, explained in §10).
- **Hardened tests:**
  - RGM-02 and RGM-19 are fixed (test-only).
  - CPM-06 was already closed.
  - RGM-18 and FRM-07 are left unchanged under TB-04. RGM-26 records the Windows-only loop stall.
- **Two reds recorded, not fixed (owner's decision, 2026-11-06):** security.yml's environment audit (black 26.5.1, **RGM-23**) and its Secret scan (gitleaks, **RGM-24**).
- **One load failure recorded:** RGM-18's loop-latency test failed once in the local migration-parity job while the machine ran four other suites. On its own it passed 15 / 15 in each of 6 runs, and both full lanes later passed it.
- **Mutations:** 79 / 79 killed, 0 hung (M-R34 equivalent). Merged tree equals RDI_HEAD.

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| ORIGIN_CANONICAL_START_HEAD (origin/worktree-billing-google-auth at start) | b5aa1701b699980406f836cc3063eafad556799c |
| RDI_HEAD (branch rag-deferred-items-20261106) | 277b7167bd53a12513010802f46b4d4c1a0a56a2 |
| CANONICAL_MERGED_HEAD | 1ff78e73d05be4f91ff2a6ed8128299332e2ea13 (parents b5aa170, 277b716); tree 8906e2a4 = RDI_HEAD's tree |
| main | 8e3a1ba (untouched) |
| alembic head | 0138, single head; no migration added or changed |

## 3. Infrastructure and the Guard

- **Network** wasla-rdi-net. All containers are disposable:
  - wasla-rdi-pg (55891), -pg2 (55892), -pg3 (55893), all pgvector/pgvector:pg16, pgvector 0.8.6;
  - -pg-old (55894, pgvector 0.7.4);
  - -redis / -redis2 / -redis3 (57891–57893) and -redis4 / -redis5 (Linux-side);
  - -minio (59891, bucket wasla-media);
  - wasla-rdi-linux (python:3.12-slim, Python 3.12.14).
- **Worktrees:** E:\wasla-rdi-fix (the branch), -base (b5aa170), -verify, -lane, -mut and -final (all detached).
- **Venvs:** venv-start (b5aa170's lock, pypdf 6.19.0), venv-head (RDI_HEAD's lock minus uvloop on Windows, pypdf 6.21.4), venv-shift (+time-machine 2.16.0). Linux: /opt/venv-head, plus /opt/job-* built by each job's own steps.
- **Scripts:**
  - The rescued scripts were copied from E:\wasla-verification-scripts\rag-merge and read in full. Hashes are in evidence/scripts_source.sha256 and scripts_copies.sha256.
  - The wall's allow-list changed only wasla-rgm- to wasla-rdi- (evidence/wall_allowlist.diff).
- **DG-01..DG-07:**
  - Dead proxy 127.0.0.1:9 for every application process. NO_PROXY: 127.0.0.1, localhost, and on Linux the container IPs 172.20.0.2–.12 and names.
  - No OpenAI key. sitecustomize wall loaded first in every app process (each log's first line).
  - Index processes ran with index.env and never imported app.
- **Canary:** at A0, at the start of Part B, and immediately before the E2E (Windows and Linux). Every client was refused: bare httpx at the proxy, then build_guarded_client, Responses, WhatsApp and the admin preview at the wall (Linux: 11 refusals).
  - **Real-provider responses in the whole stage: 0.** DG refusals logged during the E2E and canary: 7. Linux selections: 0.

## 4. Decisions Applied

| Group | Applied |
|---|---|
| EQ-01..EQ-08 | publish after the outermost commit (defer / run_committed); the existing IngestionRecoveryWorker is EQ-02's sweep (grace 300 s, lease and already-queued checks, workspace-scoped); EQ-06 inventory: knowledge upload (JSON, multipart), replace, reindex route, reindex-all; other subsystems listed as recommendations (§36) |
| PD-01..PD-06 | signal `[0-9]\.?{L}|{L}\.?[0-9]` (a digit touching an Arabic letter directly or through a full stop); the 17-PDF corpus captured under 6.19.0 / 6.21.4 (extracted_lines.json); mutants M-D01/02 |
| SY-01..SY-07 | security.yml dependencies job only: lock by hash, drift guard, pip check, version print, pip-audit 2.10.1 in its own venv on the lock (`-r`, `--require-hashes`, `--disable-pip`), environment audit `if: always()`; no SY-03 ignore needed (0 advisories) |
| DH-01..DH-10 | one lock-only commit per family with `uv pip compile … --generate-hashes --upgrade-package`; forced moves recorded (otel exporter-http-transport, exporter-otlp-common 0.66b1); DH-03's 14-day soak **waived for pypdf 6.21.4 by the owner** (the only release fixing all 18) |
| RK-01..RK-04 | RK-01 applied (stable_key + by_distance after the fetch, SQL unchanged); RK-02 non-tied orders unchanged; RK-03 PARTIAL (±1, §10); RK-04 mutants M-K01/02 |
| PL-01..PL-03 | PL-02 not reproduced (33 shapes); PL-01 applied (own rows + ANALYZE); PL-03 = M-T01 |
| TB-01..TB-06 | TB-02 load (12 and 36 processes); TB-04: RGM-18 and FRM-07 left unchanged with reasons; CPM-06 already closed |
| DT / HB | RGM-02 compared with the database's stamp; past-midnight plugin proof |

## 5. RGM-16: Reproduction

- **Before:** b5aa170 `knowledge_service.py` published (`await self._publish(...)`) inside the request's transaction, and CommittingRoute committed after the handler returned.
- **Claim window:** a polling worker claimed the job while the upload's transaction was open. It failed "not found" (TenantIsolationError) and retried about 3 s later (P-Q01 before: 1 pending, 1 claim while held, 1 failure, retry, active after 1.23 s).
- **Rollback:** a request failing after the service ran left an orphan job (Q-3 queued = 1).
- **Q-series at b5aa170:** 13 failed / 3 passed in every one of 10 runs (Q-1, Q-2, Q-3, Q-4, Q-8 ×9). Q-5..Q-7 passed because the sweep already existed.

## 6. RGM-16: The Fix and the Sweep

- **Files:** app/db/after_commit.py (new), app/db/session.py, app/api/route.py, app/services/knowledge_service.py (`defer(self._session, publish)`, line 1180), tests/integration/test_index_job_after_commit.py.
- **Mechanism:** the session's after_commit event releases deferred publishes only for the outermost transaction, and a rollback drops them. CommittingRoute (requests) and Database.session (workers, commands) run them after the commit. A publish failing after the commit is logged and swallowed; the sweep recovers it after the grace period.
- **Latency:** P-Q01 after: 0 pending while open, 0 claims, 0 failures, active 0.11 s after release (before: 1.23 s with a failure and a retry).
- **Q-1..Q-9 (16 tests):**
  - b5aa170: 13 failed every run of 10.
  - RDI_HEAD: 16 / 16 every run of 10 on Windows; Linux 116 / 116 ×3 in the 8-file set; race suite 16 / 16 ×3 alone on its database.
- **End to end:** K-12 (a)(b)(c) PASS. D-11: 0 job failures.

## 7. RGM-13: The Detector

- **Corpus:** 17 PDFs captured under pypdf 6.19.0 and 6.21.4 (tests/fixtures/rag/pdf/extracted_lines.json).
  - Under 6.21.4 the text of chromium-AR-1/-2, weasyprint-AR-1/-2 and word-AR-1 changes.
  - The Chromium price line loses the spacing b5aa170's detector relied on.
- **Signal:** a digit touching an Arabic letter directly or through a full stop.
- **Flagged lines:** exactly the visual-order lines.
  - 6.19.0: chromium-AR-1 1, chromium-AR-2 4.
  - 6.21.4: the same, plus weasyprint-AR-1 1 and weasyprint-AR-2 4.
  - 0 correct lines flagged.
- **P-D01:**
  - b5aa170's detector on 6.21.4: not flagged.
  - RDI_HEAD's detector on 6.21.4: flagged (digits_glued 1).

## 8. RGM-21: security.yml

- **Before:** `pip install ".[dev]" pip-audit`, then `pip-audit` of that environment, which resolves the newest releases that day.
- **After (97bec9f, plus the shape test 277b716):** lock + build lock by `--require-hashes`, `-e ".[dev]"`, drift guard, pip check, version print, pip-audit 2.10.1 in its own venv over the lock, and the environment audit kept with `if: always()`.
- **SY-05 (Linux, b5aa170):**
  - OLD resolved 36 of 64 packages differently from the lock and reported 1 advisory (black, dev).
  - NEW installs the lock exactly (drift 0) and reports the lock's 24.
  - Table: evidence/sy05/sy05_table.txt.
- **Local run at RDI_HEAD (its new steps):**
  - Install ok, drift 66 / 0, pip check ok.
  - **Lock audit: no known vulnerabilities (rc 0).**
  - Environment audit: 1, black 26.5.1 CVE-2026-114134 (fix 26.10.1) → **job red = RGM-23**.
  - Secret scan (gitleaks over history): 26 findings in test fixtures since at least 2026-10-05 → **RGM-24**.
  - Both are left as findings by the owner's decision of 2026-11-06.

## 9. Dependency Hygiene

| Commit | Package | From → To | Advisories closed |
|---|---|---|---|
| 405f07e | certifi | 2026.7.22 → 2026.10.23 | PYSEC-2026-4320 / GHSA-6q48-6c7w-9gw2 (×2 records) |
| f365f3e | dnspython | 2.8.0 → 2.9.0 | CVE-2026-114605 / GHSA-45c3-73f7-pv4m |
| 6d0afc7 | pyjwt | 2.14.0 → 2.15.1 | PYSEC-2026-4141 (CVE-2026-101918), PYSEC-2026-4183 (CVE-2026-102275) |
| 0a514dc | opentelemetry-* | 1.44.0 / 0.65b0 → 1.45.1 / 0.66b1 (+2 forced) | CVE-2026-114603 / GHSA-xcq7-p3pf-p88h |
| fbb05aa | pypdf | 6.19.0 → 6.21.4 | CVE-2026-113786, -113789..-113795, -115572, -115580, -115585, -119203..-119209 (18) |

- **Each commit changes requirements.lock only.** Hashes are complete; `pip install --require-hashes` works on Linux and Windows. The drift guard is 0 on Windows (uvloop excluded), on Linux, in the image, and in every local job venv.
- **pip-audit:** before, 24 advisories on the lock (pypdf 18, certifi 2, pyjwt 2, dnspython 1, otel 1). After, **0**. No SY-03 ignores.
- **DH-05 (pypdf):**
  - The text of 5 of the 12 readable PDF fixtures changes (all Arabic).
  - The poison IndexError / KeyError / TypeError.pdf now parse (1 page).
  - `content_hash` is SHA-256 of the extracted text → **RGM-22**. The RGM-22 probe: the identical chromium-AR-1 file as prices.pdf gets 409 document_source_exists; as prices-copy.pdf it gets 202 and a second document.
  - D-12 on the populated data shows 0 stored texts changed by re-index, because re-index reads the stored text and never re-extracts.
- **DH-03 changelogs read:**
  - pyjwt 2.15: new exception types are subclasses the app already catches.
  - otel 1.45: OTLP HTTP full path, already passed.
  - pypdf 6.20–6.21.4: parsing budgets and CMap fixes.
- **Majors:** none.

## 10. RGM-17: The Tie-Break

- **Decision RK-01** (ae512a2): `stable_key` = (content, title, section, ordinal, id) breaks RRF ties. `by_distance` re-sorts each vector list by (distance, stable_key) after the fetch. The SQL and indexes are unchanged.
  - **Deviation:** app/services/retrieval_service.py (1 line) is outside section 21's list. It was needed for RGM-25.
- **RGM-25:** under 6.21.4 the weasyprint Arabic PDFs gain twin passages with equal distance. HNSW scan order decided which twin survived.
- **RK-02:** non-tied orders are unchanged (unit tests; M-K01/02 killed).
- **RK-03 PARTIAL:**
  - Evaluation ×3 at RDI_HEAD: Part 1 and Part 3 identical; Part 2 "passages delivered" is 1,251 / 1,252 / 1,251.
  - The residual ±1 is the lexical channel's SQL tie by chunk id (knowledge_repository.py:829, `ORDER BY rank DESC, distance, id`). It is a repository query outside RK-01's key, so it was not changed.
  - On pypdf 6.19.0, RDI_HEAD's evaluation is identical ×2 in every cell.
- **Differences from b5aa170's column**, each explained by pypdf 6.21.4 (DH-05) or RK-01:
  - Part 1: spans 256 → 257, rank-1 240 → 243, Arabic PDF per producer chromium 2/3 → 1/3 and weasyprint 1/3 → 2/3.
  - Part 2: follow-ups 1/10 every run; passages 1,257/1,258 → 1,251/1,252.

## 11. The Hardened Tests

| Item | Reproduction | Outcome | Mutant |
|---|---|---|---|
| RGM-02 | past-midnight plugin: b5aa170 3/3 fail | 6623b30: compares with the date the database stamped; 10/10 | M-T06 killed |
| RGM-19 | PL-02 not reproduced over 33 planner shapes | 3c18a1a: own rows + ANALYZE (PL-01); 10/10 | M-T01 killed |
| RGM-18 | TB-02 12 processes: RGM-18a 5/5 fail at b5aa170 (one 1.15–1.37 s loop gap while the child is spawned, Windows) | calibrated bound still failed 10/10 on Windows; Linux original 5/5 pass → **TB-04, unchanged**; RGM-26 recorded | not run (test unchanged) |
| FRM-07, RGM-18b | not reproduced at 12 or 36 processes | **TB-04, unchanged** (audit #24) | not run |
| CPM-06 | 5/5 pass at 12 and 36 | already closed at b5aa170 (RT-08) | not run |

## 12. Mutation Matrix of this stage

- **Windows, at 277b716:** control 25 passed. 17 / 17 KILLED by a named killer: M-Q01..M-Q08 (07a/07b), M-D01, M-D02, M-K01, M-K02, M-S01, M-S02, M-T01, M-T06.
- **Linux:**
  - M-Q01..M-Q08, M-D01, M-D02: 11 / 11 KILLED.
  - M-H01 (certifi at its old pin): the drift guard reports "1 differ", rc 1.
  - M-H02 (every hash of one package altered): `--require-hashes` refuses.
- **Integrity:** every file was restored and its SHA-256 checked; the tree was clean after each mutant.

## 13. Documentation and ADR Changes

12a497f updates DECISIONS.md (ADR-188), docs/DEPLOYMENT.md (RGM-22 deploy note; pins), docs/RAG.md, docs/RUNBOOK.md and docs/SECURITY.md (the scan from the lock).

## 14. Part A Commits and the Diff Guard

| Commit | Subject | Files |
|---|---|---|
| ba45b54 | fix(knowledge): publish the indexing job after the upload commits (RGM-16) | app/api/route.py, app/db/after_commit.py, app/db/session.py, app/services/knowledge_service.py, tests/integration/test_index_job_after_commit.py |
| 501bccf | fix(extraction): flag a re-ordered Arabic price line whatever pypdf's spacing (RGM-13) | app/services/pdf_structure.py, tests/fixtures/rag/pdf/extracted_lines.json, tests/unit/test_pdf_page_structure.py |
| 97bec9f | ci(security): install the scan from requirements.lock by hash and audit the lock (RGM-21) | .github/workflows/security.yml |
| 405f07e, f365f3e, 6d0afc7, 0a514dc, fbb05aa | deps: certifi, dnspython, pyjwt, opentelemetry, pypdf | requirements.lock |
| ae512a2 | fix(retrieval): break ranking ties by a key stable across ingests (RGM-17, RGM-25) | app/services/retrieval_ranking.py, app/services/retrieval_service.py, tests/unit/test_retrieval_ranking.py |
| 6623b30 | test(knowledge): compare the publish date with the database's own stamp (RGM-02) | tests/integration/test_document_replacement.py |
| 3c18a1a | test(retention): take the table's statistics before reading its plan (RGM-19) | tests/integration/test_webhook_payload_retention.py |
| 12a497f | docs: … (ADR-188, RGM-16, RGM-13, RGM-21) | DECISIONS.md, docs/DEPLOYMENT.md, docs/RAG.md, docs/RUNBOOK.md, docs/SECURITY.md |
| 277b716 | test(security): pin what the scan installs and audits (RGM-21) | tests/unit/test_delivery_pipeline.py |

- **Totals:** `git diff --stat b5aa170 277b716` gives 21 files, 1,365+ / 72−.
- **Unchanged:** alembic/, nginx/, Dockerfile, ci.yml, both compose files, requirements-build.lock and pyproject.toml.
- **type: ignore:** 258 → 260 (both `[misc]`, Redis awaitables in the new test).

## 15. What Lands

The index job is published after the commit, with the recovery sweep behind it. The detector flags a re-ordered Arabic line whatever pypdf's spacing. The weekly scan installs and audits the lock. The lock has no advisories. Ranking ties are stable. RGM-02 and RGM-19 are hardened.

## 16. The Calendar and the Hour

- **Static sweep:** 7 changed test paths. 2 new date/time hits: one is the RGM-02 fix itself (SAFE), the other is Q-5's live lease (SEED). **0 BOMB.**
- **Hour selection (24 files):** 346 passed inside quiet hours (21:17 UTC = 23:17 Cairo), outside quiet hours (12:26 UTC), and with TZ=UTC0.
- **Time-shifted lane** (MODEL-BUILT, Windows, Python clock +100 days; PostgreSQL / Redis not shifted):
  - 108 failed / 7,820 passed / 23 skipped. Against the re-run: the same blocks minus 13 real-provider blocks that now skip, plus 1 new.
  - Five moments: the re-run's set plus the same 1 new each time.
  - The new block is Q-5 (`test_q5_the_sweep_never_doubles…`). It is a **SHIFT-ARTEFACT** with the same mechanism as the already-classified test_ingestion_recovery grace test: app clock against database clock. It passes in every real-clock lane.

## 17. Static and Alembic Gates

- **At RDI_HEAD:** ruff, black (1,119 unchanged), mypy app tests (965), mypy scripts (15): clean.
- **Alembic:**
  - empty → head: 138 steps; check clean; catalogue 0/0/0; preflight ok;
  - rag_invariants 0 (RR-01..RR-14); omnichannel 83 / 0;
  - head → 0126 → head, then base → head.
- **Refused downgrades:** 7 / 7 refuse, stamp stays 0138.
- **pgvector guard:** 0.7.4 refused, 0.8.6 ok.

## 18. Populated Data and the First Deploy (D-1..D-12)

- **Stage A:** populated by b5aa170's code (70 tables / 563 rows).
- **Stage B at RDI_HEAD:** 0 alembic steps; 0 differences; grants unchanged; sealed tokens decrypt.
- **First deploy, h0 and h1 identical:**
  - D-2 / D-3: back-fill 0 updated.
  - D-5: reindex-all of 36 documents; the sweep claimed 1 (the seeded processing generation).
  - D-6: invariants 0.
  - D-7: missing [].
  - D-8: 0 gaps.
  - D-9: downgrade refused.
  - D-10: repeats and cross-KB answers as designed.
  - **D-11: 0 job failures, 0 "not found".**
  - **D-12: 0 stored texts or hashes changed by re-index under pypdf 6.21.4.**
  - 0 Meta requests, 0 Responses calls.

## 19. Model-Built Test Results

MODEL-BUILT, Windows, real clock, venv-head (RDI_HEAD's lock, pypdf 6.21.4). Run together with §20, inside quiet hours: 20:06–21:16 UTC (22:06–23:16 Cairo).

- 7,928 passed, 23 skipped, **0 failed** (7,951 collected).
- The re-run had 7,903 / 23. The +25 are this stage's 23 new test functions, two of them parametrized.
- The skip set is identical to the re-run's.

## 20. Migration-Built Test Results (and the hour selection)

- **MIGRATION-BUILT** (WASLA_TEST_SCHEMA=migrations, fresh alembic head), Windows, real clock, venv-head, same window as §19 (QUIET): 7,932 passed, 19 skipped, **0 failed**. The re-run had 7,907 / 19; the skip set is identical.
- **Hour selection:** see §16 (346 ×3).

## 21. Kept-Data Sweep and CI Job Inventory

- **Kept-data ×3** (ci.yml selection: 37 suites + 2 invariant files; selection equality proven by ci.yml's own expression): 360 / 360 / 360 passed, 0 skipped.

| Workflow / job | Run locally (venv by its new steps) | Result |
|---|---|---|
| ci.yml quality | Linux /opt/job-rdihead | ruff, black, mypy ×2 rc 0; drift 66 / 0 |
| ci.yml tests | Linux /opt/job-q | 7,920 passed / 27 skipped / 2 failed (harness: the old-pgvector database not created; created, file 3 passed); object store, app factory, up/down/up, preflight, alembic check ok |
| ci.yml migration-parity | Linux /opt/job-q | parity 4 passed (not skipped); 4,078 passed / **1 failed: RGM-18's loop-latency test under load** (1.477 s gap; alone 15/15 ×6; both full lanes 0 failed) — recorded LOAD (RGM-18 / RGM-26); kept-data rc 0, no skips |
| ci.yml monitoring | Linux | promtool 91 rules valid, rule tests SUCCESS, amtool SUCCESS |
| ci.yml docker | Windows docker | build ok; live attempt 6 (13 s); ready 503 without services (expected); uid 1001; the new pins in the image; both sandbox children start |
| security.yml dependencies | Linux, its new steps | install, drift 66 / 0, pip check ok; **lock audit 0**; environment audit 1 (black) → **red, RGM-23** |
| security.yml secret scan | GitHub-only (gitleaks action) | red since 2026-10-05 on main → **RGM-24** |
| security.yml container scan | GitHub-only | green on main's scheduled runs |

## 22. Linux: the Sandboxes with their Limits

- **Platform:** wasla-rdi-linux, python:3.12-slim, Python 3.12.14.
- **Limits:** every child RLIMIT_AS 768 MiB, RLIMIT_CPU 21 s. The re-run's 20 fixtures give identical outcomes.
- **Poison PDFs:** IndexError / KeyError / TypeError.pdf are now accepted (pypdf 6.21.4); AssertionError / AttributeError are refused.
- **Selections:**
  - Starred ×3: 69. FRM-07's test 2.05 / 2.06 / 2.05 s.
  - Knowledge selection: 1,106 passed, 1 skipped.
  - The 8-file set ×3: 116.
  - Dedupe + vector-only ×3: 15.

## 23. Evaluation Harness

Fake embeddings, structural, three runs. The cell-by-cell comparison with b5aa170 is in §10. This is not a retrieval-quality figure.

## 24. Races, Invariants and Mutation Spot-Check

- **Races:** 12 suites × 3 = 36 / 36. The Q file as a race suite: 16 / 16 ×3 (1 job per generation, 0 early claims, 0 orphans, 0 "not found").
- **Spot-check** at 277b716, after the full lanes:
  - section 19's 17 KILLED;
  - **M-RGM20 KILLED** (the re-run had no RGM-20 mutant: Python-clock ageing restored with the transaction held 2 s; its control with the fix SURVIVES as designed);
  - the re-run's 61 KILLED, including M-RGM01 and M-RGM03; controls 23 / 13 / 26 passed.
  - **79 KILLED, 0 HUNG.** M-R34-live SURVIVED: EQUIVALENT, proof re-read at retrieval_service.py:667–676.

## 25. Runtime Verification (APP-E2E K-1..K-12)

- **Setup:** fresh alembic-built database, canary immediately before (11 refused, 0 reached). K-1..K-12 PASS.
- **K-12:**
  - (a) Arabic price list → 202, published after the commit, claimed once, the reply carries 4200.
  - (b) failure after the service ran → 500, 0 documents, 0 jobs.
  - (c) publish failed once → 202; the sweep at grace + one poll re-published once; the second pass queued 0; claimed once.
- **Totals:** 0 job_failed, 0 "not found". No secret or customer text in evidence, metrics, audit or logs.

## 26. Before / After Probes

| Probe | Before (b5aa170, pypdf 6.19.0) | After (RDI_HEAD, 6.21.4) |
|---|---|---|
| The re-run's nine (P-R03..P-R30) | 9 passed | 9 passed, every value identical |
| P-U01 (C-1, C-2) | passed | passed |
| P-L01 (ci.yml's install) | pypdf 6.19.0, drift 64 / 0, 16 passed | 6.21.4, drift 66 / 0, 21 passed |
| P-Q01 | 1 pending while open, 1 early claim, 1 "not found", retry | 0, 0, 0; 1 claim after commit |
| P-D01 | Chromium line not flagged on 6.21.4 | flagged |
| P-A01 (pip-audit of the lock) | 24 advisories | 0 |

## 27. Untracked Files

- **Files:** 11 *.md and 3 docs/ui/*.html, unchanged before and after the merge. Every modification time predates this stage; hashes are in evidence/untracked_after_merge.txt.
- **Collisions:** none with the 21 incoming paths. Nothing incoming lies under docs/ui/.

## 28. Merge and Tree Equality

- **Command:** `git merge --no-ff 277b716 -m "merge: RAG deferred items, advisories and the scan from the lock (ADR-188)"` → 1ff78e7, no conflict.
- **Equality:** `git diff --exit-code 277b716 HEAD` exits 0; tree 8906e2a4 = 8906e2a4. alembic head 0138.

## 29. Gates on the Final Canonical Tree

From E:\wasla-rdi-final, detached at 1ff78e7, on venv-head. The worktree's own `app.__file__`, interpreter and pypdf 6.21.4 were asserted in each log.

| Gate | Result | Matches RDI_HEAD (§17–24) |
|---|---|---|
| ruff / black / mypy app tests / mypy scripts | clean (1,119 files; 965; 15) | yes |
| alembic, fresh wasla_rdv_final | 138 steps; check clean; catalogue 0/0/0; preflight ok; rag_invariants 0; omnichannel 83 / 0; head → 0126 → head; base → head; documentation truth 27 passed | yes |
| MIGRATION-BUILT lane, Windows, real clock, 21:52–22:56 UTC (23:52–00:56 Cairo, QUIET), uninterrupted | **7,932 passed, 19 skipped, 0 failed** | yes (skip set identical) |
| Kept-data sweep, ci.yml selection, fresh database | **360 passed, 0 skipped** | yes |
| Documentation truth, Q-1..Q-9, detector, tie-break, hardened tests (incl. RGM-18's file), CPM-06's file, C-1..C-10, the security.yml shape test (11 files, model-built) | **192 passed** | yes |
| Drift guard, Windows | 66 checked; uvloop absent by design (not built for Windows) | yes |
| Linux (wasla-rdi-linux), venv built literally by ci.yml's tests-job install at 1ff78e7 | drift **66 / 0**, pip check ok; starred 69, Q-1..Q-9 16, C-1..C-10 13; **0 failed** | yes |
| Linux, security.yml dependencies job by its own steps at 1ff78e7 | install ok, drift 66 / 0, pip check ok; **lock audit: no known vulnerabilities**; environment audit 1 (black, RGM-23) → job red as at RDI_HEAD | yes |

Harness correction: the first run of the security job named its copy after its source, so it removed /work/final before copying from it ("cannot stat"). It was re-run from a separate extraction of the same archive.

## 30. Push

Recorded in the addendum commit that follows the push.

## 31. GitHub Actions

Recorded in the addendum commit that follows the push.

## 32. Findings

| ID | Finding | Status |
|---|---|---|
| RGM-01..RGM-12, RGM-14, RGM-15, RGM-20 | as the re-run closed them (RGM-01 / RGM-03 / RGM-20 mutants killed here) | CLOSED |
| RGM-13 | Chromium line not flagged under pypdf 6.21.4 | **CLOSED** (501bccf) |
| RGM-16 | job published before the commit | **CLOSED** (ba45b54) |
| RGM-17 | evaluation ±1 between runs | **CLOSED** for RRF and vector ties (ae512a2); lexical-SQL residual ±1 → ACCEPTED RESIDUAL (RK-03) |
| RGM-02 | publish-date test across 00:00 UTC | **CLOSED** (6623b30) |
| RGM-19 | planner-dependent retention test | **CLOSED** (3c18a1a) |
| RGM-18 | loop-latency test under load (Windows spawn) | NOT CLOSED: left for audit #24 under TB-04 |
| FRM-07 | parse deadline under load | NOT CLOSED: not reproduced; TB-04, audit #24 |
| CPM-06 | campaign sweep timing | CLOSED (earlier stage, RT-08) |
| RGM-21 | security.yml audited an environment, not the lock | **CLOSED** locally (97bec9f, 277b716); GitHub half in §31 |
| RGM-22 | pypdf 6.21.4 changes Arabic extraction → an identical Arabic PDF uploaded before the bump is not a repeat | DEFERRED PRODUCT DECISION (owner accepted 2026-11-06; external action before the first real deploy) |
| RGM-23 | black 26.5.1 (dev pin) CVE-2026-114134; security.yml's environment audit red | DEFERRED PRODUCT DECISION (owner: leave as finding) |
| RGM-24 | security.yml Secret scan: 26 gitleaks findings in test fixtures | DEFERRED PRODUCT DECISION (owner: leave as finding) |
| RGM-25 | equidistant twin passages ordered by HNSW scan | **CLOSED** (ae512a2) |
| RGM-26 | Windows under load: spawning the extraction child blocks the loop ≈1.2–1.5 s | ACCEPTED RESIDUAL (Linux, the production platform, does not show it); recommendation for audit #24 |
| Advisories (24, §9) | each bumped | CLOSED |

## 33. Decisions Ledger

| Item | Status |
|---|---|
| EQ-01..EQ-08 | CLOSED (applied) |
| PD-01..PD-06 | CLOSED |
| SY-01..SY-07 | CLOSED locally; SY-07 dispatch in §31 |
| DH-01..DH-10 | CLOSED; DH-03 waived for pypdf by the owner; DH-05 → RGM-22 |
| RK-01, RK-02, RK-04 | CLOSED; RK-03 ACCEPTED RESIDUAL |
| PL-01..PL-03 | CLOSED (PL-02 not reproduced) |
| TB-01..TB-06 | CLOSED, with RGM-18 / FRM-07 left under TB-04 |
| DG-06, DG-07 | CLOSED |
| Deviations | retrieval_service.py outside section 21's list (RGM-25); DH-03 soak waived (owner) |

## 34. Infrastructure Created by This Stage

- **Containers** (network wasla-rdi-net): wasla-rdi-pg, -pg2, -pg3, -pg-old, -redis, -redis2, -redis3, -redis4, -redis5, -minio, -linux.
- **Image:** wasla-rdi:ci.
- **Worktrees:** E:\wasla-rdi-fix, -base, -verify, -lane, -mut, -final.
- **Notes:** E:\wasla-rdi-notes, including the venvs.
- **Branch:** rag-deferred-items-20261106 (kept).

## 35. Part C — Cleanup

Runs after the addendum's run is green. The result goes to E:\wasla-cleanup\CLEANUP_rdi_<date>.md.

## 36. Remaining External Actions

- **First deploy of 0127–0138**, per the re-run's §35:
  - db_preflight with pgvector ≥ 0.8; stop the old processes; alembic upgrade head; the back-fills;
  - RAG_HYBRID_ENABLED=false; reindex-all under pypdf 6.21.4 (D-12's counts); rag_invariants to 0; then hybrid on.
  - The image rebuilt from this branch ships the new pins.
- **RGM-22, before the first real deploy:** Arabic PDFs uploaded before the bump won't be recognised as repeats. Decide whether to re-extract and re-hash them, or accept second copies. The back-fill and the detector list them for re-upload.
- **Embedding model:** never change OPENAI_EMBEDDING_MODEL directly. Use the switch (RGM-10).
- **security.yml on main:** the scheduled scan reaches main only when the canonical branch is merged into main. Merging into main is the owner's decision.
- **RGM-23:** bump black in pyproject's dev pins to 26.10.1+ (or drop the environment audit).
- **RGM-24:** gitleaks allow-list for the test fixtures.
- **Recommendations:**
  - enqueue-after-commit for the other subsystems EQ-06 listed;
  - a transactional outbox if the sweep's 300 s is ever too slow;
  - the cross-knowledge-base unique constraint;
  - the switch readiness warning (RB-03);
  - RGM-04 twin triage;
  - the lexical SQL tie by id (RK-03).
- **Audit #24:** timeout-minutes on every job, a nightly Cairo-night job, one injectable clock, FRM-05, RGM-18 / FRM-07 (TB-04), RGM-26.
- **Not run in this stage:** real-provider evaluation and calibrations; the Privacy audit (#18); row-level security (ADR-185); RR-25, RR-13.
- **The owner's review items:**
  - E:\wasla-rgm-notes, E:\wasla-rag-p1 / -p2 / -p3, the dangling volumes;
  - origin's rewrite of 2026-10-27..29;
  - the local branches (rag-deferred-items-20261106 and the RAG line's).

## 37. Final Verdict

Provisional at this commit. Every local gate is green apart from the recorded RGM-18 (load), RGM-23 and RGM-24. The owner told me (2026-11-06) to push and to treat RGM-18 as a load finding carried to audit #24, not a stop condition.

The verdict is written in the addendum, after the GitHub runs (§31).
