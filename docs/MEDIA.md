# Media

Scope: files customers attach, how they are read, where they are stored, and how a business sends one back.

**Status: Implemented** (Phase 9). See [../TASKS.md](../TASKS.md) phase 9.

## The problem this solves

WhatsApp has carried media since phase 3 and Wasla stored none of it. A photograph arrived, its message row was written with no body, and the agent answering the conversation was shown the literal string `[image]`. A customer photographing a product and asking "how much is this one?" got an answer about nothing.

## What happens to an inbound file

```
Webhook delivery
      ↓
Message stored, caption becomes the body
      ↓
message_media row written, status PENDING
      ↓
Media job enqueued  ← not an agent job
      ↓
Media worker: claim → lifecycle → probe → download → store → read
      ↓
Transcript on the row, status READY  (or SKIPPED / FAILED, with a reason)
      ↓
Nothing else unresolved on this conversation?
      ↓
Agent job enqueued
```

A message on a number the workspace has since **released** is recorded as history and not answered ([WHATSAPP.md](WHATSAPP.md)); a file on it is recorded the same way — its row is written `SKIPPED` at the webhook and no media job is queued, so Meta is never asked for it.

The webhook does none of the work. It resolves the workspace, stores the event, notes the attachment and returns, exactly as it does for text ([WHATSAPP.md](WHATSAPP.md)).

## Captions and transcripts are different things

| | Where it lives | What it is |
| --- | --- | --- |
| Caption | `messages.body` | What the customer typed |
| Transcript | `message_media.transcript` | What Wasla concluded the file says |

These never merge. A stored conversation in which an inference is indistinguishable from what somebody actually said cannot be trusted afterwards — by a colleague reading the thread, or by anyone asking later what was really said. The agent sees both, with the machine half labelled:

```
how much is this one?
[image] A blue three-seat sofa with a price tag reading 4,500 EGP.
```

## How each kind is read

| Kind | Route | Produces |
| --- | --- | --- |
| Image, sticker | Responses API, image input | A description written for an agent |
| Voice note, audio | Transcription endpoint | The words spoken |
| PDF | The bounded parser child (below) | The text layer |
| Plain text | Local decode | The text |
| Video, Office documents, anything else | Not read | `SKIPPED` before download |

Images travel as data URLs rather than links: the alternative is putting every customer's attachment behind a URL a provider can reach, which is far wider exposure than sending the bytes for one request. Image understanding reuses `ResponsesClient` through an `images` field on `Turn`, so it inherits the retry and timeout policy rather than growing a second copy of it.

Transcription forces no language. This product's customers switch between Arabic and English inside a single sentence, and pinning one makes the other come back as nonsense rather than as a translation.

The vision prompt asks for exact transcription of any text in the image — a price, a receipt, a serial number. A description that paraphrases a price list throws away the entire message.

**A PDF is parsed in a process that can be killed, never on the worker's event loop** (MEDIA-02, PD-MEDIA-09). The message path used to run `pypdf` in-process with no bound but a page count; a 25 KB PDF whose content stream inflates to text operators stalled every worker in the process for 170 seconds at 763 MB, and a sub-kilobyte malformed PDF raised exceptions outside the parser's catch list that escaped the job entirely (MEDIA-03). The reader now uses `extract_pdf_bounded` — the child the knowledge base already used (RAG-02) — with the same limits unchanged: 300 KB of input, 40 pages, 400,000 characters of text, 768 MB of address space and 20 seconds of wall clock. A PDF past any of them is stored like any other file and **not read**: `SKIPPED`, "too large or too complex to read automatically". Safety takes priority over reading every stored PDF, so a customer's PDF over 300 KB is kept but not understood; the parser limits are not raised to fit. Whatever the parser raises, the child crashes and the parent sees one fixed refusal; above that, any unexpected reader exception is contained as `FAILED` for that file alone.

## Statuses, and the two ways of giving up

`PENDING` → `DOWNLOADING` → `STORED` → `READY`, with two exits.

