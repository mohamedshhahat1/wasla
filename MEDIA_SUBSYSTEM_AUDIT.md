# Wasla — Media Subsystem Independent Audit

Audit id: `media-e5f1` · Audit only, no production code changed · Date: 2026-09-18

---

## 1. Executive Summary

Wasla's media subsystem is, in most of the places an attacker would look first, carefully built. Object keys are server-generated (`{tenant}/{yyyy}/{mm}/{uuid}{ext}`), validated against a pattern and a containment check on every read, and no filename can reach a path, a key or a subprocess. The bucket is private, nothing is presigned, and files are streamed back only through an authenticated, tenant-scoped route with `Content-Disposition: attachment` and `nosniff`. File types are decided from the bytes against an exact allowlist. The success-path download is capped by bytes actually received rather than by `Content-Length`. The DB/object write protocol commits intent before the object exists, and a reconciler settles interrupted writes without ever listing the bucket. Every SSRF attempt in this audit was refused: 44 of 44, spanning loopback, RFC 1918, link-local and metadata addresses, Docker service names, decimal, hex and octal IPs, IPv4-mapped IPv6, userinfo tricks, non-https schemes, and each of those as both first hop and redirect. Cross-tenant access against real MinIO objects failed on every path. No secret, token or CDN signature appeared in any log.

What the audit found is that the subsystem is strong at *deciding what a file is and where it lives*, and weak at *surviving what happens while it is being fetched and read*. Across the five blockers, one malformed or slow input either stops the customer's conversation from ever being answered again, or stops every worker in the process.

**Five blockers.**

* **MEDIA-01: the Meta bearer token is sent to any public host.** Redirects are followed by hand, and `Authorization: Bearer <platform token>` is re-attached on every hop. The first-hop URL from Meta's descriptor is equally unrestricted. There is no host allowlist, only a "public address" check. A sentinel token reached `cdn.attacker-controlled.example` both by redirect and by first hop. `app/core/net.py` states the opposite ("httpx strips `Authorization` when a redirect leaves the origin"), which is true only of httpx's own redirect following, which this client disables.
* **MEDIA-02: a customer's PDF is parsed in-process, on the event loop every worker shares, with no byte, time or memory bound.** The knowledge base parses PDFs in a killable child (300 KB, 20 s, 768 MB; RAG-02). The message path does none of that and accepts 25 MB. A **25 KB** PDF froze the event loop for **170 s** and reached **763 MB RSS**; the knowledge path refused the same file in 20 s at 68 MB.
* **MEDIA-03: a parser exception outside `extract_pdf`'s catch list dead-letters the job and leaves the row `downloading` forever.** Fuzzing gave 1,457 of 6,000 small PDFs (`KeyError`, `AttributeError`, `AssertionError`, `IndexError`, `TypeError`), all under 610 bytes. Because the agent reply is gated on "no unresolved media on this conversation", **every later attachment in that conversation is also never answered**. A readable follow-up document reached `ready` and still released 0 turns (control: 1).
* **MEDIA-04: a failed descriptor lookup (`probe_media`) escapes containment entirely.** A permanent Meta 404/400/403 is retried 5 times, and 5xx/429 cost 15 HTTP calls. The row then stays `pending` forever, with the same permanent block on the conversation. Nothing re-examines it: inbound recovery only rescues events that were never enqueued.
* **MEDIA-05: the webhook's "PostgreSQL refused this content" safety net is dead code.** Under asyncpg, value-too-long, NUL and overflow errors surface as `sqlalchemy.exc.DBAPIError`, never `DataError`, so `except DataError` in `receive_events` never fires, and no test reaches it. A customer-supplied document `filename` over 300 characters therefore returns **500 on every Meta retry**, losing the sibling text message in the same delivery too. This reopens the MSG-03 retry storm for this class of input. Whether Meta passes such a filename through is deployment verification; the dead handler and the unbounded column are local facts.

**Below the blockers:**

* **MEDIA-06:** an outbound attachment whose filename is over 300 characters or contains NUL is delivered by Meta, then fails to record. The request errors and the message stays `pending`.
* **MEDIA-07:** a workspace purge whose object deletes fail marks the workspace purged and leaves its customer files in the bucket **with no row anywhere naming them**. A purge that races an in-flight write does the same.
* **MEDIA-08:** the media worker ignores workspace lifecycle and released numbers. Suspended and deleted workspaces still get downloads, paid vision/transcription and new objects, contradicting `docs/AI_AGENTS.md:89`.
* **MEDIA-09:** a 4xx error body is read without the cap its own comment claims (200 MB consumed against a 1 MB cap).
* **MEDIA-10:** no total deadline on a download.
* **MEDIA-11:** a transaction and row lock are held across the Meta fetch, contrary to ADR-080.
* **MEDIA-12:** `FAILED` is never retried, although `MEDIA.md` says it is.
* **MEDIA-13:** media download ignores per-workspace credentials (ADR-034).
* **MEDIA-14:** a full local disk keeps its `.partial` file.
* **MEDIA-15:** no alert consumes any media metric, and transcription is uninstrumented.

Mutation testing killed **18 of 30**. The 12 survivors cluster on the fetch path's SSRF checks and timeout, the purge's object deletion, the one-reply lock and parser containment (§32).

**Media Reliability & Security Score: 5.7 / 10** (§38). **Verdict (§39): not production-ready.** The storage, typing and tenancy foundations are sound. Five blockers sit on the fetch/parse/turn path and must be closed before customer media is accepted in production.

---

## 2. Frozen Repository State

```text
git rev-parse HEAD            10532a149cfb4099ee5a64bce6499e8efd28d914   (matches expected baseline)
git branch --show-current     worktree-billing-google-auth
alembic heads                 0064 (head)                                (matches expected)
alembic check                 No new upgrade operations detected. (exit 0)
```

`git status --short` at start: the nine pre-existing untracked reports named in the session state (`AI_FINDINGS_REMEDIATION.md` … `WORKERS_QUEUES_FINDINGS_REMEDIATION.md`), plus two `.tmp_pytest` subdirectories that were already unreadable ("Permission denied" warnings). None was touched. `git worktree list` showed two pre-existing prunable entries (`.../rag_baseline`, `.../wasla_baseline`); they were left alone.

All code was read and run in a **detached audit worktree at the frozen HEAD**, `E:\wasla-media-audit` (`git worktree add --detach … 10532a1`). It was clean (`git status --short` empty) before mutation testing and after it (§32). Audit artefacts live outside the repository in `E:\wasla-media-audit-e5f1\` (`infra/`, `probes/`, `mutations/`, `logs/`). **This report is the only file this audit adds to `E:\wasla`.** Nothing was reset, stashed, cleaned or committed.

---

## 3. Isolation / Test Hygiene

A dedicated Compose project, **`wasla-media-audit-e5f1`**, owns its own PostgreSQL 16 + pgvector, Redis 7.4 and MinIO. Each has its own containers, volumes, network (`wasla-media-audit-e5f1_default`) and loopback host ports (`127.0.0.1:56941/56942/56943`). The runner joins Redis's network namespace, so suites hard-coding `redis://localhost:6379/*` reach only this Redis.

```text
worktree                E:/wasla-media-audit (detached, 10532a1)
Docker project          wasla-media-audit-e5f1
PostgreSQL              16.15, system_identifier 7686830229063397415
                        dbs: wasla_models, wasla_migrations, wasla_dev, wasla_alembic, wasla_invariants
Redis run_id (runner)   26b90fa88604b37d0a284930428aebb731935ad9  == wasla-media-audit-e5f1-redis-1
  other Redis on host   wasla-redis-1 598df7c4…, wasla-tools-rem-4b2a-redis-1 8b18916b…  (never touched)
Object store            MinIO http://minio:9000, bucket wasla-media, credential mediatest/<sentinel secret>
Temp                    per-test pytest tmp dirs; ENOSPC probe on a 2 MB tmpfs in a throwaway container
app imported from       /work/app/__init__.py (the audit worktree)
Runner image            wasla-media-runner:e5f1 (re-tag of the previous audit's image;
                        `git diff --stat 6058c74 10532a1 -- pyproject.toml` is empty)
```

The developer stack (`wasla-api-1`, `wasla-postgres-1`, `wasla-redis-1`) and another session's stack (`wasla-tools-rem-4b2a`) ran throughout. They were never stopped, flushed or queried. No `FLUSHALL`/`FLUSHDB`, `git reset`, `git clean` or `git stash` was run.

Databases were partitioned by purpose. Baseline gates used `wasla_models`/`wasla_migrations`, and probes committed real rows into a migration-built `wasla_invariants` with Redis DB 7. Mutation runs used `wasla_dev`, and the media-targeted suite used `wasla_dev`/`wasla_migrations` after the baseline finished. Probes ran concurrently with the baseline suites on different databases. That affects only wall-clock timing in P5, whose results are relative (same machine, same run, message path vs knowledge path).

**No run in this report is classified CONTAMINATED.**

---

## 4. Baseline Gates

Frozen HEAD, isolated resources, fresh volumes.

| Gate | Result |
| --- | --- |
| `ruff check app tests` | All checks passed (exit 0) |
| `black --check app tests` | 549 files would be left unchanged (exit 0) |
| `mypy app tests` | Success: no issues found in 549 source files (exit 0) |
| `alembic heads` / `upgrade head` / `check` | `0064 (head)`; upgrade clean; no new operations (exit 0) |
| **Model-built whole `tests/`** | **4,815 passed, 16 skipped, 0 failed** (975 s) |
| **Migration-built `tests/integration` + `tests/e2e`** | **2,406 passed, 2 skipped, 0 failed** (882 s) |

Skips, all intended: 11 × `real_provider/test_openai_contract.py` (no key), 3 × `test_schema_parity.py` (migration-only), and 1 each from `test_ai_invariants.py` and `test_tool_invariants.py` (keep-data runs only). Migration-built skips only the last two.

---

## 5. Media Inventory

