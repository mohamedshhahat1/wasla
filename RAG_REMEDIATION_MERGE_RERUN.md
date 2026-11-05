# RAG Remediation — Merge Re-run (RGM-05, RGM-08, RGM-07, RGM-10, RGM-12)

Stage of 2026-11-05 (Cairo UTC+2). Supersedes `RAG_REMEDIATION_MERGE_VERIFICATION.md` (0e00595, on its own branch, not merged).
Every number below was measured in this stage; nothing from a791633 or 4b0c4e9 stands in for a gate at RFX_HEAD.
Every retrieval number is **structural** (fake embeddings at the transport); the real-provider evaluation has not been run.

## Phase status

| Phase | State | UTC (Cairo = UTC+2) | Commit / push / removal |
|---|---|---|---|
| A0 state, ancestry, untracked files, dead-proxy guard + canary, infrastructure reset | done | 07:48–08:40 | branch `rag-merge-fixes-20261031` at 4b0c4e9 |
| A1–A5 RGM-05: reproduction, sequential answers, deterministic tests, fix, mutants | done | 08:45–09:00 | 4da6cd2 |
| A6–A7 RGM-08 and the hybrid-mask sweep (RGM-15) | done | | f31c1c0, e37edc5 |
| A8 RGM-07 (CI from the lock) | done | | 8e91301 |
| A9 RGM-10 (RUNBOOK), RB-02 | done (after RGM-12) | 09:12 | 180eda1 |
| A10–A11 docs, ADR-187, Part A gates | done | | e43aaa6 |
| STOP on RGM-12 → user widened the scope ("Yes, fix RGM-12") | done | ~09:00 | **4bae08d = RFX_HEAD** |
| Part B: every gate at 4bae08d | done (RGM-19 accepted by the user) | 09:15–21:35 | — |
| Merge (one normal merge commit), tree equality | done | 21:36 | **52af1a5** |
| Gates on the final canonical tree | done, green | 21:39–22:47 | — |
| Report commit, push, GitHub Actions | done — CI GREEN | 22:48–23:38 | 79de696 pushed; run 42124575540 |
| Part C cleanup | done, items left for review | 23:40– | 42 items removed; branches kept |

## 1. Executive Summary

- **RGM-05 CLOSED.** The duplicate checks of an upload now run under the workspace's upload lock, in the transaction that inserts. Two
  uploads of one text at the same moment — one knowledge base or two — end exactly as two uploads one after the other: one document,
  the other request answered `202 created:false` or `409 document_exists_in_other_knowledge_base`, never a 500, nothing left behind.
  Proved by tests that hold the lock themselves until every request is queued (10/10 failures at 4b0c4e9, 10/10 passes at the fix) and
  by mutants (all killed). Extraction stays outside the lock (held 25 ms p50, never a parse).
- **RGM-14 (new) CLOSED** with it: a refused upload's original stayed in the media store for ever (its purge row was written in the
  transaction the refusal rolls back) — sequential too.