| Outcome | Meaning | Retried |
| --- | --- | --- |
| `SKIPPED` | Wasla decided not to process it | Never |
| `FAILED` | An attempt broke, after the provider client's own retries | Never |

A file over the size cap, of a type nothing can read, a silent recording, a scanned PDF, a PDF past the parser's limits, a file Meta says it no longer has, a workspace no longer served — each is **skipped**: a decision no retry changes. A provider or Meta failure is **failed**. Both are final.

**The retry contract** (PD-MEDIA-08). A transient provider failure — a 5xx, a 429, a reset connection — is retried inside the client that met it, three attempts with backoff. After that the file is `FAILED`, and the conversation is released at once. There is **no second, queue-level round**: this document used to promise one "to `MAX_ATTEMPTS`", and none existed — a failed file was never retried (MEDIA-12). A permanent answer is not retried at all: a 400/404/410 on the descriptor is `SKIPPED` after one request, a 401/403/code-190 is `FAILED` after one. The customer is answered with the file marked unreadable, and can send it again.

Every failure carries a reason from a closed vocabulary (`app/services/media_outcomes.py`) with a short, fixed Wasla sentence. No provider text, MIME string, parser message or filename ever reaches `last_error`: an agent is shown that sentence, and a provider string interpolated into it once overflowed the column and stranded the conversation (MEDIA-04).

Both count as resolved: the customer is still owed an answer, and an agent that says it could not open the attachment is better than one that never speaks. Memory renders an unreadable file with its reason, so the agent can say what happened.

## Every file resolves

The reply to a conversation waits until none of its files is unresolved (`PENDING`, `DOWNLOADING`, `STORED`), so one file that never resolves silences every later attachment in that conversation. Four mechanisms make sure none stays unresolved:

1. **Every failure inside an attempt is classified** — the descriptor lookup, the download, the store, the reader — into a terminal state with a reason (above).
2. **An attempt claims the file.** `message_media.claim_id` and `claimed_at` are committed before any network call. A duplicate job finds a live claim and stands aside, so one file costs one download and one paid read; every later write checks the claim is still the attempt's own.
3. **A dead-lettered job gives its file up.** If a media job is dead-lettered anyway — an infrastructure failure outlasting the queue's retries — the worker marks the file `FAILED` (`abandoned`) and re-evaluates the conversation under the same gate, idempotently.
4. **A recovery sweep finishes what nobody will** (`MediaRecoveryWorker`, run under the `media` kind every minute). A claim older than the longest an attempt can take (the *claim lease*: download deadline + two store timeouts + understanding deadline + 60 s, 330 s by default) belongs to a worker that died; a file unclaimed for longer than the queue's whole retry budget (about 20 minutes by default) has a lost job. Both horizons are computed from configuration (`app/services/media_horizons.py`), never chosen beside it. A stranded file is put back on the queue while it has attempts left and given up on (`FAILED`, `abandoned`, conversation released) after `MAX_ATTEMPTS` (3). A file whose inbound event is still `received` is inbound recovery's, and the sweep leaves it alone.

**No database connection is held across somebody else's network** (MEDIA-11). Meta's descriptor, every redirect and the body, the object write, the read-back, vision, transcription and the PDF child's wait all run with no transaction open; the claim, the intent, the finalisation and the result are each a short transaction of their own.

**One download has a wall-clock deadline** (MEDIA-10): `MEDIA_DOWNLOAD_DEADLINE_SECONDS`, 90 seconds by default, around the descriptor and the whole body together. The HTTP client's timeouts are per read, so a host dripping a byte inside each one used to have no end. Start-up refuses a deadline at or past the queue's visibility timeout (120 s), so one download cannot outlast the lease its job was reserved under. Understanding has its own deadline, `MEDIA_UNDERSTANDING_DEADLINE_SECONDS` (120 s).

## The workspace a file belongs to