The repository was searched for media, image, audio, video, document, file, attachment, sticker, voice, mime/content_type, filename, object storage/MinIO/S3/bucket, presign, upload/download/stream, tempfile, ffmpeg/ffprobe/Pillow, PDF, transcription/whisper, media_id/media_url, multipart and blob.

| # | Path | Class | Source → destination | Entrypoint → worker/service | Storage / DB | Size bound | Timeout / retry | Validation / parser | Customer outcome | Retention |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Inbound WhatsApp image/sticker | inbound customer | Meta CDN → store → OpenAI vision (data URL) | webhook → `MediaQueue` → `MediaWorker` → `MediaService.download/understand` → `MediaReader._describe` | `message_media` + object `{tenant}/…` | descriptor pre-check + streamed actual-byte cap, `MEDIA_MAX_BYTES` 25 MB | httpx 10 s per operation, 3 attempts/hop, ≤3 redirects; job `IDEMPOTENT_RETRY` 5; **no total deadline** | bytes-first allowlist (`media_types.resolve`); no local decode | transcript → agent reply | row kept; object purged at `MEDIA_RETENTION_DAYS` (default 0 = never) and at workspace purge |
| 2 | Inbound voice/audio | inbound customer | Meta CDN → store → OpenAI transcription (multipart) | same → `TranscriptionClient` | same | same; no duration bound | transcription client 3 attempts | allowlist; no local codec | transcript | same |
| 3 | Inbound PDF / text document | inbound customer | Meta CDN → store → **in-process `pypdf`** | same → `extraction.extract_pdf` / `extract_text` | same | 25 MB; **no parse bound** (MEDIA-02) | none on parse | allowlist; `pypdf` 6.19 | extracted text (≤8,000 chars kept) | same |
| 4 | Inbound video, OOXML/OLE2 docs | inbound customer | Meta CDN → store only | same | same | 25 MB | same | allowlist | `SKIPPED` (unreadable type); stored? no: skipped before download (not in `READABLE_TYPES`) | n/a |
| 5 | Outbound attachment | outbound user media | browser multipart → Meta upload → send; copy to store | `POST /conversations/{id}/messages/media` → `MessagingService.send_media` | `message_media` (READY) + object | 16 MB (`MAX_UPLOAD_BYTES`, chunked) | send protocol (ADR-093) | bytes-first allowlist; filename sanitised for Meta, **stored raw** | message to customer | same |
| 6 | Staff download | retrieval | store → authenticated colleague | `GET /conversations/{id}/media/{media_id}` | read only | whole object into memory | S3 30 s | canonical type only, `attachment`, `nosniff`, `no-store` | — | 404 with reason when purged |
| 7 | Upload reconciliation | temporary-state repair | store HEAD/GET → row | `uploads` worker → `MediaUploadReconciler` | `storage_state` | GET ≤ object | per pass | SHA-256 re-hash | — | — |
| 8 | Retention sweep | deletion | row → store DELETE | `retention` worker → `MediaRetentionService` | two-phase claim | — | per pass | — | — | the policy |
| 9 | Workspace purge | deletion | rows → store DELETE | `purge` worker → `WorkspacePurgeService` + `purge_media_objects` | rows deleted, then objects | — | per pass | — | — | the policy |
| 10 | Knowledge PDFs | knowledge-document | API base64 → bounded child | `KnowledgeService` → `extract_pdf_bounded` | knowledge tables (RAG, closed) | 300 KB | 20 s kill | child process | — | RAG |

**Not present, with evidence:** no tempfiles (`tempfile`, `mkstemp` and `NamedTemporaryFile` have no hits in `app/`); no Pillow/ffmpeg/ffprobe/ImageMagick; no XML/ZIP/tar parser; no presigned URL; no avatar/logo/static media; no customer-supplied URL is fetched server-side; no path promotes a customer attachment into the knowledge base; no AI-sent media (outbound media is `MessageOrigin.HUMAN` only). The only subprocess is the knowledge PDF child (`create_subprocess_exec`, stdin pipe, no shell, numeric argv).

---

## 6. Architecture

Inbound, as implemented (ADR-087/092):

```text
Meta webhook (HMAC) ─ route tx ────────────────────────────────────────────────┐
  parse → resolve tenant by phone_number_id → store event → project message     │ ONE tx per delivery
  → message_media row PENDING (filename, mime, wa_media_id from payload)         │ (no per-message savepoint)
  → MediaQueue.enqueue (inside tx, failure swallowed → event left RECEIVED)      │
  → COMMIT (CommittingRoute)  ─────────────────────────────────────────────────┘
MediaWorker (1 per process, sequential, shares the event loop with every worker)
  session opened ─────────────────────────────────────────── tx A begins
    require_by_id (tenant-scoped)
    probe_media  ── HTTP graph.facebook.com (bearer)   ← NOT inside try (MEDIA-04)
    status=DOWNLOADING, attempts+=1, FLUSH  ← row lock held from here (MEDIA-11)
    fetch_media ── HTTP graph + CDN hops (bearer on every hop, MEDIA-01), streamed cap
    resolve type from bytes
    intend: SELECT … FOR UPDATE, capacity reserve (advisory lock), key, PENDING
  released(): COMMIT tx A ──────────────────────────────────── (TX1)
    put_at ─ object store PUT (no tx, no connection)
  tx B: finalize (FOR UPDATE, PENDING→STORED, STORAGE_USED meter)
    understand: store GET → reader (vision HTTP | transcription HTTP | in-process pypdf, MEDIA-02)
    READY | SKIPPED | FAILED
    ConversationMediaGate lock → count_unresolved → maybe AgentJob
  COMMIT tx B ──────────────────────────────────────────────── (TX2)
  queue.release(raw) → AgentQueue.enqueue (after commit)
Any exception from _handle → rollback tx B → handle_failure(IDEMPOTENT_RETRY) → retry or dead-letter
```

Outbound: request tx → idempotency replay check → bytes-first type → storage `require` → `_dispatch` (send intent CLAIMED **committed**, Meta upload, Meta send, SENT) → `_record_attachment` (media row, capacity reserve, key, PENDING) → `released` → PUT → STORED → request commit. The media row's first flush happens **after** Meta has delivered (MEDIA-06).

---

## 7. Tenant Boundary

`MediaRepository` is a `TenantScopedRepository`, so its predicate lives in one place, and a subclass without it cannot be constructed. `ConversationMediaGate` is scoped the same way. `PlatformMediaRepository` (unscoped) is reachable only from the retention, reconciliation and purge sweeps. The API route loads media through the tenant-scoped service and additionally requires `media.conversation_id == path conversation_id`.

P3-01 ran against real MinIO objects, with presence proven first (`objects_present=(True, True)`, keys prefixed by their owners). From tenant A: `get(B media)` gave `not_found`; `lock_for_upload(B media)` gave `not_found`; `count_unresolved(B conversation)` gave `0`; and `ConversationMediaGate.lock(B conversation)` took **0** tuple locks. No presigned URL exists to request (§25). The storage layer itself is deliberately *not* an authorization boundary (`S3MediaStorage.get` will read any well-formed key), and nothing outside the scoped repository hands it a key.

The invariant sweep over 31 rows found 0 rows whose tenant differs from their message's or conversation's, and 0 whose conversation differs from their message's (§31). Mutation M01 (tenant predicate removed) was killed by `test_media_isolation.py` (§32).

**Verdict: holds.** No cross-tenant read, write, lock, delete or presign path was found.

---

## 8. Object Key / Filename Safety

