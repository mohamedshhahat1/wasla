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
Media worker: probe size → download → store → read
      ↓
Transcript on the row, status READY
      ↓
Nothing else unread on this conversation?
      ↓
Agent job enqueued
```

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
| PDF, plain text | Local extraction | The text layer |

Images travel as data URLs rather than links: the alternative is putting every customer's attachment behind a URL a provider can reach, which is far wider exposure than sending the bytes for one request. Image understanding reuses `ResponsesClient` through an `images` field on `Turn`, so it inherits the retry and timeout policy rather than growing a second copy of it.

Transcription forces no language. This product's customers switch between Arabic and English inside a single sentence, and pinning one makes the other come back as nonsense rather than as a translation.

The vision prompt asks for exact transcription of any text in the image — a price, a receipt, a serial number. A description that paraphrases a price list throws away the entire message.

## Statuses, and the two ways of giving up

`PENDING` → `DOWNLOADING` → `STORED` → `READY`, with two exits.

| Outcome | Meaning | Retried |
| --- | --- | --- |
| `SKIPPED` | Wasla decided not to process it | Never |
| `FAILED` | An attempt broke | Yes, to `MAX_ATTEMPTS` |

A file over the size cap, of a type nothing can read, a silent recording, or a scanned PDF with no text layer is **skipped** — the file was looked at and there was nothing to get. Retrying is a loop against a wall. A provider outage or a Meta timeout is **failed**, and worth another attempt. This is the same distinction follow-ups draw ([CRM.md](CRM.md)).

Both count as resolved: the customer is still owed an answer, and an agent that says it could not open the attachment is better than one that never speaks. Memory renders an unreadable file with its reason, so the agent can say what happened.

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

Object storage is **not** an authorization boundary. A key prefix is a layout; tenant isolation is the scoped repository, and the isolation tests run against both backends for exactly that reason.

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

**On a versioned bucket, deleted does not mean gone.** A delete leaves a delete marker and previous versions stay until the bucket's own lifecycle rule expires them. What retention guarantees is that the object is no longer retrievable through Wasla; making it unrecoverable is a rule configured on the bucket.

**No bucket-listing orphan sweep exists**, deliberately. Finding an object with no row means listing the store and deciding from age, and a sweep on that rule eventually deletes a live file whose row it failed to read. One narrow orphan is left open and stated rather than papered over: the download path writes the object and then commits the row, so a transaction failing in between leaves an object nothing references.

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

`ConversationMediaGate` takes a row lock on the conversation before the count, which makes the second worker wait for the first to commit. No new table and no Redis key — the lock is held for a single count.

## Sending

| Method | Path | Role |
| --- | --- | --- |
| POST | `/api/v1/conversations/{id}/messages/media` | any member |
| GET | `/api/v1/conversations/{id}/media/{media_id}` | any member |

Sending uploads the file to Meta and sends the returned id. A hosted link would need every attachment behind a publicly reachable URL for as long as Meta might fetch it; an upload exposes the bytes to one recipient for one send.

An attachment is a free-form message, so the **24-hour service window applies** exactly as it does to text ([ADR-012](../DECISIONS.md)). Outside it, only an approved template will do.

Meta groups attachments as `image`, `audio`, `video` or `document`, which are not the mime families — `application/pdf` is a *document*. Wasla's accepted list is narrower than Meta's: Meta will carry almost any file as a document, and a business forwarding an executable to a customer is not a feature anyone asked for.

Serving a file back goes through the application rather than from a public URL, because a customer's photograph is workspace data and a link needing no authentication is a link that leaves the workspace. `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff` are not decoration: a customer-supplied HTML file rendered inline on this origin is a script running against whoever is reading the inbox.

## Documents in the knowledge base

The PDF parser added here also settles a note `KnowledgeService` had carried since phase 6, which refused PDFs for want of one. A PDF is submitted base64-encoded and a **scanned** one — a photograph of a page, no text layer — is refused with a message saying so, rather than ingested empty. An empty document looks perfectly indexed from the outside and answers every question with nothing. See [RAG.md](RAG.md).

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_STORAGE_BACKEND` | `local` | `local` or `s3` |
| `MEDIA_STORAGE_PATH` | `/var/lib/wasla/media` | `local` only; shared volume between API and worker |
| `MEDIA_MAX_BYTES` | `26214400` | 25 MB; bites on video and little else |
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
| `OPENAI_VISION_MODEL` | `gpt-4.1-mini` | Separate budget from the answering model |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | |

Without an OpenAI key, documents are still read — extraction needs no provider — and images and voice notes are recorded as unreadable rather than crashing the worker.

## Known gaps

- **No OCR.** A scanned document is reported as unreadable rather than being read. Recorded honestly on the row, so nobody is left wondering.
- **Video is downloaded and stored but not understood.** There is no route from a video to a transcript; it is skipped as an unreadable type.
- **Nothing streams.** A file is read into memory whole, bounded by the download cap and by a smaller cap on the upload endpoint.
- **Stored files are never swept.** They accumulate for the life of the deployment; retention belongs with the object-store implementation.