**Lifecycle** (MEDIA-08, PD-MEDIA-05). Before Meta is asked anything, again before the downloaded object is written, and again before a paid read, the worker reads the workspace's and the number's state fresh from the database. A **suspended** workspace, a **soft-deleted** one, and a **released or paused** number end the file `SKIPPED` with the reason — no descriptor, no download, no object, no vision, transcription or PDF parse, no provider spend — and the conversation is re-evaluated as usual; any agent turn that follows is refused by the agent worker's own lifecycle check (AI-06). An object already stored before a suspension is kept under ordinary retention and purge. A **closed conversation** or a **disabled agent** is not a reason to drop the file: it is the customer's message and is kept; whether anybody answers is the agent worker's decision.

**Credentials** (MEDIA-13). A file is fetched with the credential of the number it arrived on, resolved server-side from the conversation's account by `CredentialService` — the same authority model outbound sends use ([ADR-034](../DECISIONS.md)). A number connected with its own token is fetched with it; one without uses the platform token. A workspace token the process cannot decrypt is never downgraded to the platform's, and a file with no usable credential is `FAILED` (`credential_unavailable`) rather than left pending. The token lives only in the client built for that attempt: never on the row, in a log line or in anything an agent reads.

**Which hosts get the token** (MEDIA-01). Every read hop — descriptor, file, and each redirect — attaches `Authorization` in one place, and only to a host inside `META_MEDIA_HOST_ROOTS` (default `graph.facebook.com`, `fbsbx.com`, `fbcdn.net`), matched on a label boundary after IDNA normalisation, over https on 443, with no userinfo and no address literals. A hop anywhere else is **refused**, not fetched anonymously. The token used to be re-attached on every hop to any public host. This is a separate control from the SSRF address checks in `app/core/net.py`, which still apply to every hop. The production host list is confirmed in deployment verification (DV-1) and changes by configuration.

**Error bodies** are read to 16 KiB and never logged; Graph descriptors are read to 64 KiB and template pages to 4 MiB (MEDIA-09). A failed read costs less memory than a successful one.

## Size, and paying to find out

The cap (`MEDIA_MAX_BYTES`, 25 MB by default) is checked against the size Meta declares before the file is fetched, and then **enforced while the body is read**. `fetch_media` takes a required `max_bytes` and streams, abandoning the download mid-chunk once it passes. A buffered fetch learns a file was too big only when the process is already holding it, which makes the limit a description of what happened rather than a control. Asking first is still worth the round trip — the alternative is moving ninety megabytes to discover it was too big to keep.

The upload route reads in chunks for the same reason, so an oversized attachment is refused within a chunk of `MAX_UPLOAD_BYTES` (16 MB) rather than after the whole body is in memory.

## What a file is

**The declared type is a hint. The bytes are the answer** ([ADR-076](../DECISIONS.md)).

`app/core/media_types.py` identifies a file from a bounded prefix of its own content and returns a *canonical* type. That canonical type is what Meta is told, what the reader routes on, what goes in the `mime_type` column, and what the download handler serves. Neither `file.content_type` from a browser nor `mime_type` from Meta's media descriptor decides anything on its own.

| Situation | Result |
| --- | --- |
| Claim agrees with the bytes | Accepted, stored under the canonical spelling |
| Claim contradicts the bytes | Refused — 400 on upload, `SKIPPED` on download |
| Claim absent or `application/octet-stream` | The bytes decide alone; an ambiguous pair is refused, except text, which takes `text/plain` |
| Bytes of no supported format | Refused, whatever was claimed |
| Container that genuinely carries two types | The claim picks within the pair, and can never widen it |

The supported set is an **exact allowlist**. There is no `image/*` family rule left to widen, which is what used to admit `image/svg+xml` — a script a browser will run given the chance.

| Class | Canonical types |
| --- | --- |
| Image | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| Audio | `audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/amr`, `audio/aac`, `audio/wav`, `audio/webm` |
| Video | `video/mp4`, `video/3gpp`, `video/webm` |
| Document | `application/pdf`, `text/plain`, `text/csv`, `application/msword`, `application/vnd.ms-excel`, and the three OOXML types |