`build_key(tenant_id, mime_type)` takes no filename (P3-02b: `build_key_accepts_filename=False`). Keys are distinct UUIDs and every one matches `SAFE_KEY`. Thirteen hostile keys were refused by **both** backends (P3-02): `../`, `..\`, absolute, `%2e%2e%2f`, NUL, U+2215, `?versionId=`, `CON`, `.htaccess`, 600 chars, a leading slash, and an uppercase extension. `UNIQUE(storage_key)` gives one owner per object.

The filename is stored for display, never used in a path or header, and never passed to a subprocess. Outbound, `_safe_filename` replaces anything not matching `SAFE_FILENAME` before it goes to Meta. The download response sends a bare `Content-Disposition: attachment` with no filename, so CR/LF and RTL-override names have no header to inject into.

**Filename length is the one unsafe property, and it is not about paths.** `message_media.filename` is `String(300)`, and neither the inbound payload parser (`payload._text`) nor the outbound route bounds it (MEDIA-05, MEDIA-06). NUL is stripped inbound by the MSG-03 decoder, but not outbound.

---

## 9. MIME / Type Validation

The authoritative rule is `app/core/media_types.resolve`: **an allowlist decided by magic bytes**, with the claim used only to choose within a genuinely ambiguous pair (Matroska, OLE2). An undeclared or generic type falls back to the bytes. A contradiction is refused (`SKIPPED` inbound, 400/422 outbound). The canonical type is what is stored, sent to Meta and served. SVG and HTML cannot be admitted as images: HTML bytes can only become `text/plain`, and are served as an attachment.

The existing `tests/unit/test_media_types.py` and `tests/integration/test_media_type_enforcement.py` cover the brief's matrix: `image/jpeg`+PDF, `application/pdf`+ZIP, `audio/ogg`+random, `.jpg`+executable, missing, octet-stream, mixed case, parameters. Mutation M05 (mismatch refusal removed) was killed (§32).

One asymmetry is recorded rather than raised as a finding. The pre-download gate `_is_readable` routes on the **provider's** claimed type, before any bytes exist. A file Meta labels `video/mp4` is skipped without download even if it is really a JPEG. That only ever refuses; it cannot admit.

---

## 10. Size / Streaming Bounds

| Stage | Bound | Evidence |
|---|---|---|
| Provider-declared size | pre-check against `MEDIA_MAX_BYTES` | `media_service.py:175` |
| Streamed success body | **actual bytes**, abandoned within one chunk of the cap | P1-06: no Content-Length, 3 MB → `too_large` after 1,052,672 B; false `Content-Length: 10` → `too_large`; exactly 1 MiB → accepted; 1 MiB + 1 → `too_large` after 1,048,577 B; zero bytes → accepted, then refused by type detection (`detect(b"")` is `None`) |
| **4xx error body** | **none**: `await response.aread()` | P1-05: **209,715,200 B consumed against a 1,048,576 B cap** for 404/403/400/410 (MEDIA-09) |
| Graph descriptor JSON | none: buffered `_get` | P1-09: 64 MB descriptor buffered. Host is the hardcoded `graph.facebook.com` (info) |
| Upload route | 16 MB, 64 KB chunks | `conversations.py:86` |
| Decoded/processed output | transcript truncated to 8,000 chars; **no bound on parse work** | MEDIA-02 |

Resident amplification per job: bytes, plus a `b"".join` copy (transient), plus base64 (×1.33) and the JSON request for vision. That is roughly 100 MB for a 25 MB image, sequential per process: bounded, and acceptable. The PDF path is the unbounded one (§11). `MEDIA.md`'s "Nothing streams" known gap is accurate for storage and reads.

---

## 11. Parser / Decoder Safety

There is no image, audio or video decoder in-process: images and audio go to OpenAI as bytes. **The only in-process parser is `pypdf` on the message path**, and it is the most important finding area of this audit.

* **Amplification (MEDIA-02).** `extract_pdf` runs synchronously on the worker's event loop. `MAX_PAGES=40` caps the page count, but not per-page work, and not the 75 MB-per-stream inflation pypdf itself permits. P5 used a single-page PDF whose FlateDecode content stream inflates to text operators:

  | inflated | file | message path | knowledge path |
  |---|---|---|---|
  | 1 MB | 2.1 KB | 2.8 s loop stall, 110 MB RSS | ok, 3.2 s, 61 MB |
  | 4 MB | 6.7 KB | 17.6 s stall, 238 MB | refused (text limit), 61 MB |
  | 16 MB | 25 KB | **170.3 s stall, 763 MB** | refused at **20.05 s**, 68 MB |

  Growth is super-linear. 40 pages of 75 MB streams fit well inside 25 MB of file; the 64 MB and multi-page cases did not finish inside the harness's 600 s window.
* **Containment (MEDIA-03).** `extract_pdf` catches `PyPdfError, ValueError, OSError, RecursionError`. Across 6,000 deterministic mutations of a valid 600-byte PDF, **1,457 escaped**: `AttributeError` 904, `AssertionError` 378, `KeyError` 98, `TypeError` 39, `IndexError` 38. `understand()` catches only named domain errors, so these propagate out of the job (§27).

---

## 12. SSRF / HTTP Download Boundary

URL sources: Graph URLs are built from the hardcoded `GRAPH_BASE_URL` plus `media_id` from a signed webhook. The CDN URL is **provider-returned**. Redirect targets are provider-returned. No customer-controlled or database-stored URL is ever fetched.

Controls: https-only; every hop resolved once, and every resolved address must be `is_global` (IPv4-mapped IPv6 unwrapped); `GuardedTransport` pins the connection to the judged address while keeping SNI and Host; at most 3 redirects, followed by hand.

P1-03 tried 22 targets × {first hop, redirect} = **44/44 refused, with no request reaching a non-Meta host**: `127.0.0.1`, `localhost`, `[::1]`, `10.0.0.5`, `192.168.1.10`, `172.17.0.1`, `169.254.169.254`, `redis:6379`, `postgres:5432`, `minio:9000` (real container DNS), `file://`, `gopher://`, `ftp://`, `http://`, `graph.facebook.com@127.0.0.1`, `HTTPS://127.0.0.1`, `2130706433`, `0x7f000001`, `0177.0.0.1`, `[::ffff:127.0.0.1]`, `[::ffff:169.254.169.254]`, `100.64.0.1`. P1-04: a redirect loop stops after 4 CDN requests. Connect-time DNS-rebinding pinning is covered by the existing `tests/unit/test_outbound_pinning.py`, and mutations M07–M09 exercise it (§32).

**What is missing is a host allowlist.** Any *public* host is fetchable, and that is what turns MEDIA-01 from theoretical into a direct credential disclosure.

HTTP client safety: connect/read/write/pool are 10 s each **per operation** (P1-08: a 1 s read-timeout client took 4.84 s on a server dripping a byte every 0.4 s, without timing out). There is no total deadline (MEDIA-10). The retry policy is 3 attempts on transport error, 5xx and 429 (Retry-After is honoured on sends, not on media reads). Streamed responses are closed by `async with`.

---

## 13. Meta Credential Handling

The token is attached in `_get_once`, `_stream_hop` and `upload_media`, each with an explicit `headers={"Authorization": …}` and `follow_redirects=False`. P1-01 used a sentinel token with Graph → `lookaside.fbsbx.com` → 302 → `cdn.attacker-controlled.example`: `hosts_receiving_bearer = [graph.facebook.com, lookaside.fbsbx.com, cdn.attacker-controlled.example]`. P1-02, with the descriptor URL itself naming the foreign host, gave the same result.

The token never appears in logs, exceptions or error messages (P7-02: 19 records, `meta_token_in_logs=False`). `ProviderAuthError` carries no credential material.

**Which token.** Media downloads use `settings.meta_access_token` unconditionally (`media_worker.py:308-315`), while sends resolve the workspace's own encrypted credential when one exists (`CredentialService.resolve`, ADR-034). See MEDIA-13.

---

## 14. Object Storage

The S3 client is a hand-written SigV4 client over httpx: single PUT, GET, HEAD, DELETE; no multipart, so there are no abandoned parts to clean up; payload hash signed; `x-amz-server-side-encryption` optional; 30 s timeout; key re-validated before every request; refusals logged by status and operation only. The deployment guard refuses `s3` with incomplete configuration, and media credentials are separate from backup credentials (ADR-075). `LocalMediaStorage` writes `.name.partial` and then does an atomic rename.

* P6 (a real 2 MB tmpfs): two 900 KB writes stored; the third raised `StorageError` (bounded, observable), and **the 248 KB `.partial` file was left behind with 0 KB free** (MEDIA-14).
* P3-06: an object replaced out-of-band is served as the row's type without re-verification (`replaced_served_without_verification=True`); a deleted object gives `StorageError` 500. That is the trust boundary of anyone with bucket-write access. See §24 (INFO).
* Local-disk operations are synchronous inside `async def` (≤25 MB `write_bytes`/`read_bytes` on the event loop). This is a performance note, not a finding.

---

## 15. DB / Object Atomicity

Write order: TX1 intent (key, size, SHA-256, `pending`) **commit**, then PUT with no transaction, then TX2 `stored`. Interrupted writes are settled by `MediaUploadReconciler` (`FOR UPDATE SKIP LOCKED`, grace 900 s, HEAD then GET and re-hash, four verdicts; unreachable decides nothing, and 403 is not read as missing). There is no bucket listing, by construction (`test_no_bucket_listing.py`). The existing `test_media_write_atomicity.py`, `test_media_crash_recovery.py` (with a real child process killed mid-upload) and `test_upload_recovery_worker.py` cover the ordinary windows, and mutations M11–M13, M16 and M17 hold them (§32).

The windows this audit found open are elsewhere:

| Window | Result | Finding |
|---|---|---|
| Row deleted by workspace purge, object delete fails | purged workspace, **object kept, no row names it** (P3-04) | MEDIA-07 |
| Purge commits while a write is between TX1 and PUT | PUT lands after the purge; **orphan** (P3-05) | MEDIA-07 |
| Media job dead-letters after TX1 | reconciler later sets `storage_state=stored`, `status` stays `downloading` (P2-01) | MEDIA-03 |
| Download beyond 900 s grace (possible under MEDIA-10) | reconciler can judge the object `missing` and clear the key before the PUT lands; the later object is then unowned | MEDIA-10 (code analysis) |

---

## 16. Duplicate / Replay Semantics

Identity: `UNIQUE(message_id)` on `message_media` (one attachment per message), the WhatsApp event id for webhook dedupe, and `UNIQUE(storage_key)`. Replays stop at the event insert (ADR-102). A redelivered job reuses the committed key under `lock_for_upload`.

In P7-01 two real workers handled one job concurrently. Both fetched from Meta (2 downloads); the second blocked on the row lock for the whole first download (6.43 s for two 3 s fetches). The result was **one** object, **one** `STORAGE_USED` meter, **one** paid read, **one** `MEDIA_PROCESSING` meter, and status `ready`. Both `_handle` calls returned an agent job. In the real loop only the attempt whose `release()` succeeds enqueues, and the AI worker dedupes on `trigger_message_id` (WQ-01), so this is not a double reply. The duplicate Meta download is the only waste.

---

## 17. Temporary Files

**Not applicable.** No media path creates a temporary file: bytes stay in memory, the local store's staging file is covered in §14, and the PDF child uses pipes.

---

## 18. Audio / Transcription

