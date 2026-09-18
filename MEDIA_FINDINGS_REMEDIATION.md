# Wasla — Media Subsystem Findings Remediation

Audit remediated: `media-e5f1` (`MEDIA_SUBSYSTEM_AUDIT.md`) · Date: 2026-09-19

---

## 1. Summary

Every finding in the media audit has been remediated on branch `media-findings-remediation`. MEDIA-01 … MEDIA-15 and MEDIA-17/18 are closed; MEDIA-16 remains accepted future hardening, as the audit classified it. All five production blockers are closed, each with a permanent regression test that drives the real code path and a mutation that the test kills.

The invariant the brief set is now held and tested: **every inbound attachment either becomes usable or reaches a bounded terminal state, and neither network behaviour, parser behaviour, workspace lifecycle, database state nor object-store failure can leave a conversation's reply, or a customer's file, unresolved, unowned or unsafe.**

| | Before | After |
|---|---|---|
| Media reliability & security score | 5.7 / 10 | **8.7 / 10** (§8) |
| Mutations killed | 18 / 30 (12 survivors) | **64 / 64** (all 12 original survivors killed) |
| Model-built whole `tests/` | 4,815 passed, 16 skipped | **5,073 passed, 17 skipped, 0 failed** |
| Migration-built integration + e2e | 2,406 passed, 2 skipped | **2,511 passed, 2 skipped, 0 failed** |
| Media-targeted suite | 550 (28 files, model-built) | **775 passed, 1 skipped** (model) / **358 passed** (migration) |
| Media DB + object invariant violations | 24 attributed to product (§31: 3 unowned objects, 3 retained after purge, 11 stranded rows, 5 blocked conversations, 2 lifecycle reads) | **0** |
| Alembic head | 0064 | **0067** |

**Verdict: MEDIA FINDINGS CLOSED FOR THE CURRENT MEDIA SURFACE, WITH FINAL DEPLOYMENT VERIFICATION DEFERRED.** Real Meta inbound media has not been exercised; DV-1 … DV-8 (§9) remain.

---

## 2. Frozen State and Isolation

```text
audit baseline HEAD        10532a149cfb4099ee5a64bce6499e8efd28d914 (worktree-billing-google-auth)
branch                     media-findings-remediation
worktree                   E:\wasla-media-remediation
final code/test HEAD       8218c49a234a06bd7fc6ff5965182b0f791b84c4   (all authoritative evidence)
docs/report HEAD           the commit adding this file (documentation only)
Alembic                    0064 -> 0067 (0065 media claims, 0066 purge ledger, 0067 audit labels)
```

A dedicated Compose project, **`wasla-media-rem-m7c2`**, with its own PostgreSQL 16 + pgvector, Redis 7.4 and MinIO, on its own network and loopback ports (56961–56963). The runner joins that Redis's network namespace, so every suite that hard-codes `redis://localhost:6379/*` reaches only this Redis.

| Resource | Final evidence run (fresh volumes) |
|---|---|
| PostgreSQL system identifier | `7686990569324081190` |
| Redis run_id | `0febd07c76d3fc007d5603e2e1b409475c07327d` |
| Bucket | `wasla-media` on `wasla-media-rem-m7c2-minio-1`, credential `mediatest/<sentinel>` |
| Runner image / code path | `wasla-media-runner:m7c2`; `app` imported from `/work/app/__init__.py` (this worktree) |

The stack was recreated with fresh volumes twice: once after the first final run caught a deployment-configuration gap (§7), and once more for the run whose numbers are reported here. Every earlier run is superseded.

The shared tree `E:\wasla` was not reset, cleaned, stashed or modified; its untracked reports are untouched. The audit's evidence (`E:\wasla-media-audit`, `E:\wasla-media-audit-e5f1\`, stack `wasla-media-audit-e5f1`) was preserved and not reused. The developer stack and another session's stack (`wasla-tools-rem-4b2a`) ran throughout and were never touched. No `FLUSHALL`; the only `FLUSHDB`s are the test suites' own, against numbered databases of this isolated Redis. **No run in this report is classified CONTAMINATED.**

No production Meta token, customer media, OpenAI key or production storage was used: sentinel credentials, fake Meta and OpenAI transports, synthetic media, isolated MinIO.

---

## 3. Commits