**What this does not claim.** Detection answers "are these bytes a supported container of a known format?". It does not prove the file is harmless — a valid JPEG can carry a decoder exploit and a valid PDF can carry JavaScript — and nothing here scans for malware. What it removes is the class where a file is processed and served as a type it is not. Note also that bytes which decode as text are `text/plain`: an HTML file can still be stored, as text, served as text, behind the disposition and `nosniff` that were always there.

Two ambiguities are stated rather than guessed at. Matroska carries audio and video under one signature, and an OLE2 compound document is Word or Excel with the same first eight bytes; detection narrows each to its pair and the claim chooses within it.

## Storage

Files go through a `MediaStorage` interface, implemented on local disk ([ADR-023](../DECISIONS.md)). Keys are `{tenant}/{year}/{month}/{uuid}{ext}`, produced by the store.

The workspace prefix makes deleting or relocating one workspace's files a single operation. The generated identifier is what guarantees a customer-supplied filename can never influence where a file lands — `../../etc/passwd` arrives as a filename occasionally, and it is a request rather than an accident. The filename is recorded for display and never consulted when building a path; keys are checked against both a pattern and a containment test on every read.

`MEDIA_STORAGE_BACKEND` chooses the implementation ([ADR-077](../DECISIONS.md)).

| Backend | What it is | When |
| --- | --- | --- |
| `local` | A path on this host | Development, and a deliberate single-host deployment |
| `s3` | An S3-compatible bucket | Anything with a second host, or that must survive losing the first |

**Local disk means the API and worker containers share a volume**: one writes the file, the other serves it back. That is a single-host arrangement, and it is also the arrangement in which one lost disk is every attachment every workspace ever received.

**`s3` is one protocol, not one provider.** AWS S3, MinIO, Cloudflare R2, Wasabi, Backblaze B2 and Ceph all speak the S3 object API. The requests are signed here rather than by `boto3` — the SDK is synchronous in an application that is not, and the four operations needed (PUT, GET, DELETE, HEAD on one object) are the simplest possible use of SigV4. A real MinIO drill is what proves that; mocking a `boto` call proves nothing about a signature.

Keys are identical across both backends, so a key written by one is a key the other reads — migrating is copying objects, not rewriting rows.

**The bucket is private and there is no way to ask otherwise.** No ACL header is sent, no presigned URL is issued, and bytes reach a colleague only by being streamed through the authenticated API. Selecting `s3` with an incomplete configuration **refuses to start**: falling back to local disk would give the API and the worker each their own copy on their own container, and every download would be a coin toss.

**These are not the backup credentials.** `MEDIA_S3_*`, never the `AWS_*` pair the backup container holds ([ADR-075](../DECISIONS.md)). Media and backups are different buckets under different credentials, so an application container that is taken over still cannot delete the copies of the database — and the backup container, whose job is deleting old files, holds no media credential. The deployment guard asserts both directions.

Object storage is **not** an authorization boundary. A key prefix is a layout; tenant isolation is the scoped repository, and the isolation tests run against both backends for exactly that reason. Recovery follows the same rule: an object is settled because a row carrying a `tenant_id` says it was intended, never because a key begins with a workspace's identifier.

**A key is allocated by the caller, not by the store.** `build_key` is a pure function of a workspace and a type, so the key can be committed to PostgreSQL before anything is written at it — which is what makes an interrupted write something a query can find. `MediaStorage` therefore has no operation that allocates and writes in one step.

## Retention

Nothing ever deleted a stored file, so the store grew monotonically ([ADR-078](../DECISIONS.md)). `MEDIA_RETENTION_DAYS` is what stops that, and **zero — keep everything — is the default**: how long a business keeps what its customers sent it is not a decision this code can make, and an invented number that deletes customer data is worse than no sweep.

**The file goes, the row stays.** `transcript` is what the agent was shown and what a colleague reading the thread sees; deleting it would rewrite the record of a conversation. Afterwards the row says plainly that there was a file and that it was removed, and a download answers 404 with that reason rather than failing as a storage error — "deleted on purpose" and "the store is unavailable" are different sentences.