Transcription is remote (OpenAI; the model is configurable) and receives the whole file as multipart with `audio.{ext}` as the filename (never the customer's). There is no duration bound. The size bound is `MEDIA_MAX_BYTES` (25 MiB), which should be compared with OpenAI's own limit in deployment verification. The client retries 3 times on transport error, 5xx and 429; a 4xx gives `ExternalServiceError("…rejected this recording.")`.

Outcomes: silence → `SKIPPED`; provider failure → `FAILED`, never retried (MEDIA-12). Either way the conversation is released with an "unreadable" line, so a transcription outage degrades gracefully rather than stranding. **Transcription calls are not counted by `ProviderCall` and nothing alerts on them** (MEDIA-15). Usage is metered as a count, not seconds, deliberately.

---

## 19. Image Processing

No local decode, resize or transcode, and no EXIF handling. Images are stored byte-for-byte, so **EXIF/GPS is preserved**. They are served only to authenticated colleagues of the owning workspace and sent to OpenAI vision as a data URL, and they are never re-published to other customers by Wasla. Stripping is a product decision (PD-3), not a defect. Decompression bombs are the provider's problem; locally, only the byte cap applies.

---

## 20. Video Processing

Downloaded? No. `video/*` is not in `READABLE_TYPES`, so inbound video is `SKIPPED` at the provider-declared type, *before download*. `MEDIA.md` says video is "downloaded and stored but not understood", which is **inaccurate**: it is not downloaded (documentation drift, LOW, folded into MEDIA-12's doc fixes). Outbound video is uploaded to Meta and stored. There is no ffmpeg or ffprobe, so §15/§52 of the brief are not applicable.

---

## 21. Documents / RAG Boundary

Customer documents never enter the knowledge base; there is no promotion path. The only boundary finding is that the message path **does not reuse** the bounded extractor RAG-02 built for knowledge documents (MEDIA-02/MEDIA-03). Deletion and searchability questions from §38 of the brief are therefore not applicable.

---

## 22. Messaging Boundary

Outbound attachments reuse the ADR-093 two-phase send. Upload happens while the intent is `CLAIMED`, so an upload failure is an ordinary undelivered send. The idempotency-key replay short-circuits the whole method (MSG-15). Recipient binding is the conversation's contact, and the upload's `phone_number_id` is the conversation's account. Retrying without an idempotency key is a second send by design, as for text.

Two boundary defects:

* **MEDIA-06:** attachment recording fails after delivery.
* **MEDIA-05:** the inbound DataError net is dead.

Outbound media idempotency (§40 of the brief): a Meta upload id is single-use and is not reused across attempts. An ambiguous timeout on the *send* follows the text-message rules (terminal, uncertain), and an ambiguous *upload* timeout yields an undelivered send and at most an orphaned Meta-side media object, which Meta expires. No duplicate *customer-visible* send arises from the media layer itself.

---

## 23. Lifecycle / Suspension

| Transition during / before processing | Observed | Expected (per `AI_AGENTS.md:89`) |
|---|---|---|
| Workspace suspended (committed before the job) | Meta download ✔, **paid read ✔**, object stored, `ready`, agent job enqueued (P2-04) | no inference |
| Workspace soft-deleted | same (P2-04) | no inference, and ideally no new objects |
| Number released (historical message) | text: owes nothing. **Media: queued for download and read** (P2-05) | recorded, not answered (MSG-01) |
| Workspace purged mid-write | orphan object (P3-05) | nothing left behind |
| Conversation closed / agent disabled | not checked by the media worker; the AI worker refuses the turn (AI-06) | acceptable: the file is still the customer's message |

The AI worker's own lifecycle check (AI-06) stops the *reply*, so no customer-visible message leaves a suspended workspace. What leaks is provider spend, storage growth for a workspace awaiting erasure, and a contradiction of the written guarantee (MEDIA-08). Whether a suspended workspace's inbound files should still be *stored* is a product decision (PD-5). Reading them with a paid model is not.

---

## 24. Deletion / Retention / Purge

* **Retention** (ADR-078): claim `purging` and commit, delete the object, clear the key → `purged`. Only fully `stored` rows qualify; reconciliation finishes stranded claims; a download of a purged row returns 404 with its reason. Repeated deletes are idempotent (204/404 treated as gone). Covered by `test_media_retention.py`; M15 was killed.
* **Workspace purge**, happy path (P3-03, real MinIO, presence first): victim `present_before=[True, True]` → rows 0 and objects `[False, False]`; control workspace objects `[True, True]` untouched. **0 cross-tenant deletions.**
* **Workspace purge, failure path (P3-04):** with deletes refused, `objects_failed=2`, `purged_at` is set, the next pass purges 0, `objects_still_in_bucket=[True, True]`, `db_rows_naming_them=0`. The only trace is a warning log line without the key, plus a count in a log summary. `WorkspacePurgeFailing` does not fire, because the lifecycle outcome is `success`. The purge worker's docstring says this failure is covered "which the media retention sweep's own reconciliation is the model for", but no reconciliation covers keys whose rows are gone (MEDIA-07).
* Message, conversation and customer deletion: **not applicable**. No API or service deletes a conversation, message or contact (the only `@router.delete` routes are agents, sessions, billing, contact opt-out, Google identity, invitations, knowledge, members, platform users and workspaces). `message_media` cascades on `messages`/`conversations` deletion at the schema level, so if such a route is ever added it will orphan objects exactly as MEDIA-07 does (future requirement FR-4). The brief's `DELETE /api/v1/conversations/{id}` does not exist at this HEAD.

---

## 25. Signed URLs / Object ACL

Not applicable for presigning: none is ever issued. There is no ACL header anywhere. The bucket is `private` (`mc anonymous get`); an anonymous GET of a real object returned **403**, and an anonymous LIST returned **403**. The production bucket policy is deployment verification.

---

## 26. Logging / Privacy

P7-02 drove a failed fetch through the worker's `logger.exception` path and an S3 refusal with a wrong secret, then checked 19 formatted records: Meta token 0, S3 secret 0, CDN URL signature 0. Media log lines carry `tenant_id`, `media_id`, `byte_size`, `attempts`, `event`, and never a key, URL, filename, caption or transcript. `net.unsafe_url_refused` logs the host but not the address. SQL parameters are hidden (TOOL-10's `hide_parameters=True`; confirmed in P4's DB errors, `[SQL parameters hidden …]`).

`last_error` (≤500 chars) reaches the agent's context as `[image, unreadable: …]`. It is Wasla-authored text, except `Files of type {mime_type} cannot be read.`, which embeds a provider-supplied string and is **not truncated** in `_skip`. A Meta descriptor `mime_type` of about 470 characters or more would overflow `String(500)` and trigger MEDIA-04's stranding path (LOW, noted under MEDIA-04).

---

## 27. Customer-Turn Failure Containment

"Resolved" = `SKIPPED | FAILED | READY`. A conversation's agent turn is released only when **no** attachment on it is unresolved.

| Input | Row outcome | Agent turn | Evidence |
|---|---|---|---|
| Readable text doc (control) | `ready` | released (1) | P2-01b |
| Oversize (declared or streamed) | `skipped` | released | P1-06 + existing tests |
| Type mismatch / unsupported | `skipped` | released | existing tests |
| CDN 5xx | `failed` (attempts 1, no retry) | released, "unreadable: WhatsApp could not return this file." | P2-03 |
| Transcription/vision outage | `failed` | released | code + existing tests |
| Storage refuses write | `failed`, intent `pending` → reconciler | released | existing tests |
| **Poison PDF (5 exception classes)** | **`downloading` forever** (dead-lettered round 1) | **never**; later attachments in the conversation also never | P2-01 |
| **Descriptor 404/400/403/5xx/429** | **`pending` forever** (dead-lettered after 5 rounds) | **never**; conversation blocked | P2-02 |
| **No platform token** | `pending` forever | never | P2-06 |
| **Filename > 300 chars (inbound)** | **no row at all; delivery 500s on every retry** | never | P4-01 |
| Suspended/deleted workspace | `ready` | enqueued (AI worker refuses) | P2-04 |

Text messages on a blocked conversation are still answered: the text path enqueues directly. The agent's memory then shows the stuck file as `[document, not yet read]` indefinitely.

---

## 28. Observability

Present: `wasla_jobs_total{queue="media"}`, dead-letter counters (`DeadLetterGrowth` alert), provider calls for `fetch_media` and vision, `wasla_media_retention_total{outcome}`, `wasla_media_upload_reconciliation_total{outcome}` including `quarantined`/`pending`, and `UnprocessedInboundBacklog` for handoffs that never reached a queue. Labels are closed sets: no tenant, media id, key or filename.

Absent (MEDIA-15):

* **Alerts:** `deploy/monitoring/alerts.yml` references **no** media metric. `MEDIA.md` says "`…{outcome="quarantined"}` above zero is the alert", but no such rule exists.
* **Unresolved media:** no counter or gauge by outcome (skipped/failed/oversize/type-mismatch) and no stranded-media gauge. MEDIA-03/04 are visible only as generic `DeadLetterGrowth`.
* **Transcription:** not instrumented at all.
* **Purge object-delete failures:** not counted in metrics.

---

## 29. Fault-Injection Matrix

Representative flow: inbound image/document through the real `MediaWorker`, real Redis queue, real PostgreSQL and real MinIO.

| Injection point | DB row after | Object | Temp file | Turn | Retry | Observable |
|---|---|---|---|---|---|---|
| Before URL lookup (descriptor 404) | `pending` | none | n/a | **stranded** | 5 × 1 HTTP | dead letter only |
| Descriptor 5xx/429 | `pending` | none | n/a | **stranded** | 5 × 3 = **15 HTTP** | dead letter only |
| After lookup, CDN 503 | `failed`, attempts 1 | none | n/a | released | none (3 HTTP inside client) | `last_error`, provider metric |
| During body (oversize stream) | `skipped` | none | n/a | released | none | `last_error` |
| After download, type mismatch | `skipped` | none | n/a | released | none | log `media.type_mismatch` |
| During object upload (store refuses) | `failed`, `pending` intent | maybe | n/a | released | reconciler | reconciliation metric |
| After upload, before TX2 (job raises) | rolls back to `downloading`/`pending` | present | n/a | healed on retry (same key, same hash) | yes | — |
| During parser (poison PDF) | **`downloading`** → reconciler `stored` | present | n/a | **stranded + conversation blocked** | **0 retries** (dead-lettered round 1) | dead letter only |
| During transcription (outage) | `failed` | present | n/a | released | none | provider log; **no metric** |
| During deletion (purge delete fails) | **rows gone** | **present forever** | n/a | n/a | **none** | warning log only |
| Purge during in-flight write | rows gone | **orphan** | n/a | n/a | job raises, dead-letters | none |
| After DB commit, before queue ack | committed | present | n/a | one turn (release gate + WQ-01) | lease reaper | closed by the Workers audit |

---

## 30. Concurrency Matrix

| Race | Proven to occur | Result |
|---|---|---|
| Two workers, same media (P7-01) | both inside `fetch_media`; second blocked 6.43 s on the row lock | one object, one meter, one read; converges |
| Download + purge (P3-05) | intent `pending` committed before the purge; purge committed first | **orphan object** (MEDIA-07) |
| Two attachments, one conversation | existing `test_media_worker.py` gate tests; M21/M22 (§32) | exactly one agent job |
| Retention + reconcile | existing `test_media_retention.py` | idempotent |
| Upload vs reconciler | existing `test_upload_recovery_worker.py`, grace-based | finalises once |
| Row lock across network (P7-01a) | `idle in transaction`=1, `RowExclusiveLock` granted on `message_media` during the Meta fetch | MEDIA-11 |

No deadlocks were observed in product code. (The first P7 attempt deadlocked a *test* barrier against the product's row lock, which is how MEDIA-11 was found.)

---

## 31. Database / Storage Invariant Sweep

Over the rows and objects the probes committed in `wasla_invariants` and MinIO: **presence** 32 tenants (3 purged), 31 `message_media` rows (21 `stored`), 174 bucket objects (23 under probe tenants). Anonymous access was checked separately (403).

| Invariant | Population | Violations | Attribution |
|---|---|---|---|
| media row without tenant | 31 | 0 | holds |
| media tenant ≠ message tenant | 31 | 0 | holds |
| media tenant ≠ conversation tenant | 31 | 0 | holds |
| media conversation ≠ message conversation | 31 | 0 | holds |
| unsafe object key | 21 | 0 | holds |
| key prefix ≠ owning tenant | 21 | 0 | holds |
| duplicate storage key | 21 | 0 | holds |
| oversized stored | 21 | 0 | holds |
| failed/skipped marked ready | 31 | 0 | holds |
| public object | 23 | 0 | holds (bucket private, anonymous 403) |
| stored row with missing object | 21 | 1 | P3-06 deleted it deliberately to test the 500 path |
| **object without owning row** | 23 | **3** | P3-04 (2) and P3-05 (1): **MEDIA-07** |
| **object retained for purged workspace** | 23 | **3** | same: **MEDIA-07** |
| **unresolved media > 2 min** | 31 | 20 | **11 product** (5 poison `downloading`, 5 descriptor `pending`, 1 no-token `pending`: MEDIA-03/04/13); 9 harness artefacts (7 rows that P3 downloaded without a reader, 1 P4 control never given a worker, 1 row of the aborted first P7 run) |
| **conversation with ready media blocked by older unresolved** | 31 | **5** | all 5 poison conversations: **MEDIA-03** |
| **media read for suspended/deleted workspace** | 31 | **2** | P2-04: **MEDIA-08** |

---

## 32. Mutation Matrix

Runner: `mutations/run_mutations.py`. One mutation at a time in the audit worktree; the 28-file media-targeted suite runs model-built with `-x`, the file is restored, and its SHA-256 is re-verified. Every mutation applied on an exact single-match anchor; none produced an unrelated failure counted as a kill. The worktree was clean (`git status --short` empty) at the frozen HEAD afterwards.

**18 killed / 12 survived of 30 applied.**

| ID | Property removed | Applied | Result | Killer | Restored |
|---|---|---|---|---|---|
| M01 | tenant predicate on media lookup | yes | **killed** | `test_media_isolation.py::test_another_workspace_cannot_read_the_attachment[local]` | ✔ |
| M02 | key pattern check on local read/write | yes | **killed** | `test_object_store.py::test_a_key_that_is_not_one_we_produced_is_refused[local-not a key at a` | ✔ |
| M03 | actual-byte cap while streaming | yes | **killed** | `test_whatsapp_client.py::test_a_body_that_passes_the_cap_is_abandoned_mid_read` | ✔ |
| M04 | declared-size pre-check | yes | **killed** | `test_media_worker.py::test_an_oversized_file_is_skipped_and_still_releases_the_reply` | ✔ |
| M05 | claim/bytes mismatch refusal | yes | **killed** | `test_media_type_enforcement.py::test_a_spoofed_upload_is_refused_and_nothing_is_sent[image/jpeg-%PDF-1` | ✔ |
| M06 | redirect hop validation | yes | **SURVIVED** | — | ✔ |
| M07 | first-hop media URL validation | yes | **SURVIVED** | — | ✔ |
| M08 | non-public address judgement | yes | **killed** | `test_outbound_pinning.py::test_a_name_that_turns_private_after_validation_is_refused` | ✔ |
| M09 | https-only scheme | yes | **killed** | `test_outbound_pinning.py::test_the_transport_refuses_a_non_https_scheme[http]` | ✔ |
| M10 | HTTP timeout on media client | yes | **SURVIVED** | — | ✔ |
| M11 | failed object write not marked stored | yes | **killed** | `test_media_write_atomicity.py::test_a_retry_after_a_failed_write_reuses_the_committed_key` | ✔ |
| M12 | intent hash-conflict refusal | yes | **SURVIVED** | — | ✔ |
| M13 | finalize only a still-pending intent | yes | **SURVIVED** | — | ✔ |
| M14 | workspace purge deletes objects | yes | **SURVIVED** | — | ✔ |
| M15 | retention excludes in-flight uploads | yes | **killed** | `test_media_write_atomicity.py::test_retention_will_not_claim_an_upload_that_never_finished` | ✔ |
| M16 | reconciler: unreachable is not missing | yes | **killed** | `test_media_write_atomicity.py::test_a_store_that_cannot_be_reached_settles_nothing` | ✔ |
| M17 | reconciler hash verification | yes | **killed** | `test_media_write_atomicity.py::test_an_object_that_is_not_what_the_row_describes_is_never_adopted` | ✔ |
| M18 | download served as attachment | yes | **killed** | `test_media_endpoints.py::test_an_attachment_is_never_served_inline` | ✔ |
| M19 | download route conversation binding | yes | **killed** | `test_media_endpoints.py::test_media_from_another_conversation_is_not_found` | ✔ |
| M20 | serve only canonical types | yes | **killed** | `test_media_endpoints.py::test_a_stored_type_outside_the_supported_set_is_not_served_back` | ✔ |
| M21 | conversation gate lock | yes | **SURVIVED** | — | ✔ |
| M22 | unresolved-sibling count before release | yes | **killed** | `test_media_release_ordering.py::test_two_attachments_queue_one_agent_turn` | ✔ |
| M23 | purged row never re-downloaded | yes | **killed** | `test_media_retention.py::test_a_replayed_media_job_does_not_undo_a_purge[local]` | ✔ |
| M24 | oversize mid-stream is skipped, not retried | yes | **killed** | `test_media_type_enforcement.py::test_an_oversized_download_is_abandoned_rather_than_held` | ✔ |
| M25 | pdf parser failure containment | yes | **SURVIVED** | — | ✔ |
| M26 | inbound storage capacity check | yes | **SURVIVED** | — | ✔ |
| M27 | outbound filename sanitiser | yes | **killed** | `test_media_worker.py::test_a_hostile_filename_is_replaced_before_it_reaches_meta` | ✔ |
| M28 | webhook DataError containment | yes | **SURVIVED** | — | ✔ |
| M29 | S3 403 not read as missing | yes | **SURVIVED** | — | ✔ |
| M30 | media error text withheld from caller | yes | **SURVIVED** | — | ✔ |

Not applied, because the property does not exist to remove: "use original filename as object key" (`build_key` has no filename input, so M02 pattern-check removal is the nearest), "bearer only to Meta hosts" (MEDIA-01), "remove signed-URL authorization" (no signed URLs), "remove temp-file cleanup" (no temp files), and "remove outbound media idempotency key" (message-level, covered by the Messaging audit).

## 33. Structural Test Gaps

The survivors, grouped by what they mean:

* **The streamed download path's SSRF checks are unpinned (M06, M07).** Removing redirect-hop *or* first-hop validation in `fetch_media` leaves 550 tests green. The connect-time `GuardedTransport` would still refuse a private address in production, which is why M08/M09 are killed, but the client-level check is defence in depth that no test holds. Together with MEDIA-01, nothing tests what a media hop is allowed to reach.
* **No timeout is pinned (M10)**, matching MEDIA-10.
* **The write protocol's two guards on a pending intent are unpinned (M12, M13).** A retry that reuses a key with different bytes, and a finalisation over a row something else settled, are both unasserted. The protocol is right today, but only its happy retries are tested.
* **Workspace purge's object deletion is unpinned (M14):** with `keys = []`, the suite still passes. `test_workspace_purge.py` asserts rows, not the bucket (MEDIA-07).
* **The one-reply gate's lock is unpinned (M21).** The release tests pass without `ConversationMediaGate.lock`, so they do not construct the concurrent race the lock exists for. M22 (the count) is killed.
* **Parser containment is unpinned (M25):** narrowing to `PyPdfError` survives, and the real gap is wider still (MEDIA-03).
* **Inbound capacity refusal is unpinned (M26)**; capacity tests exercise the outbound path.
* **The webhook DataError net is unpinned (M28)**, because it is unreachable (MEDIA-05).
* **S3 403-vs-missing is unpinned at the storage layer (M29).** Reading 403 as "absent" would let a rotated credential abandon every in-flight upload (ADR-087's stated risk), and only a reconciler-level test with a raising fake store exists.
* **No test would catch a credential logged on a successful fetch (M30).** P7-02 found no leak today, but no suite asserts it.

Plus the gaps behind MEDIA-02/03/04/05/06/08: no test drives a malformed PDF, a failing descriptor, a dead-lettered media job, a long filename or a suspended workspace through the real worker or route.

---

## 34. Findings Ledger

### MEDIA-01: The Meta bearer token is sent to any public host a media URL or redirect names

* **Severity:** HIGH · **Status:** Open · **Classification:** security defect, documentation mismatch
* **Component:** WhatsApp media download · **Files:** `app/integrations/whatsapp/client.py:531-561, 616-706, 734-807`; `app/core/net.py:9-12`
* **Evidence:** P1-01 (sentinel token): hosts receiving `Authorization: Bearer SENTINEL…` were `graph.facebook.com`, `lookaside.fbsbx.com`, **`cdn.attacker-controlled.example`** after one 302. P1-02: a descriptor URL naming the foreign host directly gives the same result. `net.py` claims "httpx strips `Authorization` when a redirect leaves the origin, so a redirect to an attacker host receives no credential". That holds only for httpx's own redirect handling; this client uses `follow_redirects=False` and re-attaches the header on every hop.
* **Reproduction:** `probes/test_p1_network.py::test_p1_01…`, `…p1_02…`.
* **Impact:** disclosure of the **platform** Meta token. Absent per-workspace credentials, that token sends and reads for every workspace on the deployment. Reachable if Meta's descriptor or CDN ever yields a non-Meta URL (an open redirect on a Meta CDN host, a compromised or misrouted response). The only control is "resolves to a public address", which every attacker host satisfies.
* **Current semantics:** bearer on every hop; any public https host allowed.
* **Required semantics:** the bearer is attached only to Meta-owned hosts (an explicit allowlist such as `graph.facebook.com`, `*.fbsbx.com`, `*.fbcdn.net`, confirmed in deployment verification), and is stripped (or the hop refused) on any cross-origin redirect. The SSRF address checks stay.
* **Why existing tests missed it:** `test_whatsapp_client.py` asserts that private redirects are refused, never *which headers a public redirect carries*. The docstring's claim made the property look already settled.
* **Required remediation:** host allowlist for media hops; header only on allowlisted origins; correct `net.py`.
* **Permanent regression test:** fake transport with a sentinel token; assert that the token never reaches a non-allowlisted host, for both first hop and redirect.
* **Deployment verification:** capture the real Meta media URL and redirect hosts (DV-1).

### MEDIA-02: Customer PDFs are parsed in-process on the shared event loop with no byte, time or memory bound

* **Severity:** HIGH · **Status:** Open · **Classification:** reliability/availability defect (bypasses RAG-02)
* **Component:** media reader · **Files:** `app/services/media_reader.py:190-194`; `app/services/extraction.py:77-96, 249-256`; `app/workers/runner.py` (one event loop for all workers)
* **Evidence:** P5. A 25 KB PDF gave a **170.3 s** event-loop stall and **763 MB** peak RSS on the message path; the knowledge path (`extract_pdf_bounded`) refused the same file at 20.05 s using 68 MB. 1 MB/4 MB inflations gave 2.8 s / 17.6 s stalls.
* **Reproduction:** `probes/p5_pdf_amplification.py`.
* **Impact:** anyone who can message a business number (unauthenticated) can freeze **every worker in the process**: agent replies for all tenants, follow-ups, campaigns, billing, other media. They can also push the process toward OOM, and repeat at will. With `MAX_PAGES=40` and pypdf's 75 MB-per-stream ceiling, a file well under 25 MB carries multi-minute to multi-hour work.
* **Current semantics:** `extract_pdf(content)` synchronous, in-process, unbounded except pages.
* **Required semantics:** message-path PDFs go through the same killable child, byte cap, timeout and memory limit as the knowledge path. A limit breach becomes `SKIPPED` with a reason; nothing parses on the event loop.
* **Why existing tests missed it:** `test_pdf_extraction_bounds.py` covers `extract_pdf_bounded` only; the message path's extractor has only functional tests.
* **Required remediation:** route `MediaReader._extract` through `extract_pdf_bounded` (with message-appropriate limits), mapping `DocumentTooLargeError` to a skip.
* **Permanent regression test:** P5's inflating PDF through `MediaReader` finishes within the child timeout, with a bounded heartbeat gap.
* **Deployment verification:** production container memory limits (DV-8).

### MEDIA-03: A media job that dead-letters leaves its row unresolved forever, and a poison PDF does exactly that, blocking every later attachment reply in the conversation

* **Severity:** HIGH · **Status:** Open · **Classification:** reliability defect
* **Component:** media worker / extraction · **Files:** `app/services/extraction.py:88-94`; `app/services/media_service.py:445-462`; `app/workers/media_worker.py:187-200, 389-399`; `app/workers/inbound_recovery.py:236-249`; `app/services/media_upload_service.py:267-288`
* **Evidence:** the fuzz found 1,457 of 6,000 <610-byte PDFs escaping `extract_pdf`. In P2-01, for each of 5 exception classes: dead-lettered on round 1; `status=downloading`; reconciler then `storage_state=stored` with `status` still `downloading`; a later readable document on the same conversation reached `ready`, and **0 agent turns were released**. The control conversation released 1. The invariant sweep found 5 of 5 poison conversations blocked.
* **Reproduction:** `probes/fuzz_pypdf.py`, then `probes/test_p2_turns.py::test_p2_01…`.
* **Impact:** the customer's attachment is never answered. Because release requires *no* unresolved sibling (`count_unresolved`), **no attachment in that conversation is ever answered again**. Text is still answered, while the agent is told the file is `[document, not yet read]`. There is no metric, no handoff, and nothing on the inbox explains it. Any customer can trigger it with a sub-kilobyte file.
* **Current semantics:** `understand()` contains named domain errors only. An unexpected exception rolls back to `downloading`. Nothing ever moves a dead-lettered media row to a terminal state; inbound recovery only rescues `PENDING` rows whose event was never enqueued, and the reconciler owns `storage_state`, not `status`.
* **Required semantics:** every parser failure is contained as `SKIPPED` ("could not be read"). Independently, a media row whose job is exhausted or dead-lettered becomes terminal (`FAILED`/`SKIPPED`), and the conversation is re-evaluated for release. An unresolved row older than a bound is swept.
* **Why existing tests missed it:** tests use well-formed or clearly-invalid PDFs (which raise `PdfReadError`), and no test drives a media job to dead-letter and then asks about the conversation.
* **Required remediation:** broaden containment at the reader boundary (or isolate in the child, per MEDIA-02); add a terminal transition on exhaustion; add a stranded-media sweep and gauge.
* **Permanent regression tests:** the five saved poison PDFs through the real worker gives `skipped` plus 1 agent job; a forced dead-letter gives a terminal row and a released conversation.
* **Deployment verification:** none (fully local).

### MEDIA-04: A failed Meta descriptor lookup escapes containment, permanent errors are retried, and the turn is stranded

* **Severity:** HIGH · **Status:** Open · **Classification:** reliability defect
* **Component:** `MediaService.download` · **Files:** `app/services/media_service.py:171` (`probe_media` outside any `try`); `app/services/media_service.py:506-516` (`_skip` does not truncate its reason, which can embed a provider mime string); `app/workers/retry.py:270`
* **Evidence:** P2-02. For Graph 404/400/403: 5 rounds, **5** HTTP calls, dead-lettered, `pending`, 0 turns. For 500/429: 5 rounds, **15** HTTP calls, same end state. Contrast P2-03: a CDN failure *after* the probe is contained as `failed` and releases the turn.
* **Reproduction:** `probes/test_p2_turns.py::test_p2_02…`.
* **Impact:** any media id Meta will not describe to this token (expired, deleted, wrong WABA; see MEDIA-13) strands the turn and blocks the conversation exactly as MEDIA-03 does. A permanent 4xx is also retried five times.
* **Required semantics:** a descriptor failure is classified like a fetch failure (`FAILED`, or `SKIPPED` for permanent 4xx), releasing the turn. Permanent 4xx is not retried by the queue. `_skip` truncates its reason.
* **Why existing tests missed it:** the worker tests' stub `probe_media` always succeeds.
* **Required remediation:** move `probe_media` inside the classified `try`; map permanent/transient; plus MEDIA-03's terminal-on-exhaustion.
* **Permanent regression test:** P2-02 matrix asserting `failed`/`skipped` plus 1 agent job, and ≤3 HTTP calls for a 404.
* **Deployment verification:** Meta's real status codes for expired media ids (DV-2).

### MEDIA-05: The webhook's DataError containment is dead under asyncpg, so an over-long attachment filename makes a delivery fail on every retry

* **Severity:** HIGH · **Status:** Open · **Classification:** reliability defect, test gap, **invalidates a closed Messaging guarantee (MSG-03) for this input class**
* **Component:** webhook route / inbound projection · **Files:** `app/api/v1/webhooks.py:186-214`; `app/integrations/whatsapp/payload.py:164`; `app/db/models/media.py:242`; `app/services/conversation_service.py:131-140`
* **Evidence:**
  * `probes/exc_class.py`: under asyncpg, value-too-long, NUL-in-text and int overflow all raise `sqlalchemy.exc.DBAPIError`, and `isinstance(err, DataError)` is **False** every time.
  * P4-01, through the real app, lifespan and signed webhook: a 300-char filename gives 200 `accepted` with 2 messages stored. A 301-char ASCII, 301-char Arabic or 2,000-char filename gives an **unhandled error → 500**, 0 messages stored (including the sibling text message), 0 media rows.
  * `grep` shows no test reaches `payload_rejected_by_database`.
* **Reproduction:** `probes/test_p4_names.py::test_p4_01…`.
* **Impact:** every Meta retry of that delivery fails identically for up to seven days. The customer's message and every other message in the same delivery are never stored or answered. The failure rate counts against the deployment's webhook subscription, which is precisely the MSG-03 outcome the handler was written to prevent. Any *other* over-width field in a delivery hits the same dead branch.
* **Current semantics:** `filename` unbounded into `String(300)`; `except DataError` unreachable.
* **Required semantics:** display metadata is truncated (or dropped) to fit, never able to fail a delivery; the route's permanent-content net catches what asyncpg actually raises (`DBAPIError` whose SQLSTATE is class 22); per-message containment so one message cannot lose its siblings.
* **Why existing tests missed it:** the webhook tests stub ingestion, and MSG-03's fix was proven by stripping NUL in the decoder, never through the `except` branch.
* **Required remediation:** truncate the inbound filename; fix the exception class; add a real-DB regression through the route.
* **Permanent regression test:** P4-01 through the real route gives 200, both messages stored, filename truncated.
* **Deployment verification:** whether Meta delivers document filenames longer than 300 characters (DV-3). The dead handler is a local fact regardless.

### MEDIA-06: An outbound attachment with an over-long or NUL filename is delivered, then fails to record

* **Severity:** MEDIUM · **Status:** Open · **Classification:** reliability defect
* **Files:** `app/api/v1/conversations.py:269`; `app/services/messaging_service.py:569-590`
* **Evidence:** P4-02. For 301 chars and for `contract\x00.pdf`, Meta received `media` then `messages` (**customer received the file**), then `DBAPIError`; the message row stayed **`pending`** and there were 0 media rows. The 300-char control was `sent` with 1 media row.
* **Impact:** a member (via the API) gets a 500 for a message the customer received. The send appears among unresolved outbound sends, the attachment record is lost, and a retry without an idempotency key sends it twice.
* **Required semantics:** validate or truncate the filename **before** `_dispatch`; nothing after delivery can fail on caller-supplied metadata.
* **Why missed:** outbound tests use short, clean filenames.
* **Regression test:** P4-02 asserting a refusal before any Meta call, or `sent` plus a stored row.
* **Deployment verification:** none.

### MEDIA-07: Workspace purge can leave a deleted workspace's customer files in the bucket permanently, with nothing recording them

* **Severity:** MEDIUM · **Status:** Open · **Classification:** privacy/retention defect, documentation mismatch
* **Files:** `app/workers/purge_worker.py:18-26, 142-168`; `app/services/workspace_purge_service.py:270-287`
* **Evidence:**
  * P3-04: object deletes refused, then `purged_at` set, the second pass purges 0, `objects_still_in_bucket=[True, True]`, and `db_rows_naming_them=0`.
  * P3-05: purge committed between intent and PUT, leaving an **orphan under the purged tenant's prefix**.
  * Invariant sweep: 3 objects retained for purged workspaces.
* **Impact:** "deleted" does not reliably mean "gone" for customer photographs, voice notes and documents. The failure is silent (`WorkspacePurgeFailing` stays green), and the only recovery is an operator deleting the `{tenant_id}/` prefix by hand, which nothing tells them to do. The docstring's reliance on "the media retention sweep's reconciliation" is unfounded: that reconciliation starts from rows, and the rows are gone.
* **Required semantics:** purge completion is recorded only after the objects are gone (or the pending keys are persisted in a table that survives the row delete and is retried). Because keys are tenant-prefixed, a purged tenant's prefix can be swept by prefix without the "absence of evidence" risk ADR-087 guards against. Failures are metered and alerted.
* **Why missed:** `test_workspace_purge.py` uses a store that always succeeds.
* **Regression test:** P3-04/P3-05 asserting zero objects under the purged prefix after retry.
* **Deployment verification:** production bucket versioning and lifecycle (deleted ≠ unrecoverable, DV-6).

### MEDIA-08: The media worker ignores workspace lifecycle and released numbers

* **Severity:** MEDIUM · **Status:** Open · **Classification:** implementation defect, documentation mismatch (cost/policy)
* **Files:** `app/workers/media_worker.py:236-276`; `app/services/whatsapp_service.py:335-337`
* **Evidence:** P2-04. Suspended and soft-deleted workspaces each got 1 Meta download, **1 paid read**, an object stored, status `ready`, and 1 agent job. P2-05: on a released number, text owes nothing, but media is still queued.
* **Impact:** provider spend (vision, transcription) and storage growth for workspaces the platform has stopped serving or is about to erase, contradicting `docs/AI_AGENTS.md:89` ("no inference") and MSG-01. There is no customer-visible reply (AI-06 refuses the turn).
* **Required semantics:** read `serving_state` before paid reads; decide explicitly whether to store (PD-5); treat released-number media like text.
* **Regression test:** P2-04/P2-05 asserting 0 paid reads.

### MEDIA-09: A 4xx media response body is read without the cap its comment promises

* **Severity:** MEDIUM · **Status:** Open · **Classification:** implementation defect (availability)
* **Files:** `app/integrations/whatsapp/client.py:685-694` (`await response.aread()` under the comment "Bounded by the same cap"); also `_get` buffers Graph responses unbounded (`:776`, INFO: hardcoded host).
* **Evidence:** P1-05. For 404/403/400/410, **200 MB consumed against a 1 MB cap**.
* **Impact:** memory exhaustion from any host the media fetch reaches, which under MEDIA-01's missing allowlist is any public host a Meta response or redirect names.
* **Required semantics:** error bodies are read through the capped reader (or a small fixed cap).
* **Regression test:** P1-05 asserting consumption ≤ cap.

### MEDIA-10: No total deadline on a media download

* **Severity:** MEDIUM · **Status:** Open · **Classification:** reliability defect
* **Files:** `app/integrations/whatsapp/client.py:284-299, 649-706`; `app/workers/media_worker.py` (one sequential worker per process)
* **Evidence:** P1-07 (a 40-chunk drip completed with no deadline); P1-08 (real socket, a 1 s timeout client took 4.84 s without timing out).
* **Impact:** a slow host pins the process's only media worker. At 25 MB the worst case is effectively unbounded. The 120 s visibility timeout requeues the job for a second, duplicate download. Past `MEDIA_UPLOAD_GRACE_SECONDS` (900 s) the reconciler may judge the intent `missing` before the PUT lands, which yields an unowned object.
* **Required semantics:** a total per-download deadline (`asyncio.timeout`) well inside the lease, classified as a retryable failure.

### MEDIA-11: A transaction and row lock are held across the Meta download

* **Severity:** MEDIUM · **Status:** Open · **Classification:** implementation defect (contradicts ADR-080 and the `download()` docstring)
* **Files:** `app/services/media_service.py:186-191` (flush before `fetch_media`); `app/workers/media_worker.py:244-276`
* **Evidence:** P7-01a. During the fetch there was 1 `idle in transaction` session and a granted `RowExclusiveLock` on `message_media`; a duplicate job waited out the entire first download.
* **Impact:** one pooled connection per in-flight download, held for as long as MEDIA-10 allows. Anything else touching that row, including a colleague's download request of it, waits.
* **Required semantics:** commit the `DOWNLOADING` transition before any network call, as ADR-080 requires.

### MEDIA-12: `FAILED` media is never retried, although documentation says it is

* **Severity:** LOW · **Status:** Open · **Classification:** documentation mismatch / reliability
* **Files:** `docs/MEDIA.md:65-72, 278`; `app/services/media_service.py:206, 457-462`; `app/db/models/media.py:49, 307`
* **Evidence:** P2-03. A CDN 503 gave `failed`, `attempts=1`, 1 job round, and the turn released with "unreadable". `_fail` returns normally, so the queue never retries. `is_exhausted` can only trigger on rows downloaded 3 times, which no path produces. `MEDIA.md` also says video is "downloaded and stored"; it is skipped before download.
* **Impact:** one transient blip longer than the client's 3 quick attempts permanently marks a customer's file unreadable.
* **Required semantics:** either document "no retry beyond the client's", or requeue `FAILED` with a delay up to `MAX_ATTEMPTS` before releasing.

### MEDIA-13: Media download ignores per-workspace credentials

* **Severity:** MEDIUM · **Status:** Open · **Classification:** implementation defect (conditional on deployment configuration)
* **Files:** `app/workers/media_worker.py:293-315` vs `app/services/credential_service.py:90-119`, `app/services/messaging_service.py:887-905`
* **Evidence:** by code, sends resolve the account's own token (ADR-034) while downloads always use `settings.meta_access_token`. P2-06: with no platform token, 5 rounds end in a dead letter, `pending`, and 0 turns.
* **Impact:** a workspace connected with its own credential under a different Meta app or business may be unable to have any inbound attachment downloaded, and each one then strands via MEDIA-04. A deployment relying only on workspace credentials loses every attachment.
* **Required semantics:** download with the credential of the account the message arrived on.
* **Deployment verification:** DV-4.

### MEDIA-14: A full local disk keeps the failed write's staging file

* **Severity:** LOW · **Classification:** implementation defect (`local` backend only) · **Files:** `app/core/storage.py:157-165`
* **Evidence:** P6, on a real 2 MB tmpfs: the third write gave `StorageError`, and a 248 KB `.partial` was left at 0 KB free.
* **Required semantics:** unlink the staging file on failure.

### MEDIA-15: Media observability has no alerts, no outcome counters and no transcription instrumentation

* **Severity:** MEDIUM · **Classification:** observability gap, documentation mismatch
* **Files:** `deploy/monitoring/alerts.yml` (no media rule); `app/integrations/openai/transcription.py` (no `ProviderCall`); `app/workers/purge_worker.py:155-168` (object failures only logged); `docs/MEDIA.md:280`
* **Impact:** MEDIA-03/04/07 are silent in production: stranded media shows only as generic dead-letter growth; quarantined objects, stuck purges and a transcription outage alert nobody.
* **Required:** a stranded-media gauge and alert; media outcome counter (closed labels); transcription `ProviderCall`; purge object-failure counter and alert; the reconciliation `quarantined` alert `MEDIA.md` already promises.

### MEDIA-16: Stored objects are served without re-verification (INFO)

* **Severity:** INFO · **Classification:** hardening. P3-06: an out-of-band replacement is served as the row's canonical type. The attacker must already hold bucket-write credentials, and the response is `attachment` + `nosniff`. Optional: verify size/hash on read.

### MEDIA-17: No audit trail for staff access to customer files

* **Severity:** LOW · **Classification:** product decision / observability. No `AuditAction` covers downloading or sending an attachment. The message row records `sent_by_id`; a download leaves no record. See PD-6.

### MEDIA-18: Twelve mutation survivors on stated guarantees

* **Severity:** MEDIUM · **Classification:** test gap · See §32/§33. Each survivor needs a permanent killer test; M12, M13, M21, M29 and M30 guard properties the code gets right today but nothing holds.

### Ledger

| ID | Severity | Short title | Classification | Blocking? |
| -- | -------- | ----------- | -------------- | --------- |
| MEDIA-01 | HIGH | Bearer token forwarded to any public host | security defect | **BLOCKER BEFORE PRODUCTION** |
| MEDIA-02 | HIGH | Unbounded in-process PDF parse on shared loop | reliability/availability | **BLOCKER BEFORE PRODUCTION** |
| MEDIA-03 | HIGH | Poison file / dead letter strands the conversation forever | reliability | **BLOCKER BEFORE PRODUCTION** |
| MEDIA-04 | HIGH | Descriptor failure escapes, retried, strands | reliability | **BLOCKER BEFORE PRODUCTION** |
| MEDIA-05 | HIGH | Dead DataError net; long filename, 500 retry storm | reliability / test gap | **BLOCKER BEFORE PRODUCTION** (+ DV-3) |
| MEDIA-06 | MEDIUM | Outbound filename fails after delivery | reliability | REMEDIATION REQUIRED |
| MEDIA-07 | MEDIUM | Purge leaves unrecorded customer objects | privacy/retention | REMEDIATION REQUIRED |
| MEDIA-08 | MEDIUM | Media worker ignores lifecycle/released numbers | implementation defect | REMEDIATION REQUIRED |
| MEDIA-09 | MEDIUM | Uncapped 4xx body | implementation defect | REMEDIATION REQUIRED |
| MEDIA-10 | MEDIUM | No total download deadline | reliability | REMEDIATION REQUIRED |
| MEDIA-11 | MEDIUM | Row lock/transaction across Meta fetch | implementation defect | REMEDIATION REQUIRED |
| MEDIA-13 | MEDIUM | Download ignores per-workspace credential | implementation defect | REMEDIATION REQUIRED (+ DV-4) |
| MEDIA-15 | MEDIUM | No media alerts / transcription metrics | observability gap | REMEDIATION REQUIRED |
| MEDIA-12 | LOW | FAILED never retried; doc drift | documentation mismatch | REMEDIATION REQUIRED |
| MEDIA-14 | LOW | `.partial` left on ENOSPC | implementation defect | REMEDIATION REQUIRED |
| MEDIA-17 | LOW | No audit of file access | product decision | PRODUCT DECISION |
| MEDIA-16 | INFO | No re-verification on read | hardening | FUTURE FEATURE |
| MEDIA-18 | MEDIUM | 12 mutation survivors (M06 M07 M10 M12 M13 M14 M21 M25 M26 M28 M29 M30) | test gap | TEST GAP |

---

## 35. Product Decisions Required

| ID | Decision | Current state | Why it is the product's call |
|---|---|---|---|
| PD-1 | Media retention period | `MEDIA_RETENTION_DAYS=0` (keep forever) | how long a business keeps customer files is contractual |
| PD-2 | Malware scanning of inbound documents | none. Staff can download arbitrary PDF/Office files (served `attachment`, never rendered by Wasla); nothing is executed server-side | the risk lands on staff endpoints; scanning costs money and latency |
| PD-3 | EXIF/GPS stripping | preserved; visible only to the workspace's staff and sent to the vision provider | location metadata may be useful (delivery, damage) or a privacy liability |
| PD-4 | Supported types (video understanding, OCR, OOXML reading) | video/OOXML skipped; no OCR | feature scope |
| PD-5 | Store inbound files for a suspended workspace? | stored and read (MEDIA-08) | reading with a paid model is a defect; storing is a policy choice |
| PD-6 | Audit staff access to customer files | not audited (MEDIA-17) | compliance posture |
| PD-7 | Storage quota sizes / transcription pricing | quota exists (`STORAGE_BYTES`); transcription metered per recording | commercial |

## 36. Future Media Requirements

* **FR-1** A general media host allowlist per provider (needed again for any new channel: Instagram, Messenger).
* **FR-2** A bounded, isolated decoder service if images, video or OCR are ever processed locally (never on the shared event loop).
* **FR-3** Streaming storage I/O if `MEDIA_MAX_BYTES` rises (today a file is held whole, which is acceptable at 25 MB and sequential).
* **FR-4** If conversation, contact or message deletion is ever added, object deletion must be part of it: rows cascade today, objects would not.
* **FR-5** Malware scanning hook between `intend` and `finalize`, if PD-2 decides for it.

## 37. Final Deployment Verification Backlog

**FINAL PRODUCTION DEPLOYMENT VERIFICATION**: things that genuinely need a real sandbox or production:

* **DV-1** Real Meta media download end to end: the actual descriptor URL host(s) and redirect chain, to build MEDIA-01's allowlist. `REAL_META_WHATSAPP_E2E_VERIFICATION.md` does not cover inbound media.
* **DV-2** Meta's status codes for expired, deleted or foreign media ids (MEDIA-04 classification).
* **DV-3** Whether Meta delivers document filenames longer than 300 characters, and its caption and filename limits (MEDIA-05 reachability).
* **DV-4** Whether the platform token can download media for a number connected with its own credential under a different app or business (MEDIA-13).
* **DV-5** OpenAI's real size limits for image input and transcription against `MEDIA_MAX_BYTES` = 25 MiB.
* **DV-6** Production bucket: private policy, no public access block exceptions, versioning and lifecycle expiry of non-current versions (retention and purge are otherwise "not retrievable" rather than "gone"), SSE.
* **DV-7** Retention and purge jobs enabled, and alert delivery for the rules MEDIA-15 adds.
* **DV-8** Container memory and CPU limits and the worker topology (how many media workers share a loop and a host).


---

## 38. Reliability / Security Score

Weights chosen before scoring, from where media risk actually lies (tenant data first, then the untrusted fetch/parse boundary).

| Dimension | Weight | Score /10 | Weighted | Reason |
|---|---|---|---|---|
| Tenant isolation | 0.12 | 9.5 | 1.140 | held on every path against real objects; M01 killed; storage deliberately not an authz boundary |
| URL / SSRF safety | 0.10 | 7 | 0.700 | 44/44 refused with connect-time pinning; but no host allowlist, and fetch-path checks unpinned (M06/M07) |
| Credential handling | 0.10 | 3 | 0.300 | platform bearer reaches any public host (MEDIA-01); per-workspace credential ignored (MEDIA-13) |
| Size / resource bounding | 0.10 | 4 | 0.400 | success body correctly capped by actual bytes; error body uncapped (MEDIA-09); no total deadline (MEDIA-10) |
| Type validation | 0.06 | 9 | 0.540 | bytes-first exact allowlist, canonical type everywhere; M05 killed |
| Parser safety | 0.08 | 2 | 0.160 | 25 KB PDF gives 170 s loop stall / 763 MB; 24% of fuzzed PDFs escape containment (MEDIA-02/03) |
| Storage correctness | 0.06 | 8 | 0.480 | safe keys, private bucket, SigV4, 403≠missing; `.partial` leak on ENOSPC (MEDIA-14) |
| DB/object atomicity | 0.06 | 7 | 0.420 | intent-first protocol and reconciler are sound; purge windows orphan objects; M12/M13 unpinned |
| Idempotency / replay | 0.05 | 8 | 0.400 | duplicate jobs converge to one object/meter/read/turn; duplicate Meta download only |
| Lifecycle / deletion | 0.07 | 4 | 0.280 | retention sound; purge failure/race leaves unrecorded objects; lifecycle ignored (MEDIA-07/08) |
| Privacy / logging | 0.05 | 8.5 | 0.425 | no token/secret/URL/filename in logs; EXIF preserved by design; no access audit |
| Failure containment | 0.08 | 2 | 0.160 | poison file, descriptor failure, long filename each strand turns; the first two block the conversation for good (MEDIA-03/04/05) |
| Observability | 0.03 | 3 | 0.090 | metrics exist but no media alert, no stranded gauge, transcription uninstrumented (MEDIA-15) |
| Testing | 0.02 | 5 | 0.100 | 550 targeted tests, 18/30 mutations killed; hostile-input paths untested |
| Operational readiness | 0.02 | 4 | 0.080 | real Meta inbound media never verified (DV-1..4); config guards good |
| **Total** | **1.00** | | **5.67** | |

**Media Reliability & Security Score: 5.7 / 10.**

---

## 39. Final Verdict

**Not production-ready for customer media.** Five blockers must be remediated first: MEDIA-01, MEDIA-02, MEDIA-03, MEDIA-04, MEDIA-05.

The answer to the audit question is split along a clear line.

**Where a file lives and who may see it: yes.** Tenant isolation, key integrity, filename handling, type integrity, bucket privacy, SSRF address checks, secret hygiene and the DB/object write protocol held under every attack in this audit.

**Hostile, malformed, slow or duplicated input while it is fetched and read: no.**

* One sub-kilobyte PDF, or a single Meta 4xx on a descriptor, permanently silences a conversation's attachment replies.
* One 25 KB PDF freezes every worker in the process for minutes.
* A redirect or descriptor URL on any public host receives the platform Meta token.
* An over-long filename makes a whole delivery fail on every Meta retry, because the net written for exactly that class of failure cannot catch what asyncpg raises.

None of the blockers requires a redesign. They need a host allowlist for credentials, the existing bounded PDF child, classified containment for the probe and parser plus a terminal state on exhaustion, and bounded display metadata with the correct exception class at the webhook.

After those five, the MEDIUM items (purge orphans, lifecycle, uncapped error body, deadline, lock across fetch, per-workspace credential, alerts) are ordinary remediation. Real-Meta inbound media verification (DV-1..DV-4) has never been done and must precede launch.