```text
267acfa docs(media): record independent media subsystem audit media-e5f1
3631ce2 fix(media): restrict credential-bearing Meta media hosts and bound media reads      MEDIA-01, -09
fe07392 fix(media): parse customer PDFs in the bounded child and contain reader failures   MEDIA-02, -03
5f6b007 fix(media): make every media attempt claimed, bounded and terminal                 MEDIA-03, -04, -10, -11, -12
95ce217 fix(webhook): bound inbound media metadata and contain refused messages per message MEDIA-05
68c9554 fix(messaging): settle outbound attachment names before Meta is asked              MEDIA-06
c10fa95 fix(media): enforce workspace and number lifecycle before media network and spend  MEDIA-08
fd19624 fix(media): fetch inbound files with the credential of the number they arrived on  MEDIA-13
e8533c7 fix(media): converge workspace purge in the object store through a durable ledger  MEDIA-07
588c6d0 fix(storage): remove the local staging file when a write fails                     MEDIA-14
752c8da feat(observability): instrument media outcomes, recovery, purge and transcription  MEDIA-15
53b4d83 feat(audit): record colleagues opening and sending customer attachments           MEDIA-17
2b02cd9 test(media): pin the guarantees the audit's mutation survivors showed unheld      MEDIA-18
a6da7d1 docs(media): align media runtime contracts with the remediation                   MEDIA-12 docs, all
01c8a1f test(media): pin claim fencing, deferral and the attempt budget                   MEDIA-18
942987a fix(media): end a file retention removed before it was read                       M23 survivor
8218c49 chore(deploy): pass and document the media deadline and Meta host settings        config guard
```

Previous Tools/RAG/AI remediation history was not rebased, squashed or rewritten; the branch is independently mergeable.

---

## 4. Findings Ledger

| ID | Severity | Final status |
|---|---|---|
| MEDIA-01 | HIGH | **CLOSED** (+ DV-1) |
| MEDIA-02 | HIGH | **CLOSED** (+ DV-8) |
| MEDIA-03 | HIGH | **CLOSED** |
| MEDIA-04 | HIGH | **CLOSED** (+ DV-2) |
| MEDIA-05 | HIGH | **CLOSED** (+ DV-3) |
| MEDIA-06 | MEDIUM | **CLOSED** |
| MEDIA-07 | MEDIUM | **CLOSED** (+ DV-6) |
| MEDIA-08 | MEDIUM | **CLOSED** |
| MEDIA-09 | MEDIUM | **CLOSED** |
| MEDIA-10 | MEDIUM | **CLOSED** |
| MEDIA-11 | MEDIUM | **CLOSED** |
| MEDIA-12 | LOW | **CLOSED BY LOCKED RETRY CONTRACT** |
| MEDIA-13 | MEDIUM | **CLOSED** (+ DV-4) |
| MEDIA-14 | LOW | **CLOSED** |
| MEDIA-15 | MEDIUM | **CLOSED** (+ DV-7) |
| MEDIA-16 | INFO | **ACCEPTED FUTURE HARDENING** |
| MEDIA-17 | LOW | **CLOSED BY LOCKED AUDIT DECISION** |
| MEDIA-18 | MEDIUM | **CLOSED** |

### MEDIA-01 — Bearer token sent to any public host

* **Before.** `Authorization: Bearer <platform token>` was re-attached by hand on every hop; a sentinel token reached `cdn.attacker-controlled.example` by redirect and as a first hop. `net.py` claimed httpx stripped it.
* **Root cause.** One control ("is the address public?") was standing in for two; nothing asked whether the host was Meta's.
* **Fix.** One enforcement point, `WhatsAppClient._credential_headers`, builds the header for every read hop (descriptor, file, every redirect) and **refuses** any host outside `META_MEDIA_HOST_ROOTS` (default `graph.facebook.com`, `fbsbx.com`, `fbcdn.net`). Matching is `host == root or host.endswith("." + root)` after IDNA normalisation (`app/core/hostnames.py`); userinfo, address literals, non-https and non-443 ports never carry a credential. Refused hops are never sent, not stripped. The SSRF checks are unchanged. The setting is validated at start-up (wildcards, URLs and empty lists refused). `net.py` corrected.
* **Files.** `app/core/hostnames.py`, `app/integrations/whatsapp/client.py`, `app/core/config.py`, `app/core/net.py`, `app/workers/media_worker.py`, `.env.example`, `docker-compose.prod.yml`.
* **Tests.** `tests/unit/test_media_fetch_boundary.py` (fake transport, sentinel token, fixed DNS map): Graph → allowed CDN carries the token (positive control); allowed → 302 to unapproved public host refused with zero requests to it; descriptor naming a foreign host refused; Graph redirect to foreign host refused; suffix, embedded-root, userinfo, IP-literal, wrong-port names refused; trailing dot normalised; configurable roots honoured; allow-listed names resolving to 10.0.0.5 and 169.254.169.254 still refused by SSRF on first hop and redirect. `tests/unit/test_hostnames.py`: boundary matching, IDNA, refusals, settings parsing.
* **Mutation.** R01 (remove allowlist), R02 (forward token to unknown host), R03 (strip instead of refuse), R04 (suffix matching), M06, M07 — all killed.
* **Deployment.** DV-1: capture the real descriptor, redirect and final download hosts and compare with the configured roots before launch.

### MEDIA-02 — Unbounded in-process PDF parsing