The sweep is two writes, because removing an object and clearing the column pointing at it are two systems and no transaction spans both:

```
older than MEDIA_RETENTION_DAYS
      ↓
purge_started_at set, committed      ← survives this process dying
      ↓
object deleted
      ↓
storage_key cleared, committed
```

A pass that dies anywhere leaves a claimed row; the next pass deletes again — a no-op on an object already gone — and finishes. The other order would leave a row pointing confidently at a file that is gone, which is indistinguishable from a broken store. A **reconciliation** pass runs first each sweep and finishes claimed rows whatever their age now, because raising the retention period after a failed pass would otherwise strand them.

A poll rather than a queue: enqueueing one job per file would put the deletion of customer data behind the replay command, where an operator could re-run a dead-lettered purge weeks later against a row since re-populated.

### When the whole workspace is purged

`MEDIA_RETENTION_DAYS` removes files by age; a **workspace purge** removes a deleted workspace's rows once its retention window has passed ([RUNBOOK.md](RUNBOOK.md)). The rows and the objects are two systems again, and the purge used to hold the keys only in memory between deleting the rows and deleting the objects — so a refused delete, or a process dying after the commit, left the customer's files in the bucket with no row naming them and the workspace recorded as purged (MEDIA-07).

Every key now goes to `media_purge_objects` **in the same statement that deletes its row** (`DELETE … RETURNING` into the ledger), so no key can slip between reading and deleting. The purge worker deletes the objects after the commit and removes each ledger row only once the store confirms the object is gone; a refused delete keeps its row and is retried every five minutes. A workspace is fully erased when `purged_at` is set **and** it has no ledger rows. A key whose upload was still mid-write at the purge is not deleted before `MEDIA_UPLOAD_GRACE_SECONDS`, so a write that lands afterwards is deleted rather than orphaned; the writer also removes its own object at once if it finds its row gone. `MediaPurgeDeletesFailing` fires when deletes stay owed for six hours.

**On a versioned bucket, deleted does not mean gone.** A delete leaves a delete marker and previous versions stay until the bucket's own lifecycle rule expires them. What retention guarantees is that the object is no longer retrievable through Wasla; making it unrecoverable is a rule configured on the bucket.

**Retention only claims fully stored files.** An upload still in flight carries a key and is as old as its message the moment it exists, so an age query over keys would select it — and deleting the object of a write nobody has finished is retention destroying a file rather than expiring one. Those belong to the recovery pass below.

## The write protocol

An object and the row that owns it live in two systems, and no transaction spans them. Retention deals with one direction — remove the object, then clear the reference. The write is the other, and it used to run the wrong way round: the object was created and the row committed afterwards, so a transaction that failed in between left an object nothing referenced, invisible to every query in the system ([ADR-087](../DECISIONS.md)).

So the key is committed **before** the object can exist:

```
TX1   allocate the object key, record its size and the hash of what
      belongs in it, state = pending                        ← COMMIT
      ↓
--    write the object. No transaction, no connection held,
      no row lock across the store.
      ↓
TX2   re-read under a row lock, confirm it is still ours,
      state = stored                                        ← COMMIT
```

Anything that goes wrong after the first commit leaves a row that names the exact object, so recovery starts from PostgreSQL and never from the bucket. Both directions of the pair fail; only this one fails recoverably.

`message_media.storage_state` carries the whole lifecycle, because `storage_key IS NULL` no longer answers the question:

| State | Means | Object |
| --- | --- | --- |
| `absent` | No object, and none intended | — |
| `pending` | A key is committed; the write may or may not have landed | Unknown |
| `stored` | Verified. The only state anything is served from | Present |
| `purging` | Retention claimed it; the delete may or may not have run | Unknown |
| `purged` | Retention finished. Not "never downloaded" | Gone |
| `mismatched` | An object is at our key and it is not what we wrote | Foreign |