- **RGM-08 CLOSED** (vector-only test kills M-R02); the sweep found **RGM-15** (the alternate query's folding was untested) — CLOSED test-only.
- **RGM-07 CLOSED**, and wider than reported: today's `pip install -e ".[dev]"` resolved **32 of 58** locked packages to other
  versions (SQLAlchemy, pydantic, starlette, pypdf…). CI now installs the lock by hash and a drift guard fails on any difference.
- **RGM-10 CLOSED** — and while proving it, **RGM-12** (new, user-approved fix): `docker-compose.prod.yml` passed **none** of the 18
  RAG settings (incl. `RAG_HYBRID_ENABLED`, which the documented first deploy flips, and `OPENAI_EMBEDDING_MODEL_NEXT`) to any
  container. Fixed with its CI guard; the RUNBOOK followed step by step now gives the admin preview 200 at every switch step.
- Part B: both full lanes green together inside quiet hours (7,903 / 0 / 23 and 7,907 / 0 / 19); every other gate green; one CI-job
  test failure (**RGM-19**, a pre-existing planner-dependent test) accepted by the user.
- No real provider was contacted: 0 provider responses in the whole stage (**RGM-11**: the dead proxy alone would not have covered
  the app's clients — a process-level wall was added and proven).

## 2. Source Branches and Exact Heads

| Name | Commit |
|---|---|
| ORIGIN_CANONICAL_START_HEAD (`worktree-billing-google-auth` = origin at start) | 7cdf863 |
| RGP3_HEAD / RGP3_CODE_HEAD | a791633 / 8afa99e |
| RGM_FIX_HEAD (`rag-merge-dates-20261031`) | 4b0c4e9 |
| RFX_HEAD (`rag-merge-fixes-20261031`) | **4bae08d** |
| CANONICAL_MERGED_HEAD (parents 7cdf863, 4bae08d) | **52af1a5** |
| main | 8e3a1ba (untouched) |
| ORIGIN_MIGRATION_HEAD / head | 0126 / 0138 |
| LAST_ADR at start / now | ADR-186 / ADR-187 |

Chain: 7cdf863 ⊂ a791633 ⊂ 4b0c4e9 ⊂ 4bae08d ⊂ 52af1a5. Neither the line's branches nor origin moved during the stage
(`git ls-remote` at start and before the merge: 12 heads + pull/1, as the last report). The origin rewrite of 2026-10-27..29
(ce9a0d5 / 0abebe6 / bcb960f / 1d77fe8) is unchanged as an observation for the owner.

## 3. Infrastructure Reused and Reset, and the Dead-Proxy Guard

**Reuse decision (applied):** the wasla-rgm-* containers, network and venv of the last attempt; none of their data. Every container
had exited (host restart) and was started with the same name, port and IP. Every `wasla_rgm_*` database dropped (listed first, by
name, on the three servers), FLUSHALL on the three Redis instances, bucket `wasla-media` empty. Added for this stage (recorded, removed
in Part C): `wasla-rgm-pg3` (55994), `wasla-rgm-pg4` (55995), `wasla-rgm-redis4` (57994), image tag `wasla-rgm:4b0c4e9`.
Venv = requirements.lock except uvloop (no Windows build) + pinned dev extras; Linux container venvs built by the new CI steps (= lock).

**DG-01** `rerun/guard.env`: `HTTP(S)_PROXY` / `ALL_PROXY` (both cases) = `http://127.0.0.1:9` (nothing listens), `NO_PROXY` =
127.0.0.1, localhost and the stage's container IPs. **RGM-11 (guard defect found by DG-03):** every outbound client the app builds goes
through `build_guarded_client` (app/core/net.py:274) with an explicit transport, which httpx exempts from proxy variables (proved: the
guarded client has no proxy mounts). A second wall was therefore added: `rerun/guard/sitecustomize.py` on `PYTHONPATH` refuses every
DNS lookup and connect outside loopback, private addresses and wasla-rgm-* names, and logs each refusal; every harness asserts it is
loaded. Pytest lanes run with **no** OpenAI key (as CI), so the opt-in real-provider suites skip; harnesses use their own throwaway keys.

**DG-02 canary** (A0, start of Part B, before the E2E; Windows and Linux): bare httpx refused at the dead proxy ×4 hosts; the app's
builders — Responses, embeddings, WhatsApp, and the **admin preview's `get_provider_http`** — refused by the wall ("the host could not
be resolved"); REAL_PROVIDER_RESPONSES 0 every time.

**DG-03 inventory:** net.py:281 build_guarded_client (the single constructor) ← openai/client.py:172, openai/embeddings.py:239
(incl. api/dependencies.py:509 = admin preview), openai/transcription.py, whatsapp/client.py:297, whatsapp/messaging_limit.py,
channels/media.py; paymob.py:759 and resend.py:88 (injected transport); object_store.py:334 (MinIO, local). No OpenAI SDK installed.
Provider base URLs are constants (no setting to point them at the dead port).

**DG-05 totals:** connection attempts refused by the wall/proxy: canaries 7 + 11 + 11 + 11 + 11; shifted lane 11 (real-provider
suite with the guard's literal key, before the DG-01 adjustment); Linux CI 21 (those 11 again + 10 tests that aim at 93.184.216.34 on
purpose); E2E 0; RB-02 0. **Real-provider responses: 0.**

## 4. Decisions Applied

| Decision | Applied as |
|---|---|
| UL-01 | `_record` takes `hold_limit_lock(tenant, KNOWLEDGE_DOCUMENTS)` (transaction-scoped advisory lock, the one `reserve_slot` takes) before every duplicate check; held to the commit |
| UL-02 | workspace-keyed (`_lock_id(tenant, "knowledge_documents")`); C-5 proves two workspaces never wait |
| UL-03 | every loser's answer = the sequential answer, measured per case on a fresh workspace; insert in a savepoint, a residual unique violation re-runs the checks and answers as the repeat (never 5xx) |
| UL-04 | leftovers asserted for every loser: documents, generations, audit "created", queued jobs, usage rows, orphan originals (stored − referenced − owed) — required fixing RGM-14 |
| UL-05 | extraction before the lock unchanged; lock held 24.7 ms p50 (text), 22.7 ms (PDF) |
| UL-06 | inventory §6: only `submit` inserts documents; `replace_content` never inserts and is unchanged |
| UL-07 | the test holds the lock from its own connection with the app's key function, waits on pg_locks until all requests queue, releases |
| UL-08 | application change: `app/services/knowledge_service.py` only (+ RGM-12's compose file, user-approved) |
| VQ-01..VQ-03 | vector-only test with the policy resolved from settings (`RAG_HYBRID_ENABLED=false`), asserting `channels == ("vector",)`; Part 1's killer keeps its place with a docstring pointing to it |
| VQ-04 | sweep table §8; M-V02 → RGM-15 |
| CL-01..CL-03 | quality / tests / migration-parity install the lock + build lock by hash, then `-e ".[dev]"`; new step `scripts/check_installed_lock.py` + `pip check`; nothing else in ci.yml changed; security.yml unchanged |
| CL-04 | pypdf 6.21.4 still mis-orders the Chromium price line but no longer flags it → RGM-13 |
| CL-05 / CL-06 | Linux both ways (§9); ci.yml parses (yaml.safe_load); actionlint not available |
| RB-01..RB-04 | RUNBOOK names the API, IndexingWorker and AgentWorker, restarts worker then API, confirms each; .env.example, RAG.md, DEPLOYMENT.md say the same; no application change for the 503 |
| DG-01..DG-05 | §3 |

## 5. RGM-05: Reproduction at 4b0c4e9

`submit()` (knowledge_service.py:424–454 at 4b0c4e9): validate → source key → `_prepare` (extract, normalise, hash, write the original;
connection released) → `_record`: KB check (473) → `get_by_hash` same KB (481) → `get_by_source_key` (498) → `get_by_hash_in_workspace`
(524) → `reserve_slot` (536 = `EntitlementService.reserve_or_refuse` → `pg_advisory_xact_lock`) → inserts, enqueue, audit; commit at the
route (`CommittingRoute`). Constraints: `uq_documents_tenant_id_knowledge_base_id_content_hash`, partial `uq_documents_tenant_kb_source_key`;
nothing spans knowledge bases. The 500's mechanism: the losing insert's unique violation, then `_discard_original` writing into the
failed transaction → `PendingRollbackError`.

Last attempt's reproduction, 3 runs: same KB [IntegrityError, created], [created, ConflictError], [created, IntegrityError]; two KBs
2 documents ×3. Sequential answers (second of two uploads, both routes): S-1 same file 202 `created:false`; S-2 other filename 202
`created:false, metadata_updated:true`; S-3 other KB 409 `document_exists_in_other_knowledge_base` (**and an orphan original — RGM-14**);
S-4 other workspace 202 `created:true`; S-5 `?replace=true` 202 `created:false`. The lane failure of the last attempt
(test_upload_concurrency) opens the same window by load; the new tests do not depend on it.

## 6. RGM-05: The Fix

4da6cd2 `fix(knowledge): run the duplicate checks under the workspace upload lock (RGM-05)` — knowledge_service.py (+ the test file):
lock first, `_existing_answer` holds the three checks, insert in `begin_nested()`, a unique violation re-runs `_existing_answer`;
on every exception path of `submit` / `replace_content` the request's original is deleted from the store directly
(`_delete_unrecorded_original`, RGM-14).

| UL-05 lock hold (first acquisition → commit) | 4b0c4e9 p50 / max | 4bae08d p50 / max |
|---|---|---|
| 20 text uploads | 17.8 / 354.5 ms | 24.7 / 95.5 ms |
| 5 PDF uploads | 14.8 / 66.7 ms | 22.7 / 27.1 ms |

## 7. The Concurrency Tests (`tests/integration/test_upload_dedupe_concurrency.py`, 13 tests)

| Test | 4b0c4e9 ×10 (first failing assertion) | 4bae08d Windows ×10 | Linux ×3 | Part B (races ×3, Linux ×3, lanes, moments) |
|---|---|---|---|---|
| C-1 JSON / C-1 multipart | 10/10 FAIL — 500 | pass | pass | pass |
| C-2 two KBs | 10/10 FAIL — 2 documents | pass | pass | pass |
| C-2b other filename, C-3 five uploads, C-4 JSON vs multipart, C-8a replace vs plain | 10/10 FAIL — 500 | pass | pass | pass |
| C-5 workspaces never wait, C-7 two texts | pass (guards) | pass | pass | pass |
| C-6 one slot left | 10/10 FAIL — orphan original (RGM-14) | pass | pass | pass |
| C-8b replace of an existing source vs plain | FAIL — no lock taken at 4b0c4e9 (by design) | pass | pass | pass |
| C-9 20 unforced rounds | 10/10 FAIL — 2 documents in round 0 | pass | pass | pass |
| unique-violation backstop | 10/10 FAIL — 500 vs 202 | pass | pass | pass |
| C-10 | = Part 1's test_two_uploads_to_one_workspace_succeed_while_a_slow_pdf_parses | | | pass |

Note: with UL-03's backstop a same-KB pair under the old order gets the sequential answer, so M-U01 is killed by C-2 / C-3, not C-1.

## 8. RGM-08 and the Hybrid-Mask Sweep

f31c1c0 `test_the_vector_search_reads_the_folded_query_with_hybrid_off` — 10/10, Linux 3/3; kills M-R02. Root cause of the mask: the
default `RetrievalService` policy turns hybrid on regardless of settings, so the last attempt's `RAG_HYBRID_ENABLED=false` run proved
nothing; the test resolves its policy from settings.

| Mutant | Path | Hybrid on | Hybrid off | Class | Action |
|---|---|---|---|---|---|
| M-R01 NFKC | storage normalisation | killed (unit) | n/a | killed | — |
| M-R02 query not folded | query | Part 1's killer passes | killed by f31c1c0 | masked | RGM-08 |
| M-R03 digit map | normalisation | killed (unit) | n/a | killed | — |
| M-R04 bidi | normalisation | killed (unit, Part 1) | n/a | killed | — |
| M-V01 chunk input not folded | passage | killed (test_contextual_embedding) | n/a | killed | — |
| M-V02 alternate query not folded | query | survived 101 tests | killed by e37edc5 | untested on the vector side | **RGM-15**, test-only |

## 9. RGM-07: CI from the Lock

CL-04 (Linux, ci.yml's old install at 4b0c4e9): 32 of 58 locked packages resolved to other versions — alembic, anyio, certifi,
charset-normalizer, cryptography, dnspython, googleapis-common-protos, httptools, mako, markupsafe, numpy, the opentelemetry set (6),
pycparser, pydantic 2.14.2 / core 2.50.2 / settings 2.16.2, pyjwt, **pypdf 6.21.4**, python-dotenv, python-multipart, sqlalchemy 2.0.57,
starlette 1.10.0, typing-inspection, tzdata, uvloop 0.23.0, watchfiles, websockets; test_arabic_extraction 1 failed / 5 passed.
**RGM-13:** under 6.21.4 the Chromium fixture's price line is still re-ordered (`جنیھ للمتر المربع.4500 سعر …`), now with a space, so
`digits_glued_to_arabic` no longer fires (6.19.0: `{'digits_glued_to_arabic': 1}`) — a latent defect for the pypdf bump.

ci.yml diff (8e91301), the same in quality, tests and migration-parity:

```diff
       - name: Install dependencies
-        run: pip install -e ".[dev]"
+        # What the image installs (the Dockerfile's builder): the lock, by
+        # hash, first; then the project and the dev-only tools, which leaves
+        # every locked package at its pin. ...
+        run: |
+          pip install --require-hashes -r requirements.lock -r requirements-build.lock
+          pip install -e ".[dev]"
+
+      - name: Installed packages are the lock's
+        run: |
+          python -m scripts.check_installed_lock requirements.lock requirements-build.lock
+          pip check
```

CL-05: new steps on Linux → 64 pinned checked, 0 differ, pip check clean, pypdf 6.19.0, openpyxl 3.1.5, python-docx 1.2.0, lxml 6.1.3,
uvloop 0.22.1; Arabic / upload / plan / replacement suites 65 passed. Old steps + guard → rc 1, 36 differ (pypdf 6.21.4 named).

## 10. RGM-10: The Embedding Switch RUNBOOK (and RGM-12)

Readers of `OPENAI_EMBEDDING_MODEL_NEXT`: services/embedding_spaces.py `next_space_client` ← knowledge_preview_service.py:73 (API,
admin preview), workers/ingestion_worker.py:195 (IndexingWorker), workers/ai_worker.py:904 (AgentWorker); workers/queues.py:671/687
(the `embedding-switch` CLI, in the worker container).

**RGM-12** (found proving RB-02; fixed with the user's approval in 4bae08d): docker-compose.prod.yml enumerated each service's
environment and carried none of the 18 settings; `docker compose config` with the variables set showed migrate/api/worker without
them. Now api and worker carry them as `${VAR:-<Settings default>}`; FEATURE_SETTINGS maps `rag_` and `openai_embedding_model_next`;
with the old compose file the guard fails for both services; Settings built from the rendered environment are the defaults, or the
values set.

RB-02 (`rerun/e2e/rb02_switch.py`, the real API under uvicorn restarted per step, real IndexingWorker and AgentWorker, fakes at the
transport, customers every 2 s, ≥ 2 answers per step):

| Procedure | Step | Preview (switching / other / untouched B) | Customers |
|---|---|---|---|
| OLD | 1 worker gets NEXT · 2 start · 3 build · 4 status | 200 each | |
| OLD | 5 flip | **503** / 200 / 200 | 11 turns, 0 empty, 0 doubled |
| NEW | 1 worker then API restarted with NEXT · 2 confirm (both print the model) · 3 start · 4 build · 5 status · 6 flip | **200 at every step** | 13 turns, 0 empty, 0 doubled |

## 11. Mutation Matrix of this Stage

| Mutant | Windows | Linux | Killer |
|---|---|---|---|
| M-U01 checks before the lock | KILLED | KILLED | C-2 |
| M-U02 lock released before the insert | KILLED | KILLED | C-2 |
| M-U03 lock by KB | KILLED-OTHER (C-1 first); M-U03-C2 KILLED | same | C-1 / C-2 |
| M-U04 backstop removed | KILLED | KILLED | backstop test |
| M-U05 reservation after insert | KILLED | KILLED | C-6 |
| M-U06 replacement reserves a slot | KILLED | KILLED | test_a_full_plan_still_accepts_a_replacement_on_both_routes |
| M-U07 extraction inside the lock | KILLED | KILLED | Part 1's slow-PDF test |
| M-U08 repeat's original not owed | KILLED | KILLED | C-1 |
| M-U09 refused original kept (RGM-14) | KILLED | KILLED | C-2 |
| M-R02 | KILLED | KILLED | vector-only test |
| M-V02 | KILLED | — | RGM-15 test |
| M-RGM01, M-RGM03 (regression) | KILLED | KILLED | RGM-01 / RGM-03 tests |
| M-L01 | — | old install → guard rc 1 | drift guard |
Every file restored and SHA-256 checked by the runner; worktree clean after every batch.

## 12. Documentation and ADR Changes

ADR-187 (new) + dated amendment to ADR-163; docs/RAG.md (concurrent uploads, the lock's scope, every process of the switch);
docs/RUNBOOK.md "Changing the embedding model"; docs/DEPLOYMENT.md (OPENAI_EMBEDDING_MODEL_NEXT row); .env.example. docs/API.md
unchanged (no answer changed). test_documentation_truth green (27 tests; 258 operations; migration range 0001–0138).

## 13. Part A Commits and the Diff Guard

| Commit | Files |
|---|---|
| 4da6cd2 fix(knowledge): run the duplicate checks under the workspace upload lock (RGM-05) | app/services/knowledge_service.py, tests/integration/test_upload_dedupe_concurrency.py |
| f31c1c0 test(rag): prove the vector query is folded for matching, with hybrid off (RGM-08) | tests/integration/test_arabic_extraction.py |
| 8e91301 ci: install every job from requirements.lock and fail on drift (RGM-07) | .github/workflows/ci.yml, scripts/check_installed_lock.py |
| 180eda1 docs(rag): the embedding switch gives the next model to every process that embeds (RGM-10) | docs/RUNBOOK.md, docs/DEPLOYMENT.md, .env.example |
| e43aaa6 docs: upload lock order, CI from the lock, the embedding switch on every process (ADR-187, …) | DECISIONS.md, docs/RAG.md (both RAG.md edits here — recorded) |
| e37edc5 test(rag): prove the alternate query is folded without the exact-word channel (RGM-15) | tests/integration/test_arabic_extraction.py |
| 4bae08d fix(deploy): pass every knowledge setting to the API and the worker (RGM-12) | docker-compose.prod.yml, tests/integration/test_deployment_configuration.py, docs/RUNBOOK.md, docs/DEPLOYMENT.md, .env.example, DECISIONS.md |

Diff guard 4b0c4e9..4bae08d: the files above only; alembic, nginx, both locks, pyproject.toml, Dockerfile, security.yml and
docker-compose.yml unchanged; test-only commits touch only tests; ruff, black, mypy app tests, mypy scripts clean; `type: ignore`
256 → 258 (two narrow ones in the new test file).

## 14. What Lands

74 commits 7cdf863..4bae08d (Part 1 29, Part 2 14, Part 3 22, RGM-01 d9a5d6f, RGM-03 4b0c4e9, this stage 7); 208 files,
+26,476 / −634. Migrations: 0001–0125 byte-identical; 0126 `upgrade()` AST segment identical (sha 1a1ab885…), `downgrade()` changed
(RR-14); 0127–0138 added (12 files), single head 0138. requirements.lock: +et-xmlfile 2.0.0, lxml 6.1.3, openpyxl 3.1.5,
python-docx 1.2.0; 0 pins changed. 0 test files deleted or renamed under tests/; pre-existing tests changed by this stage:
test_arabic_extraction.py (two tests added, one docstring) and test_deployment_configuration.py (two mapping entries).

## 15. The Calendar and the Hour

Static sweep: 70 test files added or changed; 68 date/time hits; exactly one new versus 4b0c4e9 —
test_upload_dedupe_concurrency.py:122 `email_verified_at=datetime.now(UTC)` (SEED). 0 BOMB.
+100-day lane (real 2026-11-05 → shifted 2027-02-13): 120 failed / 7,794 passed / 12 skipped — 108 identical by id and error text to
the last attempt's classified 108 (51 MinIO clock skew; the line's 10 SHIFT-ARTEFACTS; the rest pre-existing at origin); 11
GUARD-ARTEFACTS (real-provider suite with the guard literal, refused at DNS; DG-01 adjusted); 1 LOAD (RGM-18). 0 unclassified.

| Moment (Cairo) | Failed / passed | Classification |
|---|---|---|
| 2027-01-01 00:30 | 11 / 1,170 | the 10 SHIFT-ARTEFACTS + 1 LOAD (Arabic PDF `[word]` timeout; 3/3 isolated) |
| 2027-04-29 21:30, 2027-10-29 21:30, 2028-02-29 10:00 | 10 / 1,171 each | the 10, identical |
| 2026-11-30 23:30 | 8 / 1,173 | subset of the 10 (shift of 25 days) |
Hour selection (15 clock files + 3): outside quiet hours 251 passed; TZ=UTC0 251; **inside quiet hours (23:24 Cairo) 251**.

## 16. Independent Static and Alembic Gates

Fresh database: 138 steps 7.9 s; check clean; catalogue 0/0/0; preflight ok; RR checks 0; omnichannel 83 / 0; head → 0126 → head
(12 / 12); base → head 138 / 138. Refused downgrades (each from 0138, stamp left at 0138, 0 invalid indexes, 3/3 key indexes valid):
gap → 0137, query space → 0135, shadow → 0135, CSV → 0134, lexical text → 0131, source key → 0128, RR-14 → 0089. pgvector 0.7.4:
db_preflight prerequisites and verify exit 1 ("older than 0.8.0 … hnsw.iterative_scan"); 0.8.6 exit 0.

## 17. Populated Upgrade from Origin and the First Deploy

Stage A at 7cdf863: 68 tables / 574 rows. Stage B at 4bae08d: 12 steps 3.1 s, **0 differences** over every origin column (except
alembic_version), grants unchanged, sealed tokens decrypt, pg_dump holds no key / token / password.
D-1..D-9 hybrid off: D-5 36 re-indexed, 1.0 provider call per document; D-6 RR all 0, omnichannel 83 / 0; D-7 SKU via lexical +
vector; **D-10** same re-upload-list PDF ×2 at once → 202 / 202 repeat, one document; fresh text into two KBs at once → 202 created +
409 other KB, one document; 0 5xx; invariants after 0; **D-8 gate: 25 samples, 0 gaps**; 0 Meta requests, 0 sends, 0 Responses calls;
D-9 downgrade refused, stamp 0138. Top-6 measure: weasyprint-MX-1 in 3 D-5 samples (RGM-04 twin). RR-17 on purpose (hybrid on):
gate 0 gaps; 3 documents left a top-6 search, none left the served set.

## 18. Model-Built Test Results

MODEL-BUILT, Windows, real clock, notes venv (= lock), **inside quiet hours** (20:06–21:23 UTC = 22:06–23:23 Cairo), together with
§19 on a separate server: **7,926 collected, 7,903 passed, 0 failed, 23 skipped** (real-provider 11, parity 4, Windows signal path 5,
kept-data-only 2, tmpfs 1). Collected = 7,909 + this stage's 17. Lane db3: 0 keys, 0 query-embedding entries after.

## 19. Migration-Built Test Results

MIGRATION-BUILT (WASLA_TEST_SCHEMA=migrations), Windows, real clock, same window, pg2: **7,907 passed, 0 failed, 19 skipped**.
Hour selection: §15. Known deferred failures (FRM-05, FRM-07, CPM-06): none failed in either lane.

## 20. Kept-Data Sweep and CI Job Inventory

Kept-data sweep (ci.yml's rule, MIGRATION-BUILT, WASLA_TEST_KEEP_AI_DATA=1): 360 / 0 / 0 ×3 on Windows; 360 / 0 on Linux.

| Job (ubuntu-latest, no timeout-minutes) | Local run, installed the new way | Result |
|---|---|---|
| quality | Linux venv by the new steps | drift guard 64 / 0, pip check, ruff, black, mypy app tests, mypy scripts — green |
| tests | Linux, localhost Redis forwarded (as GitHub's service) | **7,905 passed, 1 failed (RGM-19), 18 skipped** (the tmpfs one: GitHub mounts it with sudo); store gate ok; app factory; upgrade → base → head; preflight; check — green |
| migration-parity | Linux | enums agree; parity not skipped; integration + e2e 3,968 passed / 0 failed / 95 skipped (no S3 in that job on GitHub either); kept-data 360 / 0 |
| monitoring | promtool / amtool images | config valid (91 rules), rule tests pass, alertmanager valid |
| docker | image at 4bae08d (85 s build) | live on attempt 1 after 8 s; uid 1001; pypdf 6.19.0 in the image; both sandbox children start |
| security.yml | not triggered by this push (push to main, PR, Monday schedule) | pip-audit on the lock: 24 advisories in 5 packages (RR-38 had 17: new advisories on the same pins), none in Part 3's packages |
Harness corrections (mine): no Redis on the container's localhost, no curl, the OpenAI guard literal, the kept step's env — each
corrected and the job re-run from the start.

## 21. Linux: the Sandboxes with their Limits, and the Concurrency Tests

python:3.12-slim@sha256:78387bc3…, Linux 6.6.114.1-microsoft-standard-WSL2, Python 3.12.14. Every child **RLIMIT_AS 768 MiB,
RLIMIT_CPU 21 s**; the 1,001-row workbook, the zip bomb and XML entities refused with their reasons; 12 PDFs accepted 0.12–0.24 s,
peak ≈ 35 MB. Starred files 69 ×3; FRM-07's bound 2.05 / 2.05 / 2.06 s; knowledge / RAG / agent selection 1,103 passed, 1 skipped;
concurrency + vector-only + RGM-15 tests 15 ×3. (Selection 1 and the starred list rebuilt from the last attempt's logs; the full
Linux tests job covers them.)

## 22. Evaluation Harness

Three runs, pypdf 6.19.0 asserted: Part 1 and Part 3 tables identical ×3 and to 4b0c4e9's column (chunks / spans intact / rank 1 =
128 / 256 / 240; spans 126/126). Part 2: passages delivered 1,257 vs 1,258 (RR-20 ±1) and follow-ups "as typed" 0 vs 1 of 10 —
**RGM-17**, the same RRF tie-break by random chunk id (retrieval_ranking.py:157) on a second metric; query contract 10/10 every run.
Structural only; the real-provider table is empty.

## 23. Races, Invariants and Mutation Spot-Check

Races: 12 suites × 3 runs, each alone on a fresh database: 36 / 36 passed (the dedupe file asserts documents per text, 5xx and losers'
leftovers inside). Invariants: fresh database RR 0 / omnichannel 83 / 0; upgraded databases after D-10 0; E2E database (K-10) 0;
in-session kept-data invariant suites 3/3. Spot-check (merge prompt's 50 + this stage's): controls 23 / 13 / 26 passed; **61 KILLED,
0 HUNG**; M-R34-live EQUIVALENT (re-stated at 4bae08d: results keyed by (generation_id, ordinal) and read with the candidate's own
generation; the mutant only fetches rows never read).

## 24. Runtime Verification (APP-E2E K-1..K-11)

Harness = the last attempt's (read in full) + every process carrying the next model from the start (RB-01) + K-11; database by alembic
from empty; canary refused immediately before; 12:40–12:43 Cairo. **K-1..K-11 PASS.** K-7: 20 turns → 1 embedding request. K-9:
preview 200 after start / build / status / flip / retire; exits 0 ×5; 0 empty, 0 doubled. K-11: same KB by two admins (JSON +
multipart) → 202 created / 202 repeat; two KBs → 202 / 409 other KB; 2 documents; the price reaches the customer once; one passage
holds it. K-10: 55 terms scanned in audit, evidence and metrics — 0 found. Wall refusals during the run: 0.

## 25. Before / After Probes

| Probe | Before | After |
|---|---|---|
| P-R03 | 89 presentation forms; typed words 0/9; ٤٥٠٠≠4500 | 0; 8/9; ٤٥٠٠=4500 |
| P-R05 | 7200 then 7200+7900 | 7200 then 7900 only |
| P-R06 | 4 delivered whatever asked | 4 / 6 / 8 / 10 |
| P-R11 | "Still checking." sent | the price sent, 2 searches |
| P-R12 | 2,2,2 same documents | 2,2,1 disjoint (5 distinct) |
| P-R17 | 1 → 0 after a direct model change | 1 → 0 (by design, RR-32) |
| P-R22 | SKU found 0/18 | 18/18 |
| P-R28 | csv / xlsx / docx / html refused | accepted |
| P-R30 | 20 embedding requests | 1 |
| P-U01 (4b0c4e9 → 4bae08d) | C-1 / C-2: 2 failed (500; 2 documents) | 2 passed |
| P-S01 | old RUNBOOK: 503 after the flip | new: 200 at every step |
| P-L01 | ci.yml at 4b0c4e9: pypdf 6.21.4, Chromium test fails | at 4bae08d: 6.19.0, guard 0, passes |

## 26. Untracked Files

E:\wasla held 15 untracked paths (12 documents, 3 under docs/ui/), SHA-256 identical to the last attempt's list at start and before the
merge. `RAG_SYSTEM_REVIEW.md` collided with an identical incoming blob (b9a6500f…): backed up to
`E:\wasla-rgm-notes\rerun\untracked-backup\`, moved aside, merged; the merged file's SHA-256 equals the user's copy. After the merge:
the 14 remaining paths, hashes unchanged. No incoming path under docs/ui/; the report path was free.

## 27. Merge and Tree Equality

`git merge --no-ff 4bae08d -m "merge: RAG remediation parts 1-3 (ADR-158..187)"` in E:\wasla → **52af1a5**, parents 7cdf863 and
4bae08d; no conflict; `git diff --exit-code 4bae08d 52af1a5` exit 0; alembic heads 0138; a791633 and 4b0c4e9 ancestors.

## 28. Gates on the Final Canonical Tree

From E:\wasla-rgm-final, detached at 52af1a5 (notes venv, pypdf 6.19.0): ruff, black (1093), mypy app tests (963), mypy scripts
(15) clean. Fresh `wasla_rgr_final`: 138 steps 9.6 s, check clean, 0/0/0, preflight ok, rag_invariants ok, omnichannel 83 / 0,
head → 0126 → head and base → head clean. **MIGRATION-BUILT lane: 7,907 passed, 0 failed, 19 skipped** (21:39–22:47 UTC =
23:39–00:47 Cairo, quiet hours). Kept-data sweep 360 / 0 / 0 skipped. Documentation truth, isolation gate, replacement, switch,
cache suites, concurrency and vector-only tests, deployment configuration: 163 passed. Linux at 52af1a5 (venv by the new tests-job
steps; my first attempt ran pip behind the dead proxy and installed nothing — the drift guard listed every package missing; rebuilt):
drift guard 64 / 0, pip check clean, pypdf 6.19.0; starred files 69 ×3; concurrency 15 passed. Tracked tree clean.

## 29. Push

Report commit **79de696** (one file) on 52af1a5. Before the push origin was still 7cdf863 (fast-forward). `git push origin
worktree-billing-google-auth` (no force) at 22:48 UTC: `7cdf863..79de696`; afterwards origin = local = 79de696. Nothing else pushed.

## 30. First GitHub Actions Run (addendum)

CI run **42124575540** (https://github.com/mohamedshhahat1/wasla/actions/runs/42124575540) on 79de696, 22:48–23:38 UTC
(00:48–01:38 Cairo, inside quiet hours) — **success**, the first green run on this branch since 2026-10-27:

| Job | Conclusion | Time |
|---|---|---|
| Lint, format, types | success | 1 min 41 s — drift guard "64 pinned packages checked, 0 differ", pip check clean, ruff, black (1117), mypy (963) |
| Tests and migrations | success | 49 min 51 s — **7,907 passed, 17 skipped**, 0 failed (skips: real-provider 11, parity 4, old-pgvector 2 — all allowed); store gate, app factory, upgrade → base → head, preflight, check green |
| Migration-built schema | success | 45 min 1 s — 4,336 passed (enums 4, parity 4, integration + e2e 3,968, kept-data 360), 95 skipped (no object store in that job; old-pgvector), 0 failed |
| Alert rules and receivers | success | 3 min 3 s |
| Docker build | success | 1 min 41 s |

GitHub installed the lock: pypdf 6.19.0, openpyxl 3.1.5, python-docx 1.2.0, lxml 6.1.3, uvloop 0.22.1; the drift guard reported 0
differences in every job that installs Python. test_the_chromium_pdf_flags_its_reordered_price_line, the dedupe concurrency tests,
the two vector-only tests and RGM-19's planner test all passed there. security.yml did not run (not triggered by this branch).

## 31. Findings

| ID | Finding | Status |
|---|---|---|
| RGM-01 | route-level full-plan refusal test deleted in Part 1 | CLOSED (d9a5d6f, landed) |
| RGM-02 | midnight-UTC date comparison in a replacement test | DEFERRED (audit #24) |
| RGM-03 | carriage-return guard test missing | CLOSED (4b0c4e9, landed) |
| RGM-04 | near-identical twins tie in top-6 during a rebuild | ACCEPTED RESIDUAL (seen again in D-5) |
| RGM-05 | duplicate checks before the plan lock | **CLOSED** (4da6cd2) |
| RGM-06 | Part 3 lanes on the system Python | CLOSED BY DESIGN (every run here asserts the lock's interpreter and pypdf) |
| RGM-07 | CI resolved 32 / 58 packages away from the lock | **CLOSED** (8e91301) |
| RGM-08 | vector query folding untested | **CLOSED** (f31c1c0) |
| RGM-09 | two real-provider requests in the last attempt | CLOSED (0 here; RGM-11's wall) |
| RGM-10 | RUNBOOK gave the next model to the worker only | **CLOSED** (180eda1, RB-02) |
| RGM-11 | dead proxy does not cover the app's guarded clients | CLOSED (process-level wall, proven) |
| RGM-12 | prod compose passed no RAG setting to any container | **CLOSED** (4bae08d, user-approved) |
| RGM-13 | pypdf 6.21.4 still mis-orders the Chromium line, undetected | DEFERRED (dependency-hygiene stage) |
| RGM-14 | refused uploads' originals left in the store | **CLOSED** (4da6cd2) |
| RGM-15 | alternate-query folding untested | **CLOSED** (e37edc5) |
| RGM-16 | indexing job enqueued before the upload commits; first claim fails, retry publishes | DEFERRED (recommendation: enqueue after commit) |
| RGM-17 | eval "as typed" follow-up ±1 between runs | ACCEPTED RESIDUAL (RR-20 mechanism) |
| RGM-18 | two timing bounds failed under heavy concurrent load; pass isolated | DEFERRED (audit #24, FRM-07 class) |
| RGM-19 | planner-dependent assertion in test_webhook_payload_retention failed once in the Linux tests job | ACCEPTED RESIDUAL (user decision; passed in both full lanes, parity and isolation) |

## 32. Decisions Ledger

UL-01..UL-08 CLOSED; VQ-01..VQ-04 CLOSED; CL-01..CL-06 CLOSED (actionlint not available — EXTERNAL VERIFICATION for that half of
CL-06); RB-01..RB-04 CLOSED; DG-01..DG-05 CLOSED. CI jobs: quality, migration-parity, monitoring, docker CLOSED (run locally, green);
tests CLOSED with RGM-19 accepted; security.yml EXTERNAL VERIFICATION (not triggered by this push; pip-audit run locally).
Recommendations (DEFERRED PRODUCT DECISION / external): a cross-knowledge-base unique constraint as a second wall; a readiness warning
or a refusal at `embedding-switch start` when a process lacks the next model; RGM-04 twin triage; the pypdf bump with RGM-13;
timeout-minutes, a nightly job, one injectable clock, FRM-05 / FRM-07 / CPM-06 / RGM-02 / RGM-18 / RGM-19 (audit #24).

## 33. Infrastructure Created or Kept by This Stage

Containers wasla-rgm-pg, -pg2, -pg-old, -pg3, -pg4, -redis, -redis2, -redis3, -redis4, -minio, -linux; network wasla-rgm-net;
images wasla-rgm:ci, wasla-rgm:4b0c4e9; worktrees E:\wasla-rgm-fix (branch), -verify, -lane, -mut, -origin, -final (detached), and
the last attempt's -mut2, -dates, -evalb4, -report; E:\wasla-rgm-notes (venv, rerun\ draft, logs, evidence, proof, mutation, e2e,
upgrade — the throwaway values are generated, none real).

## 34. Part C — Cleanup (addendum; full record E:\wasla-cleanup\CLEANUP_rag_20261106.md)

CI_GREEN_HEAD = 79de696 = origin. Rescued first: 478 files of E:\wasla-rgm-notes (scripts, harnesses, proof plugins, mutation
runners, the RGM-05 reproduction and the RGM-08 probe, guard.env — no secret — logs and evidence; not the venv, the throwaway
credential files or media stores) to E:\wasla-verification-scripts\rag-merge\, every file SHA-256 verified. E:\wasla-rag-notes kept,
MANIFEST.sha256 (251 files) added. Removed, by exact name, one per command: 18 worktrees (rg1-base, rg1-mut, rg2-base, rg2-eval,
rg2-mut, rg3-eval, rg3-lane, rg3-mut and wasla-rgm-verify, -lane, -origin, -mut, -mut2, -dates, -evalb4, -report, -fix, -final; each
clean, each HEAD in CI_GREEN_HEAD's history except -report's, whose branch stays), 21 containers (wasla-rg1-*, -rg2-*, -rg3-*,
wasla-rgm-*), network wasla-rgm-net, images wasla-rgm:ci and wasla-rgm:4b0c4e9. E:\wasla-rgm-notes removed after this addendum
is pushed. Branches before = after (53). Development stack, its volumes (wasla_postgres-data) and every other stage untouched.
**Left for review (ASK):** E:\wasla-rag-p1, -p2, -p3 (the Parts' worktrees on their branches, each with an untracked audit\ folder);
the removed PostgreSQL containers' anonymous data volumes (dangling, not identifiable by name beside other stages' volumes).

## 35. Remaining External Actions

- The first deploy of 0127–0138 in DEPLOYMENT.md's order (db_preflight with pgvector ≥ 0.8 on the real server; stop the old processes;
  alembic upgrade head; backfill-normalised-content and backfill-source-keys and act on the listed collisions; `RAG_HYBRID_ENABLED=false`
  — now actually forwarded by the Compose file; reindex-all; rag_invariants until RR-01..RR-14 are 0; then hybrid on).
- The Arabic PDFs the back-fill lists for re-upload.
- Never change `OPENAI_EMBEDDING_MODEL` directly; use the switch with the next model on the API and the worker (RUNBOOK).
- Recommendations of §32; the dependency-hygiene stage (24 advisories, RGM-13); real-provider evaluation and calibrations; the Privacy
  audit (#18); row-level security (ADR-185); RR-25, RR-13; audit #24 (CI from the lock is now done).
- The owner's review of origin's rewrite of 2026-10-27..29.
- The frontend for replacement, cursor listings, the preview, knowledge settings, multipart uploads, the gap report and the 409 answers.
- The line's local branches (rag-remediation-p1/p2/p3, rag-merge-dates, rag-merge-fixes, report branches): keep or delete after review.
- E:\wasla-rag-notes: keep until the Final System-Wide Audit has used the probes.
- Whether and when the canonical branch is merged into main.

## 36. Final Verdict

**RAG REMEDIATION MERGED AND PUSHED, CI GREEN** — **CLEANUP COMPLETE WITH ITEMS LEFT FOR REVIEW** (the three Part worktrees with
untracked audit\ folders; dangling anonymous volumes).

The final question: deployed tomorrow from GitHub onto yesterday's database by DEPLOYMENT.md and the RUNBOOK — every document stayed
findable at every sample of the back-fills and the rebuild (gate 0 gaps); two colleagues uploading one price list at the same second
get one document and the answer a second upload gets, never a 500 or a price served twice; a replaced price list serves one price at
a time; an Arabic PDF is found by the typed words with the exact-word channel off; the embedding switch keeps the preview and every
reply working at every step now that the Compose file passes the next model to the API and the worker; nothing of one workspace reached
another (K-8, isolation gate); the deploy messaged no one; and GitHub's CI, installing exactly the packages the image ships, agrees.
Every retrieval number is structural; how well the real model ranks a real Arabic question is still the unrun real-provider evaluation.