* **Before.** `extract_pdf` ran `pypdf` on the shared event loop; a 25 KB PDF stalled the loop 170 s at 763 MB.
* **Fix.** `MediaReader` reads PDFs through `extract_pdf_bounded`, the RAG-02 child, with limits **unchanged** (300 KB input, 40 pages, 400,000 characters, 768 MB address space, 20 s kill, 2 concurrent). The in-process `extract_pdf` and the parent's `pypdf` import were removed. Past any limit: stored, `SKIPPED` with a fixed media sentence (PD-MEDIA-09).
* **Files.** `app/services/extraction.py`, `app/services/media_reader.py`, `app/services/media_service.py`.
* **Tests.** `test_media_parser_containment.py::test_an_amplifying_pdf_is_killed_on_time_while_the_loop_keeps_running` drives the audit's 25 KB amplifying PDF through the real `MediaWorker` and `MediaReader` beside a 50 ms heartbeat: terminated at the deadline (20 ≤ t < 30 s), loop gap < 1 s, parent RSS growth < 150 MB, row `SKIPPED`, turn released. `test_media_reader.py`: child spawned for every PDF; over-byte PDF refused without spawning; text-limit PDF is a decision; no in-process parser left (`test_extraction.py`).
* **Mutation.** R05 (restore in-process parsing) and R06 (remove the child's kill deadline) killed.

### MEDIA-03 — Poison file or dead letter strands the conversation

* **Before.** 1,457/6,000 fuzzed PDFs escaped the catch list; the job dead-lettered, the row stayed `downloading`, and every later attachment in the conversation went unanswered.
* **Fix.** Four layers. (1) Parser crashes happen in the child and arrive as one fixed refusal. (2) `MediaService._read_stored` contains any unexpected reader `Exception` as `FAILED` (`reader_failed`), never `CancelledError`/`SystemExit`/`KeyboardInterrupt`. (3) **Terminal-on-exhaustion**: a dead-lettered media job calls `MediaWorker._abandon`, which fresh-loads the row and, if unresolved and not held by another live claim, makes it `FAILED` (`abandoned`) and re-evaluates the conversation under the same gate — idempotent. (4) **Stranded recovery**: `MediaRecoveryWorker` (under the `media` kind, every minute) finds rows whose claim is older than the derived claim lease, or unclaimed longer than the queue's retry budget (excluding events still owed to inbound recovery), requeues them while attempts remain and gives them up after `MAX_ATTEMPTS` with the turn released. Horizons are derived in `app/services/media_horizons.py`: claim lease = download deadline + 2 × S3 timeout + understanding deadline + 60 s (330 s default); unclaimed horizon = retry attempts × (visibility × 4/3 + max backoff) (1,175 s default). A purged-but-unread row is now terminal too (§7, M23).
* **Files.** `app/services/media_service.py`, `media_recovery_service.py`, `media_horizons.py`, `media_outcomes.py`, `app/workers/media_worker.py`, `media_recovery.py`, `runner.py`, `app/repositories/media_repository.py`, `app/db/models/media.py`, migration `0065`.
* **Tests.** Five saved poison PDFs (AttributeError, AssertionError, KeyError, TypeError, IndexError; byte-exact fixtures) through the real worker → `SKIPPED` + one turn each; a later readable attachment still gets its turn; an unexpected `KeyError` from a reader → `FAILED` + turn; cancellation not swallowed. `test_media_attempt_recovery.py`: dead-lettered job via real Redis → `FAILED abandoned`, one agent job, second terminalisation writes nothing; worker killed after its claim → untouched inside the lease, requeued after, next attempt `READY`; a six-row sweep matrix (live, crashed, exhausted, fresh, lost, owed-to-inbound).
* **Mutation.** M25, R07 (remove terminal-on-exhaustion), R08 (remove recovery), R27–R29 killed.

### MEDIA-04 — Descriptor failure escapes, is retried, strands

* **Fix.** The descriptor lookup is inside `MediaService._fetch`'s classified boundary with the download. The client now types refusals: `MediaUnavailableError` (400/404/410 → `SKIPPED`), `MediaCredentialRefusedError` (401/403/code 190 → `FAILED`), `MalformedMediaDescriptorError`, `MediaHostRefusedError`; 429/5xx/transport after the client's three attempts → `FAILED`. No exception reaches the queue, so nothing is queue-retried. Reasons are a closed vocabulary with fixed sentences ≤ 200 characters; no provider string reaches `last_error`.
* **Tests.** `test_media_terminal_states.py` with the real client over a counting transport: 404/410/400 → 1 Graph call, `SKIPPED`; 401/403 → 1 call, `FAILED`; malformed → 1 call; 429/500/503/connect → 3 calls; all release one turn. A 600-character provider MIME string cannot reach the reason. `test_media_outcomes.py` bounds every sentence.
* **Mutation.** R09 (probe outside containment), R10 (retry permanent 4xx), R30 (provider string in reason) killed.
* **Deployment.** DV-2.

### MEDIA-05 — Dead DataError net; long filename causes a retry storm

* **Fix.** (1) Inbound filenames normalised before any flush (`display_filename`: NFC, control characters including NUL removed, lone surrogates replaced, ≤ 300 characters keeping a short extension, Unicode preserved); over-wide declared MIME types dropped; over-long media handles treated as none. Column widths come from the same constants. (2) One savepoint per message in `WhatsAppIngestionService.ingest`, flushed inside it; only SQLSTATE class 22 (`app/db/errors.is_data_exception`) is contained there. (3) The route's net catches `DBAPIError` and re-raises anything outside class 22. Signature verification and event dedupe unchanged.
* **Tests.** `test_webhook_media_metadata.py` through the real app (lifespan, HMAC, `CommittingRoute`) and real PostgreSQL: 300 / 301 ASCII / 301 Arabic / 2,000 / control / NUL filenames → 200 `accepted`, both messages stored, bounded name; redelivery a duplicate; a 40-digit sender refused alone inside a delivery while siblings are stored; route net fires on 22001; 40P01/40001/08006/57P01 still answer 5xx. `test_database_error_classes.py` pins the classifier against real asyncpg errors (22001, 22021, 22000, 23505, 42P01).
* **Mutation.** M28, R11 (remove bound), R12 (swallow wrong class), R13 (remove savepoint) killed.
* **Deployment.** DV-3.

### MEDIA-06 — Outbound name fails after delivery

* **Fix.** `send_media` canonicalises the name first (`require_storable_filename`); over-long or control-bearing names are refused with 422 before any Meta call. One canonical name is recorded; the Meta-safe name derives from it.
* **Tests.** `test_outbound_attachment_names.py` with a recording Meta transport: 301, 2,000, NUL, BEL → 422, **0 Meta calls**, 0 message rows, 0 media rows, no object; 300-char, Arabic and padded names sent and recorded canonically.
* **Mutation.** R14 (validation moved after the send) killed.

### MEDIA-07 — Purge leaves unrecorded customer objects

* **Fix.** `media_purge_objects` (migration 0066): every key is written **by the statement that deletes its row** (`DELETE … RETURNING` into the ledger, in the purge transaction). The purge worker drains after commit with no transaction across a delete and removes a ledger row only when the store confirms; refusals keep their row (attempts counted); the worker re-polls every 5 minutes while deletes are owed. Keys pending at the purge wait out `MEDIA_UPLOAD_GRACE_SECONDS`; a media attempt that finds its row gone after writing deletes its own object (post-purge write fence). A workspace is fully purged when `purged_at` is set and no ledger rows remain. The ledger is classified as retained by the purge and references tenants `RESTRICT`.
* **Tests.** `test_workspace_purge_objects.py`, real MinIO, presence before absence: refused deletes → workspace purged, 2 owed, objects present; later pass → 0 objects, 0 owed. Crash after the purge commit → converges next pass. Paused writer whose PUT lands after the purge (object proven present after PUT) → gone before the writer returns; ledger waits for the grace then settles. Writer dying after its late PUT → object survives an early pass, deleted after the grace.
* **Mutation.** M14, R15 (settle without delete), R16 (no ledger), R17 (remove write fence) killed.
* **Deployment.** DV-6. Objects orphaned by purges before 0066 cannot be derived; the runbook documents prefix deletion.

### MEDIA-08 — Lifecycle ignored

* **Fix.** `MediaRepository.serving` reads tenant status/deletion and account status/release as columns, tenant-scoped, **before Meta, before the object write and before any paid read**. Suspended, deleted, released/paused → `SKIPPED` with no spend. A released number's file is written terminal at the webhook and never queued. Closed conversations and disabled agents are not refusals (PD-MEDIA-05).
* **Tests.** `test_media_lifecycle.py`: suspended/deleted/released before the job → 0 probes, 0 fetches, 0 reads, no object; suspension mid-download → no write, no read; after storage → no paid read, object kept; closed conversation still read; fresh, tenant-scoped read; released number at the webhook → row terminal, nothing queued.
* **Mutation.** R18, R19, R32 killed.

### MEDIA-09 — Uncapped error bodies

* **Fix.** Error bodies read to 16 KiB on both paths; Graph descriptors to 64 KiB (oversize is a malformed descriptor), template pages to 4 MiB; nothing logs the body.
* **Tests.** 404/403/400/410 with 200 MB streams from both the file host and Graph → bytes pulled ≤ 16 KiB + one chunk; 64 MB descriptor refused within its bound and not retried.
* **Mutation.** R20 killed.

### MEDIA-10 — No total deadline

* **Fix.** `asyncio.timeout(MEDIA_DOWNLOAD_DEADLINE_SECONDS)` (90 s) around descriptor + download; `MEDIA_UNDERSTANDING_DEADLINE_SECONDS` (120 s) around read-back + reading. Start-up refuses a download deadline ≥ `QUEUE_VISIBILITY_TIMEOUT_SECONDS` (120 s). **Relationship:** one download ends ≤ 90 s < 120 s visibility; leases are also renewed while the worker lives, so a live worker's job is never reclaimed, and a dead worker's claim is recovered by the sweep, never raced by a second download.
* **Tests.** A body dripping one byte every 50 ms through the real client → `FAILED timeout` at the deadline, the stream stops advancing, no object, turn released; validator tests; defaults pinned.
* **Mutation.** M10, R21 killed (§7 for R21's first run).

### MEDIA-11 — Transaction and row lock held across the Meta fetch

* **Fix.** Claim → commit → network with no connection → fenced short transactions for intent, finalisation and result; the claim replaces the row lock (see ADR-110).
* **Tests.** `test_no_connection_is_held_while_meta_or_the_reader_is_asked`: a pool of **one** connection; during the descriptor, the download and the read, `pool.checkedout() == 0` and a second session on that same pool reads the committed claim. Duplicate race on two pools with the first parked inside the download: second attempt stands aside with 0 probes/fetches/reads; one download, one read, one object, one turn (P7-01 had two downloads). Crash-window recovery as MEDIA-03.
* **Mutation.** R22 (hold transaction across fetch) killed.

### MEDIA-12 — Retry documentation

* **Fix.** PD-MEDIA-08 locked in code and docs: provider-client retries only, then terminal, conversation released at once; `MEDIA.md` no longer promises a queue-level retry and states inbound video is skipped before download. `MAX_ATTEMPTS`/`is_exhausted` are kept because they now bound attempts that never finished (the recovery budget).
* **Tests.** `test_a_failed_file_is_never_retried_by_a_later_job` (second job: 0 Meta calls); `test_an_inbound_video_is_skipped_before_it_is_downloaded`; the adapted atomicity test (§7).

### MEDIA-13 — Workspace credential ignored

* **Fix.** At claim time `MediaService._client_for` loads the conversation's account server-side and resolves its token through `CredentialService`: own credential if present, platform token otherwise; an undecryptable workspace credential is never downgraded; no credential → `FAILED credential_unavailable`. The token lives only in the per-attempt client.
* **Tests.** `test_media_credentials.py` through the worker's production client path: workspace token on every hop and platform token on none; platform token only for a number without its own; unreadable and missing credentials → 0 Meta requests, turn released; neither token on the row or in any formatted log.
* **Mutation.** R23 killed. **Deployment.** DV-4.

### MEDIA-14 — `.partial` kept on a full disk

* **Fix.** `LocalMediaStorage.put_at` removes the staging file on any failure (ENOSPC, rename, cancellation) and re-raises the original.
* **Tests.** Deterministic injections, plus a real **2 MB tmpfs** filled by real writes: 0 partial files, 248 KB free (audit: 248 KB partial, 0 KB free).
* **Mutation.** R24 killed.

### MEDIA-15 — No alerts, no outcome metrics, transcription uninstrumented

* **Fix.** Counters `wasla_media_outcomes_total{outcome}` (closed domain held equal to the reason vocabulary by a test; includes download failure classes), `wasla_media_recovery_total{outcome}`, `wasla_media_purge_objects_total{outcome}`; transcription via `ProviderCall` (`operation="transcribe"`); scrape-time gauges `wasla_media_stranded`, `…_oldest_age_seconds`, `wasla_media_purge_deletes_owed`, `…_oldest_age_seconds`. Alerts `MediaStranded`, `MediaProcessingFailureSpike` (failure subset only, volume floor), `MediaPurgeDeletesFailing`, `TranscriptionFailureRate`, `MediaUploadQuarantined`; runbook entries with symptom, query, causes and safe action.
* **Tests.** `test_media_observability.py` renders each through the real exposition; promtool tests prove each rule fires, clears, and stays silent on steady unsupported files and on one failure on a quiet night.
* **Mutation.** R25, R31, and two alert mutations under promtool killed. **Deployment.** DV-7 (delivery).

### MEDIA-16 — No re-verification on read

**Accepted future hardening**, unchanged in substance: exploiting it requires bucket-write compromise, and the private bucket, authenticated tenant route, `attachment` disposition and `nosniff` stand. Documented in `MEDIA.md`.

### MEDIA-17 — No audit of staff access

* **Fix (PD-MEDIA-06).** `media_downloaded` (after a served read) and `media_sent` (human send not provably failed), actor = authenticated member as `USER`, `meta` = conversation/message/media ids only. Migration 0067.
* **Tests.** `test_media_staff_audit.py`: safe metadata only (sentinel filename, caption and key absent); another workspace's file cannot be opened or audited; refused sends and worker reads not audited. Endpoint tests: recorded after a served file, not for a refused or unservable one.
* **Mutation.** R26 killed.

### MEDIA-18 — Mutation survivors

All twelve original survivors now have subject-matching killers (§5).

---

## 5. Mutation Testing

Runner `E:\wasla-media-rem-m7c2\mutations\run.py`: one mutation at a time, exact single-match anchors, **only the subject-matching test files run** (so an unrelated test cannot count), original bytes restored and SHA-256 verified. The worktree was clean after every batch.

**Applied 64 · killed 64 · survived 0 · inapplicable 0.** Result shape: **survivor → test added → rerun killed** (M23), and one first-run hang converted to a failing test (R21) — not a single clean pass.

The twelve original survivors:

| ID | Property | Killer |
|---|---|---|
| M06 | redirect-hop validation | `test_media_fetch_boundary::…private_address_is_refused_on_a_redirect` |
| M07 | first-hop validation | `test_media_fetch_boundary::…private_address_is_refused_on_the_first_hop` |
| M10 | media client timeout | `test_media_mutation_gaps::test_the_media_http_client_has_a_finite_timeout_on_every_phase` |
| M12 | intent hash conflict | `test_media_mutation_gaps::test_a_retry_with_different_bytes_cannot_reuse_the_pending_key` |
| M13 | finalize only pending | `test_media_mutation_gaps::test_a_stale_finaliser_cannot_convert_a_row_somebody_else_settled` |
| M14 | purge deletes objects | `test_workspace_purge_objects::test_a_purge_whose_deletes_are_refused_…` (MinIO) |
| M21 | conversation gate lock | `test_media_release_race::test_two_files_finishing_together_release_exactly_one_turn` |
| M25 | reader crash containment | `test_media_parser_containment::test_an_unexpected_reader_exception_…` |
| M26 | inbound capacity | `test_media_mutation_gaps::test_an_inbound_file_past_the_workspaces_capacity_is_not_stored` |
| M28 | webhook data-exception net | `test_webhook_media_metadata::test_a_content_refusal_outside_the_savepoints_…` |
| M29 | S3 403 ≠ missing | `test_object_store_refusals::test_a_refused_head_is_not_an_absent_object[403]` |
| M30 | credential logging | `test_media_fetch_boundary::test_a_successful_fetch_logs_neither_the_token_nor_the_signature` |

The M21 killer is a genuine race: two workers on two pools, a barrier at the release decision, and one backend observed in `pg_stat_activity` waiting on the conversation lock; without the lock both workers count while the sibling is uncommitted and **no** turn is released. M30 proves the bearer reached an allow-listed host before asserting the logs hold neither the token nor the CDN signature; the S3 half proves requests were signed with the sentinel key; the workspace-credential half is in `test_media_credentials.py`.

The other 18 audit mutations (M01–M05, M08, M09, M11, M15–M20, M22–M24, M27) and 34 remediation mutations (R01–R32, two alert rules) are all killed; per-mutation killers are in `mutations/results_*.json`. Remediation mutations cover every item the brief listed: allowlist, unknown-redirect forwarding, bounded child, in-process parsing, parser containment, terminal-on-exhaustion, stranded recovery, probe outside containment, permanent-404 retry, inbound filename bound, wrong DBAPIError class, per-message savepoint, outbound validation ordering, purge completion before convergence, post-purge write fence, lifecycle gate, suspended processing, error-body cap, total deadline, transaction across fetch, platform-vs-workspace credential, ENOSPC partial, stranded metric, media alert, staff-download audit — plus claim deferral, claim fence, attempt budget, provider text in reasons, transcription counting and released-number queueing.

---

## 6. Runtime Proof

### Fault-injection matrix (final HEAD, real worker, Redis, migration-built PostgreSQL, MinIO)

| Injection | DB state | Object | Turn | Retry / calls | Metric | Visibility |
|---|---|---|---|---|---|---|
| Descriptor 404 | `skipped` unavailable | none | released | 1 Graph call, 0 queue retries | `unavailable` | reason on row |
| Descriptor 403 | `failed` credential_refused | none | released | 1 call | `credential_refused` | reason, failure-spike alert |
| Descriptor 429 | `failed` rate_limited | none | released | 3 (client) | `rate_limited` | reason |
| Descriptor 500 | `failed` download_failed | none | released | 3 (client) | `download_failed` | reason |
| Slow drip | `failed` timeout at 2.2 s (2 s deadline) | none | released | none after | `timeout` | `media.download_timed_out` |
| Oversized stream | `skipped` oversize | none | released | none | `oversize` | reason |
| Type mismatch | `skipped` type_mismatch | none | released | none | `type_mismatch` | `media.type_mismatch` |
| Object PUT failure | `failed` storage_failed, intent `pending` | absent → reconciler clears | released | none | `storage_failed` | reconciliation metrics |
| Crash after DOWNLOADING commit | `downloading` + claim → requeued → `ready` | present | released once | attempt 2 | recovery `requeued` | stranded gauge until swept |
| Crash after PUT before finalize | `downloading`/`pending` → reconciler `stored` → requeued → `ready`, same key | present | released | attempt 2 | recovery `requeued` | as above |
| Poison PDF | `skipped` unreadable | stored | released | none | `unreadable` | reason |
| Transcription outage | `failed` provider_failed | stored | released | 3 (client) | `provider_failed`, transcribe provider call | `TranscriptionFailureRate` |
| Suspended / deleted workspace | `skipped` lifecycle | none | released (AI worker refuses) | 0 Meta fetches, 0 paid reads | `workspace_*` | reason |
| Two concurrent attachments | both `ready` | 2 | exactly 1 | — | — | — |
| Purge delete failure | purged, 2 owed → 0 | present → absent | n/a | next pass | `purge_objects{failed}` then `{deleted}` | owed gauge, `MediaPurgeDeletesFailing` |
| Purge / write race | writer deferred | PUT landed → deleted by writer; ledger settles after grace | n/a | — | — | owed gauge |
| Local ENOSPC | `StorageError` | 0 partials, 248 KB free | — | — | — | `media.store_failed` |

A final recovery pass found nothing stranded and no deletes owed.

### Invariant sweep (same population: 18 tenants — 1 suspended, 3 deleted, 2 purged — 17 media rows, 7 stored objects)

| Invariant | Population | Violations |
|---|---|---|
| cross-tenant row link | 17 | 0 |
| unsafe object key / key-prefix mismatch / duplicate key / oversized stored | 7 | 0 |
| failed/skipped marked ready | 17 | 0 |
| stored row without object | 7 | 0 |
| public object (anonymous GET) | 7 | 0 |
| unresolved beyond processing horizon | 17 (0 unresolved) | 0 |
| ready media blocked by older unresolved sibling | 17 | 0 |
| paid processing for suspended/deleted workspace | 2 | 0 |
| object without owning row or purge record (bucket listing, run tenants) | 7 | 0 |
| object retained for a fully purged workspace | 2 workspaces | 0 |
| new object for a released number | released-number test | 0 |

Populations include success, failure, skip, lifecycle-refused, crash-recovered, purge-failure, purge-race and concurrent cases. No deliberate test corruption was needed.

---

## 7. Honest Notes

* **One existing test changed meaning.** `test_a_retry_after_a_failed_write_reuses_the_committed_key` asserted that a `FAILED` row is downloaded again, which PD-MEDIA-08 retires. It became two tests: a refused write is terminal and keeps its intent (still the M11 killer), and key reuse is proven on a genuine crash-retry.
* **M23 survived the first matrix.** The purge guard was only reachable for a purged row that was never read, which it left unresolved. That row is now terminal (`942987a`), with a direct test; the rerun killed it.
* **R21 hung rather than failed** on its first run (the drip test had no bound of its own); the test now bounds itself and the rerun killed it.
* **The first final gate run failed** 5 deployment-configuration guard tests: the three new settings were missing from `.env.example` and `docker-compose.prod.yml`. Fixed in `8218c49`; the stack was recreated and everything rerun. The compose default for `META_MEDIA_HOST_ROOTS` is the documented list rather than empty, because an empty list is refused at start-up.
* **Graph is asked twice per download** (probe, then fetch). Pre-existing, bounded, left as is.
* **Residual:** if Redis refuses the recovery sweep's agent enqueue at that moment, the terminal row is committed but the turn is only logged and counted (`wasla_media_recovery_total{outcome="release_failed"}`). The requeue path self-heals; the release path does not.
* **Behavioural consequences, by locked decision:** customer PDFs over 300 KB are stored but not read; a worker that dies mid-download delays that file by up to the claim lease (330 s); failed files are final.

---

## 8. Score

| Dimension | Weight | Before | After | Weighted | Remaining deduction |
|---|---|---|---|---|---|
| Tenant isolation | 0.12 | 9.5 | 9.5 | 1.140 | storage deliberately not an authz boundary |
| URL / SSRF safety | 0.10 | 7 | 9 | 0.900 | production host list unverified (DV-1) |
| Credential handling | 0.10 | 3 | 8.5 | 0.850 | cross-app workspace credential unverified (DV-4) |
| Size / resource bounding | 0.10 | 4 | 8.5 | 0.850 | files held whole in memory (25 MB) |
| Type validation | 0.06 | 9 | 9 | 0.540 | pre-download routing on declared type |
| Parser safety | 0.08 | 2 | 8.5 | 0.680 | RLIMIT only on POSIX; container limits DV-8 |
| Storage correctness | 0.06 | 8 | 8.5 | 0.510 | no read re-verification (MEDIA-16) |
| DB/object atomicity | 0.06 | 7 | 8.5 | 0.510 | a writer stalled past the upload grace can still orphan |
| Idempotency / replay | 0.05 | 8 | 9 | 0.450 | Graph descriptor asked twice |
| Lifecycle / deletion | 0.07 | 4 | 8.5 | 0.595 | versioned buckets (DV-6); pre-0066 orphans not derivable |
| Privacy / logging | 0.05 | 8.5 | 9 | 0.450 | EXIF preserved by decision |
| Failure containment | 0.08 | 2 | 9 | 0.720 | release-enqueue residual during a Redis outage |
| Observability | 0.03 | 3 | 8.5 | 0.255 | alert delivery unverified (DV-7) |
| Testing | 0.02 | 5 | 9.5 | 0.190 | real Meta never exercised |
| Operational readiness | 0.02 | 4 | 5 | 0.100 | DV-1 … DV-8 outstanding |
| **Total** | **1.00** | **5.7** | | **8.74** | |

**Media Reliability & Security Score: 5.7 → 8.7 / 10.** Not higher, because deployment-only uncertainty (DV-1 … DV-8) and deliberately deferred capabilities remain visible.

---

## 9. Product Decisions

| ID | Applied |
|---|---|
| PD-MEDIA-01 | Retention unchanged: `MEDIA_RETENTION_DAYS=0` default. Only purge/retention correctness changed. |
| PD-MEDIA-02 | Malware scanning deferred; documents stored, served `attachment` + `nosniff`, never executed. |
| PD-MEDIA-03 | EXIF/GPS preserved; documented. |
| PD-MEDIA-04 | Supported types unchanged; no OCR, video understanding, OOXML parsing, transcoding or image decoding. Inbound video documented as skipped before download. |
| PD-MEDIA-05 | Suspended / soft-deleted / released: no Meta fetch, no object, no paid processing, terminal row; historical objects follow retention; closed conversation / disabled agent still stored and read. |
| PD-MEDIA-06 | Staff download and send audited with internal ids only. |
| PD-MEDIA-07 | Quotas and pricing unchanged; inbound capacity enforcement tested (M26). |
| PD-MEDIA-08 | Bounded client retries, then terminal, then release; no queue-level retry. |
| PD-MEDIA-09 | PDF understanding through `extract_pdf_bounded` with RAG limits unchanged; over-limit PDFs stored and `SKIPPED`. |

## 10. Deployment Verification Backlog

* **DV-1** Real Meta inbound media: capture descriptor host, every redirect host and the final download host; compare with `META_MEDIA_HOST_ROOTS` before launch.
* **DV-2** Meta status codes for expired, deleted and foreign media ids (classification in §4).
* **DV-3** Meta's real filename and caption limits (inbound names are bounded regardless).
* **DV-4** Download with a workspace credential under a different Meta app/business.
* **DV-5** OpenAI's real image and transcription size limits against 25 MiB.
* **DV-6** Production bucket: private policy, versioning with non-current-version expiry, lifecycle, SSE.
* **DV-7** Retention, purge and media-recovery loops running; delivery of the new media alerts.
* **DV-8** Container CPU/memory limits and media worker topology (the PDF child's address-space limit applies on Linux).

Nothing locally reproducible was moved here.

## 11. Future Requirements (not implemented)

Malware scanning hook between intent and finalisation; an isolated decoder service before any local image/video/OCR processing; streaming object I/O if `MEDIA_MAX_BYTES` grows; object deletion as part of any future message/conversation/contact delete API; an EXIF policy; channel-specific media host policies for new channels; optional read re-verification (MEDIA-16).

## 12. Final Evidence (frozen HEAD `8218c49`, fresh volumes)

```text
ruff check app tests                    All checks passed
black --check app tests                 579 files unchanged
mypy app tests                          Success: no issues found in 579 source files
alembic heads / check                   0067 (head) / No new upgrade operations detected
alembic round trip                      0067 -> 0064 -> 0067, check clean after each migration
promtool check config                   SUCCESS, 39 rules
promtool test rules                     SUCCESS
model-built whole tests/                5073 passed, 17 skipped, 0 failed
migration-built integration + e2e       2511 passed, 2 skipped, 0 failed
media-targeted, model-built (45 files)  775 passed, 1 skipped
media-targeted, migration-built         358 passed
cross-boundary (messaging/webhook,
  workers/queues, AI, RAG bounded PDF,
  purge/retention, credentials)         606 passed, 1 skipped
local ENOSPC on 2 MB tmpfs              6 passed; 0 partials, 248 KB free
fault matrix                            17 cases, all terminal, all released
invariant sweep                         0 violations
mutations                               64 applied, 64 killed, 0 survived
```

Skips: 11 real-provider tests (no key), 3 schema-parity tests (migration-only), 1 each AI/tool keep-data invariants, 1 tmpfs test outside a size-limited filesystem (run separately above).

Not merged, not pushed. Awaiting merge instructions.