A check constraint says what each state is allowed to look like, so a consumer can trust the column instead of re-deriving the lifecycle from which columns happen to be null. `UNIQUE(storage_key)` gives every object exactly one owning row, so recovery never has two candidates and no way to choose.

**A retry reuses the committed key.** The allocation happens under a row lock, so a redelivered media job finds the key the first attempt committed and writes its bytes there. Two attempts each minting a fresh key would each write an object, of which only one could end up on the row — which is the orphan again, through the front door.

## Recovering an interrupted write

The `uploads` worker polls for intents that have sat in `pending` past `MEDIA_UPLOAD_GRACE_SECONDS`, claims them with `FOR UPDATE SKIP LOCKED`, and asks the store about each one by its exact key. Four answers, not two:

| The store says | Verdict | What happens |
| --- | --- | --- |
| The object is there and hashes to what the row expects | `finalized` | `stored`. The attachment becomes readable |
| The object is there and does not | `mismatched` | Quarantined. Not served, and **not deleted** — it is the only evidence of how it got there |
| The object is not there | `missing` | The row goes back to owning nothing, so a later attempt can allocate afresh |
| Nothing, or a refusal | `unreachable` | Nothing is written. The next pass asks again |

The fourth is the one that matters. A store that is down, read as "the object is gone", would abandon every upload in flight during the outage — and for an outbound attachment, whose bytes arrived in a request body that no longer exists, abandoning is final. So `exists()` raises rather than answering `False`, and a `403` is treated as a refusal rather than as a missing key: S3 returns it both for a key you cannot list and for a credential that has been rotated, indistinguishably.

**Verification is a recomputed SHA-256 over the bytes read back.** Not the ETag, which S3 defines as opaque and which is the MD5 of the body only for a single-part unencrypted upload; not a header travelling with the object, which is a claim rather than a fact. The cost is one GET bounded by `MEDIA_MAX_BYTES`, on a path that runs only for writes that were interrupted.

**No bucket-listing orphan sweep exists, and none can be added by accident.** Finding an object with no row would mean listing the store and deciding from age, and a sweep on that rule eventually deletes a live file whose row it failed to read — a PostgreSQL failure, a lagging replica, a query that timed out, each one making a live attachment look like an orphan. `tests/unit/test_no_bucket_listing.py` makes that structural: the storage protocol has four operations, each takes one key, and no module under `app/` speaks the S3 listing API. A sweep of that shape cannot be written without first changing that file.

## Durability

**The PostgreSQL backup does not cover media, and never did** ([BACKUP.md](BACKUP.md)). It carries the `message_media` rows — the transcript, the type, the size, the key — and none of the bytes those keys point at. The two halves have separate owners:

| | Owner | Restores |
| --- | --- | --- |
| Metadata and references | The PostgreSQL backup, off-host ([ADR-075](../DECISIONS.md)) | Which files existed, what they said, who they belong to |
| The bytes | The object store's own durability, versioning and replication | The files themselves |

On `local` there is no second owner: the volume is the only copy, and losing the host loses every attachment. That is the sentence `MEDIA_STORAGE_BACKEND=s3` exists to stop being true.

A disaster recovery that restores the database and points it at a surviving bucket gets both halves back. One that restores the database alone gets rows whose keys resolve to nothing — which the API reports as a storage error per file rather than as a failure to start, so the rest of the product works while the store is being restored.

## One reply per conversation

Two photographs in one delivery are two jobs, possibly on two workers. Each finishes and asks whether anything is still unread; if both ask at the same moment, both see nothing and both ask an agent to answer. An agent turn is not idempotent, so the customer gets two replies to one question.

`ConversationMediaGate` takes a row lock on the conversation before the count, which makes the second worker wait for the first to commit. No new table and no Redis key — the lock is held for a single count. `tests/integration/test_media_release_race.py` builds that race on two real connections and observes the lock contended.

## Sending

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/v1/conversations/{id}/messages/media` | any member |
| GET | `/api/v1/conversations/{id}/media/{media_id}` | any member |

Sending uploads the file to Meta and sends the returned id. A hosted link would need every attachment behind a publicly reachable URL for as long as Meta might fetch it; an upload exposes the bytes to one recipient for one send.

An attachment is a free-form message, so the **24-hour service window applies** exactly as it does to text ([ADR-012](../DECISIONS.md)). Outside it, only an approved template will do.

Meta groups attachments as `image`, `audio`, `video` or `document`, which are not the mime families — `application/pdf` is a *document*. Wasla's accepted list is narrower than Meta's: Meta will carry almost any file as a document, and a business forwarding an executable to a customer is not a feature anyone asked for.

**The attachment's name is settled before Meta is asked anything** (MEDIA-06). A name longer than the column (300 characters) or carrying a control character is refused with a 422 and nothing is sent; a storable name is used in one canonical form (NFC, trimmed) for the recorded row, and the Meta-safe upload name is derived from it. It used to be stored raw after delivery, so a long or NUL name reached the customer and then failed to record. An **inbound** name is never refused — the message is the customer's — and is normalised instead: control characters removed, bounded to 300 characters keeping a short extension, Arabic and other scripts kept (MEDIA-05). Neither is ever used for a path, a key or a header.

**Colleague access is audited** (MEDIA-17, PD-MEDIA-06): `media_downloaded` when a file is served to a member and `media_sent` when a member's attachment send may have reached the customer, each with the actor and internal ids only — never the filename, caption, key, URL, transcript or content. The worker reading a file is not a colleague and is not audited.

Serving a file back goes through the application rather than from a public URL, because a customer's photograph is workspace data and a link needing no authentication is a link that leaves the workspace. `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` are not decoration: a customer-supplied HTML file rendered inline on this origin is a script running against whoever is reading the inbox.

## Documents in the knowledge base

The PDF parser added here also settles a note `KnowledgeService` had carried since phase 6, which refused PDFs for want of one. A PDF is submitted base64-encoded and a **scanned** one — a photograph of a page, no text layer — is refused with a message saying so, rather than ingested empty. An empty document looks perfectly indexed from the outside and answers every question with nothing. See [RAG.md](RAG.md).

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_STORAGE_BACKEND` | `local` | `local` or `s3` |
| `MEDIA_STORAGE_PATH` | `/var/lib/wasla/media` | `local` only; shared volume between API and worker |
| `MEDIA_MAX_BYTES` | `26214400` | 25 MB storage cap per file; PDF *reading* has its own 300 KB bound |
| `MEDIA_DOWNLOAD_DEADLINE_SECONDS` | `90` | Wall clock over descriptor + body; must be below `QUEUE_VISIBILITY_TIMEOUT_SECONDS` |
| `MEDIA_UNDERSTANDING_DEADLINE_SECONDS` | `120` | Wall clock over read-back + vision / transcription / PDF child |
| `META_MEDIA_HOST_ROOTS` | `graph.facebook.com,fbsbx.com,fbcdn.net` | Hosts a media read may send the Meta token to; confirm in DV-1 |
| `MEDIA_S3_BUCKET` | — | Required for `s3`; must be private |
| `MEDIA_S3_ENDPOINT_URL` | — | Empty means AWS. A bare origin, no path |
| `MEDIA_S3_REGION` | `us-east-1` | |
| `MEDIA_S3_ACCESS_KEY_ID` | — | Required for `s3`. Not the backup credential |
| `MEDIA_S3_SECRET_ACCESS_KEY` | — | Required for `s3`. Not the backup credential |
| `MEDIA_S3_PATH_STYLE` | `true` | Required by MinIO and most self-hosted gateways |
| `MEDIA_S3_SERVER_SIDE_ENCRYPTION` | — | `AES256` asks the store to encrypt at rest |
| `MEDIA_S3_TIMEOUT_SECONDS` | `30` | |
| `MEDIA_RETENTION_DAYS` | `0` | Zero keeps everything, and is the default |
| `MEDIA_RETENTION_BATCH_SIZE` | `200` | Rows per sweep |
| `MEDIA_RETENTION_POLL_SECONDS` | `86400` | Daily |
| `MEDIA_UPLOAD_GRACE_SECONDS` | `900` | How long an intent must sit before recovery treats it as abandoned rather than in progress |
| `MEDIA_UPLOAD_RECOVERY_POLL_SECONDS` | `300` | Minutes, not days: what it finds is a file somebody is waiting for |
| `MEDIA_UPLOAD_RECOVERY_BATCH_SIZE` | `100` | Intents per pass |
| `OPENAI_VISION_MODEL` | `gpt-4.1-mini` | Separate budget from the answering model |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | |

Without an OpenAI key, documents are still read — extraction needs no provider — and images and voice notes are recorded as unreadable rather than crashing the worker.

## Known gaps

- **No OCR** (PD-MEDIA-04). A scanned document is reported as unreadable rather than being read. Recorded honestly on the row, so nobody is left wondering.
- **Inbound video is neither downloaded nor stored.** It is skipped on Meta's declared type before any download; there is no route from a video to a transcript, and no transcoding. (This section used to say video was downloaded and stored; it never was.) Office documents are skipped the same way. Outbound video is uploaded to Meta and stored.
- **PDFs over 300 KB are stored but not read** (PD-MEDIA-09), because the bounded parser's limits are the knowledge base's and were not raised.
- **No malware scanning** (PD-MEDIA-02). Customer documents are stored, served to colleagues only as attachments with `nosniff`, and never executed server-side.
- **EXIF and GPS metadata are preserved** (PD-MEDIA-03). Images are stored byte for byte, visible to the workspace's colleagues and sent to the vision provider.
- **Stored objects are not re-verified on read** (MEDIA-16, accepted hardening). An object replaced by somebody with bucket-write access is served as the row's type; the defences are the private bucket, the authenticated tenant route, `attachment` disposition and `nosniff`.
- **Nothing streams.** A file is read into memory whole, bounded by the download cap and by a smaller cap on the upload endpoint.
- **A quarantined object is never cleaned up automatically.** `mismatched` is deliberately terminal: the object stays where it is and an operator decides. It should be zero always; the `MediaUploadQuarantined` alert fires on it.

## Observability

| Signal | Kind | Watch for |
| --- | --- | --- |
| `wasla_media_outcomes_total{outcome}` | counter | `ready` or a reason token; the failure subset drives `MediaProcessingFailureSpike` |
| `wasla_media_stranded`, `…_oldest_age_seconds` | gauge (scrape) | above zero for 15 minutes: `MediaStranded` |
| `wasla_media_recovery_total{outcome}` | counter | `requeued`, `abandoned`, `release_failed` |
| `wasla_media_purge_deletes_owed`, `…_oldest_age_seconds` | gauge (scrape) | owed for six hours: `MediaPurgeDeletesFailing` |
| `wasla_media_purge_objects_total{outcome}` | counter | `failed` is a store refusing deletes |
| `wasla_provider_requests_total{operation="transcribe"}` | counter | `TranscriptionFailureRate` |
| `wasla_media_upload_reconciliation_total{outcome="quarantined"}` | counter | `MediaUploadQuarantined` |

Labels are closed vocabularies; none carries a workspace, file, key, URL or filename. Procedures for each alert are in [RUNBOOK.md](RUNBOOK.md).

## Storage capacity

A workspace cannot fill the object store without limit. `LimitKey.STORAGE_BYTES` caps the bytes it holds, summed over the attachments whose `storage_state` still names an object — so an intent that never became an object, and a file retention has purged, both stop costing space (ADR-091).

The check runs where the intent is committed, under the workspace's advisory lock, which makes the intent itself the reservation: two uploads racing for the last megabyte cannot both have it.

The two write paths answer differently, and the difference is the same one that runs through the rest of this system. An **inbound** attachment is recorded as `SKIPPED` with the reason on the row, exactly as an oversized file is — a customer's message is stored, projected and answerable whatever the business's plan says. An **authenticated upload** is refused with a 402 before Meta is asked to do anything, because that is a request somebody made and can act on.
