# Operational Runbook

**Status: Implemented** — written for whoever is holding the pager, including the version of you that wrote the code and has forgotten it.

Everything here has been executed against this system rather than imagined. Where something has *not* been verified in production — because there is no production yet — it says so.

Scope: what to do when something is wrong. Architecture is in [../ARCHITECTURE.md](../ARCHITECTURE.md); how to deploy is in [DEPLOYMENT.md](DEPLOYMENT.md).

---

## First: what is actually broken

Ask in this order. Each step is cheap and rules out a class of problem.

```
1. Is the API answering?        curl -fsS https://<host>/health/live
2. Are its dependencies up?     curl -fsS https://<host>/health/ready
3. Are the workers alive?       docker compose -f docker-compose.prod.yml ps
4. What do the logs say?        docker compose -f docker-compose.prod.yml logs --since=15m api worker
```

`/health/live` answers whether the process is running. It deliberately does **not** touch PostgreSQL or Redis, so a database outage does not make an orchestrator restart healthy containers. `/health/ready` does check them, and is the one to look at when the API is up but failing requests.

A **healthy** worker container means every loop it is configured to run has published a heartbeat in the last 90 seconds. It does **not** mean work is being processed — see [Queue not draining](#queue-not-draining).

---

## Symptoms

### The API is up but every request fails

Check `/health/ready`. It names each dependency and how long it took.

```json
{"status": "degraded", "components": [{"name": "postgresql", "status": "down", ...}]}
```

- **postgresql down** — the database is unreachable or refusing connections. Check the container, then connection limits: each API replica holds a pool, and a worker mid-inference holds a connection for the length of that call ([ARCHITECTURE.md §14](../ARCHITECTURE.md)).
- **redis down** — the API keeps serving. Rate limiting fails **open** (requests are allowed), refresh-token revocation cannot be checked, and no new background jobs can be enqueued. Inbound messages are still stored: the webhook logs `agent.enqueue_failed` and returns 200 so Meta does not retry. Each such event stays `received` in `whatsapp_events` with a bounded reason, and `InboundRecoveryWorker` finishes them once Redis is back — see *Inbound stored but never answered*. Nothing is lost and nothing needs a person, but check that the backlog actually drains.

### The migrate service refuses a release

Check the migrate service logs for the failing validation or migration revision.
`MIGRATION_DATABASE_URL` must be the schema owner, and API and worker
`DATABASE_URL` must name a separate, non-superuser runtime role. Migration
`0069` also needs `CREDENTIAL_ENCRYPTION_KEYS` and the separate, stable
`PAYMENT_TOKEN_FINGERPRINT_KEY` before it can protect existing saved-card
tokens. Correct the secret-store configuration, rerun the migrate service,
then start the new API and worker. A failed `0069` transaction leaves the
previous schema intact; do not copy card tokens into logs or manually rewrite
payment-method rows. See [DEPLOYMENT.md](DEPLOYMENT.md) for role provisioning,
key rotation, and rollback constraints.

### Customers' messages are not arriving

The webhook is the one path that must never be refused. Work backwards:

1. **Is Meta still delivering?** Check the Meta app dashboard for a disabled subscription. Meta disables a webhook that keeps failing — the most likely cause is a run of non-2xx responses.
2. **Signature failures?** `grep whatsapp.invalid_signature`. A rotated `META_APP_SECRET` that reached only some replicas looks exactly like this.
3. **Unknown number?** `grep whatsapp.unknown_phone_number_id`. The number is not connected to any workspace, or was disconnected. Not an error Meta can fix by retrying.
4. **Signature failures, counted?** `WhatsAppWebhookSignatureFailures` fires on a sustained rate. A drifted `META_APP_SECRET` drops every customer message with a 403 while the endpoint keeps answering, and enough of those makes Meta disable the subscription for every workspace on the deployment.
5. **Refused at the parser?** `wasla_inbound_entries_refused_total` counts entries the adapter could not turn into an event, by reason - see *Inbound entries are being refused*.
6. **Stored but unanswered?** Rows in `whatsapp_events` but silence from the agent means the queue, not the webhook. `python -m app.workers.queues unprocessed-inbound` lists them — see below.

Nothing rate-limits this path and nothing times it out ([ADR-032](../DECISIONS.md)). If you are considering adding either, read that record first.

### Inbound entries are being refused

**Alerts:** `InboundEntriesRefused`, `InboundForeignPayloads`. **Metric:** `wasla_inbound_entries_refused_total{channel, reason}`.

The webhook answered 200 - it always does once the signature is good - but the
adapter could not turn some entries into events. Before this counter existed
such a delivery looked like a successful inbound call with nothing in it, which
is how WhatsApp username senders were lost (OMNI-002). The `reason` says which:

| Reason | What it means | What to do |
| --- | --- | --- |
| `foreign_object` | The payload is another Meta product's (`object` is `page` or `instagram`, not `whatsapp_business_account`) | A webhook subscription for another product points at `/api/v1/webhooks/whatsapp`. Fix it in the App Dashboard; nothing was misread |
| `unsupported_field` | A WhatsApp change Wasla does not process: template updates, `user_id_update`, the Coexistence fields `history`, `smb_app_state_sync`, `smb_message_echoes` | Expected as a trickle if the app is subscribed to those fields; unsubscribe if they are not needed. Not alerted |
| `missing_sender` | A message with neither `from` nor `from_user_id` | Meta changed the sender shape again. Compare a raw delivery with docs/WHATSAPP.md |
| `identifier_too_long` | A sender identifier longer than any documented form (a phone past 32, a business-scoped id past 255) | Same: compare with Meta's current documentation |
| `missing_event_id`, `missing_status`, `missing_connection`, `malformed` | An entry without the ids Wasla needs | A malformed or truncated delivery; check the log line below |

Every webhook log line `whatsapp.webhook_received` carries the delivery's
`refused` counts beside `stored`, `duplicates`, `echoes` and `collisions`. A
refused entry is not stored, so there is nothing to replay - the fix is the
subscription or the parser, and Meta's own retries will not bring it back.

### Inbound stored but never answered

**Alert:** `UnprocessedInboundBacklog`. **Metric:** `wasla_unprocessed_inbound_events`.

A customer wrote in, the message was accepted and stored, and no worker was told to answer it. Almost always Redis was unavailable at the moment the webhook arrived: swallowing that failure is deliberate, because a non-2xx would make Meta retry the whole delivery and eventually disable the subscription ([ADR-102](../DECISIONS.md)).

**Nothing is lost.** The message is in the inbox and in `whatsapp_events` with `state = 'received'`, and `InboundRecoveryWorker` re-derives what is missing and supplies it. A brief spike during a Redis restart is expected; the alert's `for:` window covers it.

```
docker compose exec worker python -m app.workers.queues unprocessed-inbound
```

Each row shows its age, kind, workspace, the reason it is stuck, and the provider event id. No message bodies: the workspace and conversation are enough to find the conversation in the product, and customer text does not belong in a shell history.

If the backlog is **not** shrinking:

1. Is a worker running the `inbound_recovery` loop? Check `WORKER_KINDS` — it is in the default set, so an explicit list that omits it is the usual cause.
2. Can that worker reach Redis? The reason column will read `agent_enqueue_failed` or `media_enqueue_failed`, which is the sweeper trying and being refused.
3. Anything marked `failed` is out of the sweeper's hands and into yours. `projection_missing` should be unreachable — the event and its projection commit together — so it means something wrote an event row directly.

**Do not replay the webhook.** Recovery re-derives rather than replays for a reason: replaying would re-project the message, re-cancel its follow-ups and re-meter the delivery. Running the sweeper twice is safe; feeding Meta's payload back in is not.

### Customer attachments are stranded

**Alert:** `MediaStranded`. **Metric:** `wasla_media_stranded`, `wasla_media_stranded_oldest_age_seconds`.

**Symptom.** Attachments are unresolved (`pending`, `downloading` or `stored`) for longer than any attempt can take. A conversation is answered only once none of its attachments is unresolved, so each of these is a customer whose reply is being held - including for any later file they sent.

**Query.**

```sql
SELECT id, tenant_id, conversation_id, status, attempts, claimed_at, created_at
FROM message_media
WHERE status IN ('pending', 'downloading', 'stored')
ORDER BY coalesce(claimed_at, created_at)
LIMIT 50;
```

**Likely causes.** The `media_recovery` loop runs inside every worker that runs the `media` kind and finishes these within a minute: put back on the queue while attempts remain, given up on (`FAILED`, reason `abandoned`) after three, with the conversation released. A reading that persists means no worker runs the `media` kind (see `WorkerLoopNotBeating`), the sweep is failing (`media_recovery.sweep_failed` in the worker log), or Redis refuses the requeue (`media_recovery.requeue_failed`).

**Safe action.** Fix the worker or Redis; the sweep then drains the backlog on its own. Do not set rows to `ready` by hand - a file marked ready with no transcript is answered as if it had been read. A row whose inbound event is still `received` is inbound recovery's, not this sweep's (see "Inbound stored but never answered").

### Replies owed after attachments are not being taken up

**Alert:** `MediaReleaseOwed`. **Metric:** `wasla_media_release_owed`, `wasla_media_release_owed_oldest_age_seconds`, `wasla_media_recovery_total{outcome="release_failed"}`.

**Symptom.** Every attachment in a conversation reached a final state, the agent turn it was owed is recorded, and no agent worker has taken that turn up within the release horizon. The customer is waiting for a reply.

**Query.**

```sql
SELECT tenant_id, conversation_id, trigger_message_id, created_at, claim_expires_at AS last_published
FROM agent_turns
WHERE state = 'claimed' AND claimed_by = 'media-release'
ORDER BY created_at
LIMIT 50;
```

**Likely causes.** The media release records the turn in the transaction that finishes the last file, then queues the agent job; if Redis refuses that job, or the process dies first, the turn stays owed. The `media_recovery` loop republishes each owed turn once per release horizon (`media_recovery.releases_republished`, `outcome="release_recovered"`). A reading that persists means Redis is still refusing (`media_recovery.release_failed` in the worker log), no worker runs the `agent` kind (`WorkerLoopNotBeating`), or no worker runs the `media` kind to republish.

**Safe action.** Fix Redis or the workers; the sweep republishes on its own and the agent worker answers each turn once - a turn is keyed on its trigger message, so a republished job that races an original that was only slow is still one reply. Do not delete these rows or mark them `completed` by hand: either one silences a customer who is owed an answer.

### Customer attachments are failing

**Alert:** `MediaProcessingFailureSpike`. **Metric:** `wasla_media_outcomes_total{outcome}`.

**Symptom.** More than a quarter of attachments end `FAILED`. Customers are still answered - the agent is told the file could not be read - but it is not read. Decisions (oversize, unsupported type, unreadable document, a suspended workspace) are never counted as failures.

**Query.** `sum by (outcome) (rate(wasla_media_outcomes_total[30m]))`, and in the database `SELECT last_error, count(*) FROM message_media WHERE status = 'failed' AND processed_at > now() - interval '1 hour' GROUP BY 1`.

**Likely causes, by outcome.** `credential_refused` / `credential_unavailable` - a number's token is revoked or missing (see "WhatsApp is refusing this workspace's credential"). `host_refused` - Meta served a file from a host outside `META_MEDIA_HOST_ROOTS`; confirm the host is Meta's (the `whatsapp.media_host_refused` log names it) before adding it to the list. `download_failed` / `timeout` / `rate_limited` - Meta or the network. `provider_failed` - the vision or transcription provider (see `TranscriptionFailureRate`). `storage_failed` - the media store.

**Safe action.** Fix the cause. Failed files are final and are not retried by the queue (PD-MEDIA-08), so an outage costs the files that arrived during it; the customer can send them again.

### Voice notes are not being transcribed

**Alert:** `TranscriptionFailureRate`. **Metric:** `wasla_provider_requests_total{provider="openai", operation="transcribe"}`.

**Symptom.** More than half of transcriptions fail after their retries, so voice notes are answered as unreadable.

**Likely causes.** The OpenAI key or quota, a provider outage, or `OPENAI_TRANSCRIPTION_MODEL` naming a model the key cannot use. The worker logs `transcription.unreachable` (network) or `transcription.rejected` with the status.

**Safe action.** Fix the key, quota or model; nothing needs replaying.

### A purged workspace's files are still in the store

**Alert:** `MediaPurgeDeletesFailing`. **Metric:** `wasla_media_purge_deletes_owed`, `wasla_media_purge_deletes_oldest_age_seconds`, `wasla_media_purge_objects_total{outcome}`.

**Symptom.** A workspace purge recorded object deletes that the store has not confirmed for hours. The workspace's rows are gone, and its customers' files are not.

**Query.**

```sql
SELECT tenant_id, count(*), max(attempts), min(created_at), max(last_attempt_at)
FROM media_purge_objects GROUP BY tenant_id;
```

**Likely causes.** The media store refuses deletes - rotated or narrowed credentials (`MEDIA_S3_*`), a bucket policy, an outage. The purge worker logs `purge.object_delete_failed` for each refusal, without the key.

**Safe action.** Restore delete permission; the purge worker retries every five minutes while anything is owed and removes each row as its delete is confirmed. **Do not delete rows from `media_purge_objects` by hand**: a row is the only record of a file still in the bucket. A key in its in-flight grace (fifteen minutes after the purge, for an upload that was mid-write) is owed by design.

### An attachment object does not match what was written

**Alert:** `MediaUploadQuarantined`. **Metric:** `wasla_media_upload_reconciliation_total{outcome="quarantined"}`.

**Symptom.** Upload reconciliation found an object at a key Wasla owns whose size or hash is not what Wasla recorded, and quarantined it (`storage_state = 'mismatched'`): it is neither served nor deleted.

**Query.** `SELECT id, tenant_id, storage_key, byte_size, content_hash, upload_started_at FROM message_media WHERE storage_state = 'mismatched';`

**Likely causes.** Somebody with bucket write access replaced the object, or a store fault. Nothing in the application writes a different object to an owned key.

**Safe action.** Treat it as a possible bucket compromise: preserve the object, review bucket access logs, and rotate the media credentials if the change is unexplained. Only once explained, delete the object and set the row to `purged` by hand, recording why.

### An uploaded document is never searchable

**Alert:** `UnindexedDocumentBacklog`. **Metrics:** `wasla_pending_documents`, `wasla_oldest_pending_document_age_seconds`, `wasla_documents_retry_waiting`.

Somebody uploaded a document successfully, it has no active generation, and its indexing attempt has been outstanding for over half an hour in a workspace that is being served. Suspended workspaces are excluded on purpose: their indexing is paused until they are reactivated.

**Nothing is lost.** The row and its text are in `documents`, and the attempt is a `pending` or `processing` row in `document_index_generations`.

```
docker compose exec worker python -m app.workers.queues unindexed-documents
```

Each row shows the attempt's age, state, generation number, attempts so far, last error code, workspace and document id — never document text, titles or filenames. **The last error column tells the two causes apart:**

* **`-`, zero tries** — the job never ran. Almost always Redis was unavailable when the upload committed. Check that a worker runs both the `ingestion` and `ingestion_recovery` loops (`WorkerLoopNotBeating`) and can reach Redis. The recovery sweep re-queues these on its own.
* **A provider code** (`provider_unavailable`, `provider_rate_limited`, `provider_unreachable`, `dependency_unavailable`) — indexing is backing off after transient failures and will retry on its own, up to five attempts, then end as `retry_exhausted` in `failed-documents`. See `EmbeddingProviderUnavailable` below.
* **`embeddings_not_configured`** does not appear here as a code; if nothing is ever tried and the worker logs `knowledge.embeddings_not_configured`, `OPENAI_API_KEY` is unset on the worker. Nothing is spent and nothing is marked failed; setting the key drains the backlog.

Re-queueing is safe to repeat. A worker's claim admits one worker per attempt, so a duplicate job costs one short transaction and no embedding call.

### Documents failed to index

**Alert:** `RAGIngestionFailureRate`. **Metrics:** `wasla_documents_indexing_failed`, `wasla_documents_indexing_exhausted`, `wasla_rag_ingestion_outcomes_total{outcome}`.

```
docker compose exec worker python -m app.workers.queues failed-documents
```

A failed attempt is **never retried automatically**. Read the code:

* `provider_unauthorized`, `provider_forbidden` — the OpenAI key is wrong, revoked or not allowed embeddings. Fix the key first.
* `provider_model_not_found`, `provider_invalid_request` — `OPENAI_EMBEDDING_MODEL` names a model that does not exist or does not accept `dimensions` (for example `text-embedding-ada-002`). Fix the configuration first.
* `invalid_embedding`, `provider_response_too_large` — the provider, or a proxy in front of it, returned something that is not a valid embedding. Check egress proxies.
* `retry_exhausted` — a transient outage outlasted five attempts. Check that the provider is healthy now.
* `document_too_large`, `document_empty`, `invalid_document` — the upload itself. Not an incident; the workspace needs to split or fix the document.
* `internal_error` — a defect. Collect the `knowledge.ingestion_failed` log line (it carries the exception class, never its message) before retrying.

Then retry, one document at a time or through the workspace's own re-index action:

```
docker compose exec worker python -m app.workers.queues reindex-document <workspace-id> <document-id>
```

**A failed re-index takes nothing away.** If a document was already serving, `status` stays `ready` and its previous generation keeps answering; only the new attempt is `failed`. The failure is also in the workspace's audit trail as `knowledge_document_indexing_failed`.

### Embedding calls are failing or throttled

**Alerts:** `EmbeddingProviderUnavailable` (critical), `EmbeddingRateLimited`. **Metrics:** `wasla_provider_requests_total{provider="openai", operation=~"embed_.*"}`, `wasla_provider_attempts_total{…}`.

`embed_ingest` is document indexing; `embed_query` is a knowledge search inside an agent turn. Split by `outcome`:

* **`failure`** is a refusal — key, model, or an invalid response. Every document indexed now fails permanently with the codes above; searches return "could not be searched" to the model. Fix the configuration, then re-index the failed documents.
* **`unavailable`** is the provider or the network. Indexing backs off and retries by itself; searches fail and agents answer without the knowledge base (the customer still gets a reply). Nothing to replay.
* **`rate_limited`** — check the account's embedding limits and whether a bulk upload or a `reindex-stale-embeddings` run is under way; the latter is bounded by `--limit` and can be paused by not running the next batch.

The OpenAI alerts in the AI section (`OpenAIUnavailable` and its siblings) cover only `respond_*` operations, so a firing embedding alert with those quiet means the embeddings path specifically.

### Knowledge searches are failing

**Alert:** `RAGRetrievalFailureRate`. **Metrics:** `wasla_rag_retrievals_total{outcome}`, `wasla_rag_retrieval_duration_seconds`, `wasla_rag_retrieved_passages`.

Customers are still answered — a failed search becomes a failed tool call and the turn continues — but without the company's documents, so answers about products, prices and policies are worse or deferred to a colleague. Check `knowledge.search_failed` logs for `stage`:

* `embedding` — the provider; see the section above.
* `search` — the database: statement errors, a pgvector error, or an unhealthy connection. `reason` carries the exception class.

A high `empty` rate with a low `failed` rate is not this alert. It can mean workspaces have not uploaded what customers ask about, or — after a model change — that documents are served from another embedding space: check `wasla_documents_stale_embedding`.

### Documents are served from an old embedding model

**Metric:** `wasla_documents_stale_embedding`.

After `OPENAI_EMBEDDING_MODEL` changes, documents indexed with the previous model are **not searched** — a query from one model is not comparable with vectors from another, even at the same width — until they are re-indexed. Each keeps its old generation until the new one publishes.

```
docker compose exec worker python -m app.workers.queues stale-embeddings
docker compose exec worker python -m app.workers.queues reindex-stale-embeddings --limit 100 --dry-run
docker compose exec worker python -m app.workers.queues reindex-stale-embeddings --limit 100
```

Run it in batches and watch `EmbeddingRateLimited`. The command is coalesced per document, so running a batch twice queues nothing twice.

### An indexing claim was abandoned

**Alert:** `RAGDocumentsStuck`. **Metrics:** `wasla_documents_processing`, `wasla_oldest_processing_document_age_seconds`.

A worker claims an attempt and renews the claim before every embedding batch; a claim not renewed for ten minutes is reclaimed by the recovery sweep and spends one of the five attempts. Held for half an hour means the worker holding it died and the sweep is not running. Check `WorkerLoopNotBeating{kind="ingestion_recovery"}`, then `unindexed-documents`. Nothing needs to be released by hand: once recovery runs, the attempt is re-queued, and a late worker that comes back cannot publish over it.

### A worker loop has stopped

**Alert:** `WorkerLoopNotBeating`. **Metric:** `wasla_worker_heartbeat_alive{kind}`.

The named loop has not refreshed its liveness key inside its expiry. Workers serve no HTTP and are deliberately not a scrape target ([ADR-069](../DECISIONS.md)), so `ScrapeTargetDown` cannot see them — this is the only alert that can.

**Which kind it is decides how urgent this is.**

* `recovery` — the deployment has **no crash recovery at all**. Jobs a dead worker was holding stay in flight until this comes back. Treat as an incident.
* `inbound_recovery` / `ingestion_recovery` — work lost to a Redis outage is no longer being repaired. Nothing is lost yet; nothing is converging either.
* `agent` / `media` / `ingestion` — customers are waiting. `QueueJobsStuck` will follow within fifteen minutes.
* `billing` — collections, dunning and invoicing have stopped. Hours matter, minutes do not.
* `follow_up`, `campaign`, `email`, `retention`, `purge`, `uploads` — scheduled work is not happening.

What to check, in order:

1. `docker compose ps` — is the worker container running, or restarting? A crash loop is usually configuration: the container validates its settings at startup and refuses to boot rather than running half-configured.
2. `docker compose logs worker` — `worker.startup` names the kinds this process actually started. A kind missing there is a `WORKER_KINDS` problem, not a crash.
3. The container's own `HEALTHCHECK` (`entrypoint.sh worker-health`) asks the same question against Redis. An unhealthy container with a healthy API is exactly this alert.
4. If the process is up and one kind alone is silent, that loop is blocked rather than dead. Its work is still claimable — every queue consumer's reservation expires and every sweep's lease runs out — so restarting the container is safe.

### A send WhatsApp never confirmed

**Alert:** `UnresolvedOutboundSends`. **Metric:** `wasla_unresolved_outbound_messages`.

A message was committed, Meta was asked to deliver it, and no usable answer came back — a 5xx, a read timeout, a reset connection, or a `2xx` with no message id. **It may be on the customer's phone.**

```
docker compose exec worker python -m app.workers.queues unresolved-sends
```

**Do not send it again, and do not clear the row by hand.** This is the same rule as an unresolved payment and for the same reason: the send endpoint takes no idempotency key and offers no lookup keyed on anything Wasla holds before Meta answers, so nothing can ask Meta what became of the request ([ADR-093](../DECISIONS.md)). A retry is the one action that can put a second copy on somebody's phone, and it cannot be taken back.

What can settle one:

1. **Read the conversation.** If the customer replied to the message, it arrived. If the next thing in the thread is them asking again, it probably did not.
2. **Check Meta's own delivery reporting** for the number, if the workspace has access to it.
3. **Ask the workspace.** A colleague who was in the conversation usually knows.

Then, if the message genuinely did not arrive, **send a new one** through the product. Do not edit `delivery_state`: the row is the record that a send was attempted and its outcome was never established, and that record is what makes the next investigation possible.

A handful of rows seconds old is every send currently in flight and is not an incident — the alert fires on the *age* of the oldest, not on existence.

### WhatsApp is refusing this workspace's credential

**Log:** `whatsapp.credential_refused`. **Alert:** `WhatsAppSendFailureRate`.

Meta answered `401`, `403` or its own `code 190`: the number's access token is expired, revoked, or was never valid. Nothing was delivered.

This is deliberately not a per-message failure. A campaign stops on the first one rather than burning one attempt budget per recipient, and the follow-up sweep skips the rest of that workspace's nudges for the pass — so the symptom is a campaign that stopped early, not ten thousand individually-failed recipients.

Reconnect the number in the workspace's WhatsApp settings, which re-runs the ownership check and stores a fresh credential. Follow-ups resume on the next sweep by themselves; a stopped campaign has to be restarted, which is the right point to confirm the credential works.

### A connection's credential is being refused

**Alert:** `ChannelConnectionCredentialRefused`. **Metric:** `wasla_channel_connections{status="active", health="auth_failed"}`.

The provider refused a send on this connection as unauthorised (Meta's 401 or
code 190). The connection's `health` is `auth_failed` until a send succeeds on
it again, which clears it; nothing else moves it. Every reply, follow-up and
campaign on that connection fails the same way until the workspace re-verifies
the number with a working token (see the previous section). Find which:

```
docker compose exec api python -m scripts.omnichannel_invariants census
```

prints `connections_credential_refused`; the rows are
`SELECT tenant_id, id, channel, health_changed_at FROM channel_connections WHERE health = 'auth_failed'`.

### Queue not draining

A worker container reporting **healthy** proves its event loop is scheduling. It does not prove progress: a loop waiting on a query that never returns keeps beating.

```sql
-- Jobs the workers have taken but not finished
SELECT count(*) FROM whatsapp_events WHERE state = 'received';
```

```bash
# Every queue at once: pending, in-flight, waiting to retry, dead-lettered,
# and how long the oldest waiting job has been waiting.
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues status
```

```
queue         pending  inflight  delayed   dead    oldest
------------------------------------------------------------
agent               0         0        0      0         -
ingestion           0         0        0      0         -
media               0         0        0      0         -
```

The same numbers are on `/metrics` as `wasla_queue_pending_jobs`,
`wasla_queue_inflight_jobs`, `wasla_queue_delayed_jobs`,
`wasla_queue_dead_letter_jobs` and `wasla_queue_oldest_pending_age_seconds`
([OBSERVABILITY.md](OBSERVABILITY.md)), which is what an alert should be
watching so nobody has to run this to find out something is wrong.

- **pending growing, in-flight empty** — nothing is consuming. Check `WORKER_KINDS`: a typo fails at startup, but a *valid* subset silently runs only those loops. `wasla_worker_heartbeat_alive{kind="agent"}` settles it in one look.
- **delayed growing** — jobs are failing transiently and backing off. Not yet an incident; the failure category says why:
  ```bash
  docker compose -f docker-compose.prod.yml exec redis \
    redis-cli ZRANGE agent:jobs:delayed 0 4 WITHSCORES
  ```
  Paired with `wasla_job_failures_total{category=...}` and the provider counters, this is usually somebody else's outage.
- **in-flight non-empty and static** — jobs a worker reserved and has not finished. **This now recovers itself** (ADR-074): every reservation carries a lease, a living worker renews it every third of `QUEUE_VISIBILITY_TIMEOUT_SECONDS`, and the `recovery` loop reclaims anything whose lease has run out. Expect in-flight to be non-zero and *moving* on a busy queue; static and growing is the thing to look at.
  ```bash
  # What is stuck, and since when
  docker compose -f docker-compose.prod.yml exec redis \
    redis-cli HGETALL agent:jobs:reservations
  ```
  `wasla_queue_expired_reservations` is the same question as a metric. If it is above zero for more than a few minutes, either the `recovery` loop is not running anywhere (check `WORKER_KINDS`) or workers are dying faster than it reclaims.
- **jobs being recovered** — `wasla_jobs_total{outcome="recovered"}` climbing means something is killing workers. The work is not lost; find out what.
- **dead growing** — work that stopped being retried. Read the records before doing anything with them:
  ```bash
  docker compose -f docker-compose.prod.yml exec worker \
    python -m app.workers.queues dead-letters agent --limit 5
  ```
  Each record carries the job type, the workspace, the attempt count, the first and last attempt times and a failure *category* — never an exception string, and never message content. Find the rest in the logs by `worker.job_dead_lettered` and the `tenant_id`.

### A worker died mid-send: uncertain deliveries

`wasla_jobs_total{outcome="quarantined"}`, or `worker.job_dead_lettered` with
`category=uncertain_delivery`. It means a worker stopped while an agent turn
was talking to Meta, so the customer may or may not have received a reply and
**nothing will send it again automatically**. That refusal is the design
(ADR-074): a second answer to a question that already has one is worse than a
late one.

```bash
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues dead-letters agent --limit 10
```

The record carries the workspace and the conversation. Open the conversation:

- **The customer was answered** — nothing to do. Note it and move on.
- **The customer was not answered** — decide whether an answer is still useful.
  A question from four hours ago may be better answered by a person than by
  replaying the turn. If replaying is right, `replay agent --limit 1 --force`,
  and read the next section first.

There is no way to tell these apart from outside the conversation, which is why
this is a runbook step and not a sweep.

### Replaying dead-lettered work

Only after reading a record and knowing why it failed.

```bash
# Idempotent queues: re-ingesting replaces a document's chunks, and a file
# already read is not read again, so a replay costs a round trip.
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay ingestion --limit 20
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay media --limit 20
```

**Start with a dry run.** It changes nothing, needs no `--force` even on the
agent queue, and prints one line per record — age, category, attempts,
workspace, job id — so the decision is made after looking rather than before.

```bash
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay agent --limit 20 --dry-run
```

The agent queue is **refused** without `--force`, because an agent turn ends in
a WhatsApp message: replaying a job whose failure came after the provider was
engaged sends a second answer to a question that already has one. Read the
conversation first, decide whether answering it again is right, and then:

```bash
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay agent --limit 20 --force
```

**An `uncertain_delivery` record is not replayed by that, and that is the
point.** `--force` says *this queue* may be replayed; it does not say *this
record* is safe, and the two are different questions. An `uncertain_delivery`
job engaged a provider and then stopped, so the customer may already have the
reply — the exact harm the engagement barrier exists to prevent, and exactly the
records a provider outage leaves mixed in with ordinary ones. They are listed as
`PROTECTED` and left alone, whatever `--force` says.

If you have read the conversation and know the reply never arrived, replay that
one record and only that one:

```bash
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay agent --force --include-uncertain --job-id <job-id>
```

`--job-id` on its own is worth using for any careful recovery: it turns a
decision about one conversation into an action on one conversation.

Replayed jobs go back as fresh first attempts, and the dead-letter records are
kept — so if the replay fails too, comparing the new record with the old one is
what tells you whether anything changed.

The logical-turn claim is a second line under all of this: a replayed agent job
whose turn already reached `engaged` or `completed` is consumed and does nothing,
so even a mistaken replay cannot re-run the inference or re-send the reply
(WQ-01). Do not treat that as permission to replay uncertain records casually —
the claim protects a turn that ran, and the operator's judgement is still what
decides whether one *should* run again.

### Nobody can log in

- **429 on `/auth/login`** — the authentication limiter, ten attempts per minute per client address. Behind a proxy that does not set `X-Forwarded-For`, *every* user shares one identity and ten attempts total. Check that nginx is forwarding it.
- **401 for everyone, suddenly** — `JWT_SECRET` changed. Every issued token is now invalid. There is no recovery except issuing new ones: users log in again.
- **401 for one user** — their account was disabled, or their refresh token was rotated and the old one replayed. Access tokens are not revocable by design ([AUTH.md](AUTH.md)).

### A workspace says it is being refused

**402 `plan_limit_exceeded`** is a plan limit, not a bug. What they hit and where they stand:

```
GET /api/v1/billing/entitlements     # every limit, used and remaining
GET /api/v1/billing/channel-capacity # channel slots, active connections, any reduction
GET /api/v1/usage                    # the meters behind the period limits
```

Resource limits (agents, colleagues, documents) count rows that exist now; period limits (messages, AI turns, campaign messages) count the current usage cycle.

**409 `channel_capacity_exceeded`** and **409 `channel_type_not_allowed`** are the channel entitlements (ADR-131), refused by the capacity guard before Wasla calls any provider. The first means every slot is taken - by an active connection of any channel; the second that the plan in force does not include that channel type, whatever slots are free. The details carry the figures (`effective_limit`, `active`, `typed_capacity`, `allowed_channel_types`). A disabled or released connection holds no slot; a company in a capacity reduction's grace, or not served (suspended, cancelled, expired), cannot connect or re-enable anything until it fits. See *Entitlements and channel capacity (0092-0098)*. Nothing on the *inbound* path is ever refused for a limit ([ADR-030](../DECISIONS.md)), so a workspace over its message allowance still receives its customers' messages — it is charged for the overage rather than cut off.

**403** is a role problem, not a plan problem. **429** is the rate limiter.

### The agent has stopped replying

Every turn the worker completes records how it ended, so start there rather than
with the logs:

```sql
SELECT t.outcome, t.state, t.engaged_at, t.completed_at, t.provider_response_id,
       c.mode, c.status, c.handoff_reason
  FROM agent_turns t
  JOIN conversations c ON c.id = t.conversation_id AND c.tenant_id = t.tenant_id
 WHERE t.conversation_id = '<conversation id>'
 ORDER BY t.created_at DESC
 LIMIT 5;
```

| `outcome` | What happened | What to do |
| --- | --- | --- |
| `replied` | A reply was sent | Check delivery: see *A send WhatsApp never confirmed* |
| `handed_off` / `escalated` | The agent's handoff tool, or the sentiment classifier, gave it to a person | Nothing — `handoff_reason` says why |
| `quota_blocked` | The workspace's one AI allowance is spent this usage cycle, counting the turns generating right now (their holds) on every channel (`handoff_reason` starts `AI_QUOTA_EXHAUSTED`) | Commercial: an upgrade, a top-up or a platform grant, or the next usage cycle. A failed generation never spent a turn (ADR-131) |
| `empty_response` | The provider answered with no words; the customer was told a colleague will follow up and the conversation was handed over (`AI_EMPTY_RESPONSE`) | If it recurs, `AgentEmptyResponses` fires — check the model and the provider |
| `suppressed_workspace` | The workspace is suspended or deleted | Intended. A deleted workspace is not served during retention |
| `suppressed_agent` | No active default agent, or it was disabled mid-turn | Activate an agent |
| `suppressed_closed` | The conversation was closed, before or during the turn | Intended: an old turn never reopens a closed conversation |
| `suppressed_human` | A person owns the conversation | Nothing |
| `suppressed_channel` | The connection is disabled, released or paused. `disabled_reason` says who disabled it: `manual`, `capacity_reduction` (an owner kept others) or `capacity_reduction_automatic` (a grace ended) | Re-enable it when there is a free slot, or reconnect |
| `channel_not_in_plan` | The plan in force does not include this channel - usually a suspended, cancelled or expired workspace on the default plan (ENT-16). Never charged | Commercial: pay the overdue invoice, or a plan that includes the channel |
| *no row, or `state = 'engaged'` with no `completed_at`* | See *An agent turn engaged and never finished* | |

If there is no turn at all, the job never reached the worker: `grep agent.enqueue_failed`, and see *Inbound stored but never answered*.

### An agent turn engaged and never finished

**Alert:** `AgentTurnsStranded`. **Metric:** `wasla_agent_turns_engaged_unfinished`.

A turn becomes `engaged` in the same transaction that holds a unit of the
workspace's AI allowance, immediately before the provider is called. If anything
then fails — the provider past its retries (`OpenAIUnavailable` will usually be
firing too), an unexpected exception, a worker killed mid-turn — the turn stays
`engaged` for ever. That is deliberate: the reply may already be on the
customer's phone, so nothing retries it, and the job is dead-lettered rather
than replayed.

Its `charge_state` says how far it got (ADR-131). `held`: it died before its
outcome was settled, and the reply is sent only after the settle commits, so
nothing went to the customer; the hold stops counting after
`AI_TURN_HOLD_TTL_SECONDS` and the billing sweep releases it (`hold_expired`) -
the customer is not charged. `released`: the provider failed and the hold was
given back. `charged`: the provider answered and the turn was charged; a reply
may be on the customer's phone.

```sql
SELECT t.tenant_id, t.conversation_id, t.trigger_message_id, t.engaged_at, t.charge_state
  FROM agent_turns t
 WHERE t.state = 'engaged'
   AND t.engaged_at < now() - interval '15 minutes'
 ORDER BY t.engaged_at;
```

For each, read the conversation. If an outbound message follows the trigger
message, the customer was answered and only the bookkeeping is stranded. If not,
answer the customer by hand. **Do not replay the dead-lettered agent job** — see
*Replaying dead-lettered work* for why agent replays require `--force`.

### Agent tools are failing or being denied

**Alerts:** `AgentToolFailures`, `AgentToolDenials`.
**Metric:** `wasla_agent_tool_executions_total{tool,outcome}`.
**Table:** `tool_executions`.

Every tool call an agent is asked to make writes one row — the ones that ran,
the ones refused before they ran, and the ones suppressed as duplicates — so
"did this run, and did it have an effect" is a query rather than an
investigation (TOOL-12).

Start with which tool and which reason:

```sql
SELECT tool_name, state, reason_code, count(*)
  FROM tool_executions
 WHERE requested_at > now() - interval '1 hour'
 GROUP BY 1, 2, 3
 ORDER BY 4 DESC;
```

**`failed`** means a handler raised: its work was rolled back to the call's own
savepoint, the model was told the tool did not work, and the turn went on to
reply or hand over. The exception class is in the `agent.tool_crashed` log line;
the values it was given are deliberately not, anywhere (TOOL-10).

**`denied`** (`not_granted`, `tool_disabled`) means the agent holds no enabled
grant. A handful is ordinary — a model reaching for something it was never
offered is refused safely. A sustained share means either a capability was
revoked while agents are still configured to use it, or a conversation is
talking a model into naming tools it does not have. `audit_logs` now answers the
first: look for `agent_tool_granted` / `agent_tool_revoked` and who made the
change (TOOL-13). For the second, read the conversation.

**Lifecycle reasons** (`workspace_suspended`, `workspace_deleted`,
`agent_disabled`, `conversation_human`, `conversation_closed`) are the system
working: the world changed while a model was composing and the tool was refused
rather than run. No action beyond confirming the change was intended.

**Bounded reasons** (`response_call_limit`, `turn_call_limit`, `round_limit`)
mean a model asked for more than one turn may spend, or asked on the final round
for something whose answer no round could read. Bounded and safe; a sustained
rate is a prompt to look at.

To see one turn end to end:

```sql
SELECT round_number, call_ordinal, tool_name, state, reason_code, argument_fields
  FROM tool_executions
 WHERE agent_turn_id = :turn
 ORDER BY round_number, call_ordinal;
```

### Campaigns are not sending

```sql
SELECT status, next_send_at, last_error FROM campaigns WHERE id = '<id>';
```

- `scheduled` with a future `scheduled_at` — waiting, correctly.
- `running` with `next_send_at` in the future — the rate limit pacing it ([ADR-026](../DECISIONS.md)). Expected.
- `failed` with `last_error` — the template was withdrawn or the number was disabled. Fix the cause, then schedule again.
- Recipients stuck `pending` while the campaign is `running` — check the campaign worker is in `WORKER_KINDS`.

---

### A customer paid and nothing happened

The commonest real payment incident, and the order below is the order that
distinguishes causes.

**Did the callback arrive at all?** This is the usual answer. The callback goes
to `APP_PUBLIC_URL` + `/api/v1/webhooks/paymob`, which must be reachable from
the internet and must *not* sit behind the proxy's auth or IP allowlist.

```sql
SELECT provider, provider_event_id, event_type, outcome, detail, received_at
FROM payment_events ORDER BY received_at DESC LIMIT 20;
```

Nothing recent means nothing is reaching the endpoint. Check the Paymob
dashboard's transaction for the callback attempt and its response.

**Was it rejected?** A verification failure logs `billing.callback_rejected`.
That means the deployment's `PAYMOB_HMAC_SECRET` does not match the one Paymob
is signing with — usually test credentials against a live account or the
reverse. It never means "retry with the check off".

**Was it applied to nothing?** `outcome` tells you which refusal fired:

| `outcome` | Meaning |
| --- | --- |
| `applied` | Believed, and something changed. If the invoice is still open, the payment failed rather than the plumbing |
| `duplicate` | A retry of an event already handled. Correct, and not a problem |
| `unmatched` | Verified but naming a payment this system did not issue, or one belonging to another workspace |
| `mismatched` | The reported amount or currency disagreed with the invoice. Investigate before touching anything |
| `no_change` | Believed, and said nothing new — a second notification of a state already recorded |
| `refused` | Believed, and asked for a move the rules forbid: a success reported for a refunded payment, or a second settlement of a paid invoice. **Always worth reading.** Either a customer paid twice, or callbacks are arriving out of order |

`detail` says why in a sentence. It is written by this application and never
copied from the provider's payload, so it is safe to paste into a ticket.

A row with `processed_at` NULL was claimed and never decided — the process died
between the two. The event is recorded and the payment was not applied; the
provider's retry will be a `duplicate`, so the money needs recovering by hand
from the transaction in their dashboard.

**Where does the payment stand?**

```sql
SELECT p.status, p.amount, p.currency, p.provider_reference,
       p.provider_intent_reference, p.failure_reason, i.status AS invoice_status
FROM payments p JOIN invoices i ON i.id = p.invoice_id
WHERE p.tenant_id = '<tenant-id>' ORDER BY p.created_at DESC LIMIT 10;
```

`provider_intent_reference` is the Paymob intention id and is what to search
their dashboard by when the customer abandoned the page.
`provider_reference` is the transaction that settled it.

**What you must not do:** do not mark an invoice paid by hand to make a
customer's problem go away. `invoices.status` is the record of whether money
arrived, and writing it from a SQL prompt records a payment that did not
happen. If money really did arrive and the callback never will, record it as a
payment through the platform billing API so there is a row saying who decided
that and when.

## Procedures

### Deploy a specific version

Every published image is tagged `sha-<commit>` and addressable by digest. Deployment pins the digest, never `latest`.

```bash
export WASLA_IMAGE=ghcr.io/mohamedshhahat1/wasla@sha256:<digest>
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml up -d --wait
```

`--wait` blocks until health checks pass, so a container that starts and dies fails the deploy rather than being reported as shipped.

### Roll back

```bash
export WASLA_IMAGE=ghcr.io/mohamedshhahat1/wasla@sha256:<previous digest>
docker compose -f docker-compose.prod.yml up -d --wait
```

**Do not run `migrate` when rolling back.** Rolling *back* a migration is a separate, deliberate decision: `alembic downgrade` on a schema the previous version wrote to can drop columns holding live data. If the new version's migration is the problem, read the migration first and decide explicitly.

Every migration in this project has been verified to downgrade and reapply cleanly on an empty database. That is not the same as being safe to downgrade over production data — `0020` drops the encrypted credential column, and with it every workspace's stored token.

### Find out what is running

```bash
docker inspect <container> --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Every published image carries its commit, version and build time as OCI labels.

### Check the workers are actually running

`docker compose ps` showing green services is not the same as every worker
running, and the difference is easy to misread: a service that never started has
no row at all, so two healthy containers beside a missing one looks like two
healthy containers.

```bash
docker compose ps                       # is there a worker row at all?
docker compose exec worker scripts/entrypoint.sh worker-health
docker compose logs worker | grep worker.startup
```

`worker-health` is the container's own `HEALTHCHECK`: it asks Redis whether
every loop this container is configured to run has beaten recently, and exits
non-zero if any has not. `worker.startup` names the kinds the process actually
started, which is what catches a `WORKER_KINDS` that no longer selects a loop -
a different failure from a crash, and one the logs are the only place to see.

**A worker that refuses to boot is usually configuration.** `Settings` validates
at startup and the container exits rather than running half-configured, so the
first line of `docker compose logs worker` names the variable. The commonest one
locally is `JWT_SECRET`, which must be at least 32 random characters:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

In a deployment, `WorkerLoopNotBeating` is what tells you this without anyone
having to look — see the symptom section above. Locally there is no alerting, so
this is the check.

### Rotate a secret

| Secret | Effect | Procedure |
| --- | --- | --- |
| `JWT_SECRET` | Every session ends immediately | Change, restart the API. Users log in again. No staged rotation is possible — the tokens carry no key id |
| `CREDENTIAL_ENCRYPTION_KEYS` | None, if done correctly | **Prepend** the new key, keep the old: `NEW,OLD`. New credentials use the new key, old ones keep decrypting. Removing the old key before rewriting the rows makes those credentials unreadable ([ADR-034](../DECISIONS.md)) |
| `META_APP_SECRET` | Webhook signatures fail until every replica has it | Change everywhere, then restart. A partial rollout looks like an attack in the logs |
| `POSTGRES_PASSWORD` | Everything stops | Change in the database and the environment in the same maintenance window |

Nothing automatically rewrites credentials onto a new encryption key. `CredentialCipher.needs_rotation` identifies the stragglers; rewriting them is manual.

### Add or remove worker capacity

`WORKER_KINDS` selects which loops a container runs — empty means all of them (`media`, `agent`, `ingestion`, `follow_up`, `campaign`, `billing`, `email`, `recovery`, `retention`, `uploads`).

```yaml
worker-campaign:
  image: ${WASLA_IMAGE}
  command: ["worker"]
  environment:
    WORKER_KINDS: campaign
```

Splitting them apart is an environment variable, not another image. `campaign` most often wants its own replica: a broadcast is bandwidth against Meta rather than inference.

**A kind not covered by any running container is a queue that silently grows.** The health check only asserts the loops *that container* was told to run.

### Take a workspace offline

There is no suspension API — that is deliberate ([app/api/v1/platform.py](../app/api/v1/platform.py)): the product has no answer yet for what happens to a suspended workspace's in-flight conversations. What exists today:

```
POST /api/v1/whatsapp/accounts/{id}/disable
```

That stops inbound and outbound traffic for one number and is audit-logged. Its conversations and data remain.

---

### Email is not being delivered

Work down this list; the first three cost nothing to check.

1. **Is email even on?** `EMAIL_ENABLED=false` makes every enqueue a silent
   no-op — password reset answers 202 and does nothing. This is the most
   common cause and does not look like a fault anywhere.
2. **Is the email worker running?** It is one kind among the set; a
   `WORKER_KINDS` that excludes `email` queues rows nobody drains.
3. **What does the queue look like?**

```sql
SELECT status, count(*), min(created_at) AS oldest
FROM email_messages
GROUP BY status ORDER BY status;
```

`pending` growing with `sent` flat means nothing is draining — worker down,
or the provider refusing everything. `sending` rows older than ten minutes
mean no worker is alive to recover them; they return to `pending`
automatically once one is.

4. **Why did the failures fail?**

```sql
SELECT last_error_code, count(*)
FROM email_messages
WHERE status = 'failed' AND failed_at > now() - interval '1 day'
GROUP BY last_error_code ORDER BY 2 DESC;
```

`suppressed` means the address bounced or complained earlier — the row is
doing its job. `render_error` is a bug: a template got a context it could not
render. An `http_4xx` or a provider error name usually means the sending
domain is not verified, or `EMAIL_FROM` is not on it.

**Retrying a failed row is deliberately not an API operation**, and there is
no admin endpoint for it. It would be a way to make the platform re-send mail
on demand, and the usual reason a row failed is that the address does not
work. If a batch failed for a cause since fixed, re-queue them deliberately
in SQL with a fresh idempotency key, and know how many you are about to send.

### An address has stopped receiving mail

It is probably suppressed. Suppression is written by a hard bounce or a
complaint and is never undone automatically.

```sql
SELECT recipient, reason, created_at FROM email_suppressions
WHERE recipient = lower('person@example.com');
```

Removing a suppression is a deliberate act, taken only when the mailbox is
known to work again — a fixed typo, a mailbox restored. Re-sending into a
hard bounce is how a sending reputation dies.

```sql
DELETE FROM email_suppressions WHERE recipient = lower('person@example.com');
```

Suppression is **global, not per-workspace**, because a dead mailbox is dead
for everyone. It never disables an account, never bumps `token_version` and
never denies a sign-in (ADR-042) — so a suppressed address is a delivery
problem, never an access one.

### Somebody cannot verify their email address

Work down this list; each step distinguishes a different cause.

**Is email on at all?** With `EMAIL_ENABLED=false` the endpoint answers `202`
and queues nothing, so the person waits for mail that was never sent. This is
the first thing to check and the most common cause on a new deployment.

**Is the address suppressed?** A hard bounce or complaint stops every send to
it, verification included. See *An address has stopped receiving mail* above.

**Is there a live challenge, and what happened to it?**

```sql
SELECT id, expires_at, attempts, consumed_at, superseded_at, created_at
FROM email_verification_challenges
WHERE user_id = '<user-id>'
ORDER BY created_at DESC LIMIT 5;
```

`consumed_at` set means it already worked - the person is probably looking at a
stale tab. `superseded_at` set means they asked again and are typing the older
code. `attempts` at the ceiling means the challenge is dead even for the right
code, and the fix is to ask for a new one. An `expires_at` in the past is the
same fix.

**Did the mail actually go?**

```sql
SELECT status, attempts, last_error_code, sent_at, provider_message_id
FROM email_messages
WHERE idempotency_key = 'email-verification:<challenge-id>';
```

**Why were their attempts rejected?** The trail carries a category, and this is
the query worth knowing:

```sql
SELECT occurred_at, metadata->>'reason' AS reason
FROM audit_logs
WHERE target_id = '<user-id>' AND action = 'email_verification_failed'
ORDER BY occurred_at DESC LIMIT 20;
```

`wrong_code` repeatedly against one account is the one to escalate - that is
what guessing looks like. `rate_limited` means they hit the per-account budget;
it clears on its own within the window and there is nothing to unlock, because
the limit refuses for a window rather than disabling anything.
`address_changed` means the code was issued for an address the account no
longer has.

**What you must not do:** there is no way to read a code, and no endpoint or
query that will give you one - only an Argon2 verifier is stored. Do not
"verify them manually" by writing `users.email_verified_at` from a SQL prompt.
That records a proof that never happened, in the one column whose entire value
is that it is only ever set by a proof. Have them request a new code.

### Rotating the Resend credentials

**The API key** is used only by the worker. Issue a new key in the Resend
dashboard, deploy it to the worker, confirm `email.sent` events resume, then
revoke the old one. In-flight sends fail transiently and retry, so nothing is
lost.

**The webhook secret** is used only by the API. Resend's signature header
carries a space-separated list and verification accepts any entry that
matches, which is what makes rotation possible without dropping deliveries —
but this application reads a single `RESEND_WEBHOOK_SECRET`, so the change is
still a deploy. Expect `email.webhook_invalid_signature` between the secret
changing at Resend and the deploy landing; bounces in that window are lost,
not queued, so rotate at a quiet time.

### The provider is down

Nothing needs doing. Transient failures back off exponentially with jitter to
a one-hour ceiling and keep trying up to `EMAIL_MAX_ATTEMPTS` (default 8),
which spans roughly a day. Domain actions are unaffected — an invitation
still commits, its delivery is just late. Watch for rows reaching `failed`
with `exhausted: true`, which is the point at which the outage became data
loss.

### Backups have stopped

`wasla_backup_age_seconds` above 36 hours, or absent entirely.

```bash
# What the last run actually did
cat /var/backups/wasla/status.json

# Whether anything even tried
systemctl status wasla-backup.timer
systemctl is-failed wasla-backup.service
journalctl -u wasla-backup.service -n 100
```

Read `failed_stage` first — it is the whole diagnosis:

- **`dump`** — PostgreSQL refused or was unreachable. The database is the
  problem, not the backup.
- **`validate`** — `pg_dump` produced something `pg_restore --list` cannot
  read. Disk full is the usual cause; the artifact was deleted rather than
  kept, deliberately.
- **`upload`** — **the dangerous one.** The database is fine and the dumps are
  fine and none of them is leaving the host, so the deployment has been losing
  its recovery point silently. Check the object store's reachability and the
  credentials in `/etc/wasla/backup.env`. The local artifacts are still in
  `BACKUP_DIR`; copy one off by hand now, then fix the cause.
- **`retention`** — harmless to the backup that just succeeded; the pruning
  failed after the upload did not.

**`last_success_at` never moves for a failed run.** If it says two days ago,
the deployment's recovery point is two days ago whatever the newest file in
`BACKUP_DIR` says.

### Recovering from an off-host backup

The full procedure, with the guards it goes through, is in
[BACKUP.md](BACKUP.md). The short form:

```bash
sh scripts/fetch_backup.sh /var/tmp/recovery          # newest artifact
sh scripts/restore_postgres.sh /var/tmp/recovery/<artifact> wasla_recovered --clean
```

The restore refuses to touch the database `DATABASE_URL` names unless
`WASLA_RESTORE_ALLOW_PRODUCTION=yes` is set. That is deliberate: recovering
into a scratch database and then switching to it is one decision at a time,
and restoring over a live database is the one thing that cannot be undone.

**Redis is not restored, and after a recovery it is empty.** Queued work is
gone. The messages themselves are `messages` rows in PostgreSQL with no reply,
and re-driving them means enqueueing agent jobs for the affected
conversations — which nothing does automatically, and should not: answering a
day-old question may be worse than silence.

### Recovering media

**The database restore does not bring the files back, and is not supposed to**
([BACKUP.md](BACKUP.md)). It restores the `message_media` rows — the transcript,
the type, the size, the storage key — and the bytes those keys name are the
object store's to keep.

On `MEDIA_STORAGE_BACKEND=s3` there is nothing to do: point the replacement at
the same bucket and the keys resolve. Check first that the bucket's lifecycle
rule has not already expired objects older than the backup you restored, because
that combination gives a product that works and files that are missing, and
nothing here compares the two windows for you.

On `local` there is no second copy. Whatever backs up the host is the recovery
point for attachments; if nothing does, they are gone.

**A restore with missing objects still serves.** Rows whose keys resolve to
nothing report a storage error for that file rather than failing the
application, so the inbox works while the store is being brought back.

To check the store from a replacement container before switching traffic to it,
store and read back one object under a scratch tenant id:

```bash
docker compose -f docker-compose.prod.yml exec api python - <<'PY'
import asyncio, uuid
from app.core.config import get_settings
from app.core.storage import build_media_storage

async def main() -> None:
    store = build_media_storage(get_settings())
    key = await store.put(tenant_id=uuid.uuid4(), data=b"%PDF-1.7 probe", mime_type="application/pdf")
    assert await store.get(key) == b"%PDF-1.7 probe"
    await store.delete(key)
    print("media store reachable, readable and writable")

asyncio.run(main())
PY
```

### Suspend a workspace, and restore it

Both are API operations and neither needs the database.

```
POST /api/v1/platform/tenants/{tenant_id}/suspend   {"reason": "…"}
POST /api/v1/platform/tenants/{tenant_id}/restore
```

Find the id from `GET /api/v1/platform/tenants` (search by name or address).
The reason is free text, is recorded verbatim in the audit entry, and is read by
colleagues — write the ticket number.

**What suspension does:** every workspace-scoped route refuses on the next
request. **What it does not do:** revoke anybody's session (they stay signed in
and keep their *other* workspaces), touch memberships, touch data, or touch
billing. The subscription keeps running — if the intent is to stop charging,
cancel the subscription as a separate, deliberate act. See
[BILLING.md](BILLING.md).

Restoration gives back exactly what suspension took. It does not readmit
somebody an administrator had removed, and does not re-enable an account the
platform had disabled. If a customer says they still cannot get in after a
restore, that is why — check `memberships.status` and `users.is_active` before
assuming the restore failed.

### A workspace was deleted and should not have been

There is **no undelete API**, in either direction. Deletion is the customer's
decision, and a button that let staff reopen a business relationship the
customer ended would be worse than the inconvenience of this procedure.

Confirm first, from the audit trail, who did it and when:

```sql
SELECT occurred_at, actor_label, target_label, metadata
FROM audit_logs
WHERE action = 'workspace_deleted' AND target_id = :tenant_id;
```

Reversing it needs two writes, and the second is the one people forget —
deletion revokes every membership, so clearing `deleted_at` alone produces a
workspace nobody can enter:

```sql
BEGIN;
UPDATE tenants SET deleted_at = NULL WHERE id = :tenant_id;
-- Only the memberships this deletion revoked. `revoked_at` is stamped with the
-- deletion's own timestamp, so it identifies them without touching anybody
-- removed for an unrelated reason beforehand.
UPDATE memberships
SET status = 'active', revoked_at = NULL, revoked_by_id = NULL
WHERE tenant_id = :tenant_id AND revoked_at = :deleted_at;
COMMIT;
```

Take `:deleted_at` from `tenants.deleted_at` **before** clearing it. Check the
roster afterwards and confirm there is at least one active owner; if the last
owner also closed their account, promote somebody by hand
(`UPDATE memberships SET role = 'tenant_owner' …`) rather than leaving a
workspace nobody can administer.

### Erasing a deleted workspace's data

**Automated.** The `purge` worker erases a deleted workspace's operational data
once `WORKSPACE_DELETION_RETENTION_DAYS` has passed, and records `purged_at` so
it happens once. Nothing here is normally an operator's job.

What you may need to do:

**Check what is pending.**

```sql
SELECT slug, deleted_at, purge_due_at, purged_at
FROM tenants
WHERE deleted_at IS NOT NULL
ORDER BY purge_due_at;
```

`purge_due_at IS NULL` on a deleted workspace means it predates migration 0050
and the backfill missed it — give it a deadline rather than leaving it retained
for ever by accident.

**Confirm the worker is running.** `WORKER_KINDS` must include `purge`, or
nothing erases anything and the documentation is making a promise the deployment
does not keep. The `WorkspacePurgeFailing` alert covers the sweep erroring; it
cannot cover a sweep that was never started.

**Bring a purge forward** (a customer asking for erasure sooner) by moving the
deadline, not by deleting rows by hand:

```sql
UPDATE tenants SET purge_due_at = now() WHERE id = :tenant_id AND deleted_at IS NOT NULL;
```

**What the purge deliberately does not touch**: `invoices`, `payments`,
`payment_events`, `subscriptions` and `audit_logs`. Those are the accounting and
evidentiary record and outlive the workspace on purpose. If an erasure request
genuinely reaches them, that is a legal decision and not a runbook one — and
deleting the `tenants` row itself would cascade through twenty-eight tables
including all of them. Do not.

Provider-side records (Paymob's own transaction history) are not reachable from
here and need their own request.

**When is a workspace's media really gone?** The purge deletes the media rows
and, in the same commit, records every storage key they held in
`media_purge_objects`; the worker then deletes the objects and removes each row
only once the store confirms. A workspace is fully erased when `purged_at` is
set **and** `SELECT count(*) FROM media_purge_objects WHERE tenant_id = :id` is
zero. Anything still owed is covered under "A purged workspace's files are
still in the store" above.

Objects orphaned by purges that ran before migration 0066 have no row naming
them. If an erasure must be proven for such a workspace, delete its prefix
(`{tenant_id}/`) in the bucket directly - keys are tenant-prefixed, so the
prefix is exactly that workspace's files - and record the action.

### A workspace was suspended and nobody suspended it

Look for the reason on the audit entry:

```sql
SELECT occurred_at, actor_label, metadata
FROM audit_logs
WHERE action = 'workspace_suspended' AND target_id = :tenant_id
ORDER BY occurred_at DESC;
```

`{"reason": "last_owner_removed_by_platform"}` means platform staff deleted or
disabled the workspace's last owner, and the workspace was suspended
automatically because an ACTIVE workspace with no owner cannot be administered
by anybody — inviting an owner, changing the plan and closing it are all
owner-only.

**Recovery is two deliberate steps, in this order.**

```
POST /api/v1/platform/tenants/{tenant_id}/ownership   {"user_id": "..."}
POST /api/v1/platform/tenants/{tenant_id}/restore
```

The first promotes somebody who is **already a member** — it cannot admit a new
account, staff's own included, which is what keeps it from being a general
membership power. A revoked membership is eligible, and is usually the only
candidate: the colleagues left behind are frequently the ones removed alongside
the owner.

Find candidates with:

```sql
SELECT u.id, u.email, m.role, m.status
FROM memberships m JOIN users u ON u.id = m.user_id
WHERE m.tenant_id = :tenant_id AND u.deleted_at IS NULL AND u.is_active
ORDER BY m.created_at;
```

The second re-enables service, and **refuses while the workspace still has no
owner** (`409 workspace_orphaned`) — so the order above is enforced rather than
merely recommended.

If no eligible member exists at all, there is nobody to hand the workspace to.
That is a conversation with the customer, not a database edit.

### Watch for orphans that nobody reported

`wasla_orphaned_workspaces` is counted at every scrape and should always be
zero. The `OrphanedWorkspace` alert fires if it is not. To see which:

```sql
SELECT t.id, t.slug
FROM tenants t
WHERE t.status = 'active' AND t.deleted_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM memberships m
      WHERE m.tenant_id = t.id AND m.status = 'active' AND m.role = 'tenant_owner'
  );
```

A non-zero count means an invariant was broken by a path that was supposed to
prevent it. Repair ownership as above, and treat the cause as a defect.

### Platform staff: who can act on whom

`PLATFORM_OWNER` outranks `PLATFORM_ADMIN`. An admin administers the platform
over every account that is **not** a platform owner; disabling, deleting or
demoting an owner is an owner's act and answers `403 permission_denied` to
anybody else.

Above that sits one invariant the platform cannot be talked out of: **at least
one live platform owner must remain**, where live means `is_active` and
`deleted_at IS NULL`. Four commands can take ownership away, and every one of
them refuses the last:

```
DELETE /api/v1/platform/users/{user_id}
POST   /api/v1/platform/users/{user_id}/disable
python -m app.platform.roles revoke <email-or-id>
python -m app.platform.roles grant  <email-or-id> platform_admin   # a demotion
```

The last one is the trap worth knowing about: `grant … platform_admin` against
an account that currently owns the platform *removes* platform ownership, under
a subcommand whose name suggests it only ever adds. It is guarded the same way.

Who counts, right now:

```sql
SELECT email, is_active, deleted_at IS NOT NULL AS deleted
FROM users
WHERE platform_role = 'platform_owner';
```

Only rows with `is_active = true` and `deleted = false` count. A deleted owner
does not, and since the deletion path clears `platform_role`, new tombstones do
not appear here at all — the role they held is recorded in the audit entry's
`previous_platform_role` instead.

**Neither role can act on its own account** through the platform API: both
`disable` and `delete` answer `422` for a self-target. Closing your own account
is `DELETE /auth/me`, which asks for the password first.

**If the platform somehow has no live owner**, the way back is the operator
command from a shell on the deployment — it is the only path that can create
platform authority, by design, and it cannot create an account:

```
python -m app.platform.roles grant <email-or-id> platform_owner
```

It refuses a deleted account outright, and a disabled one until it is re-enabled
(`POST /api/v1/platform/users/{id}/enable`, which any platform staff may run).


### Close somebody's account on their behalf

```
DELETE /api/v1/platform/users/{user_id}
```

Irreversible, and there is no `enable` counterpart. Two things it *will* refuse:
your own account, and — if you are a platform admin — an account holding
platform ownership. See the section above.

Before running it, check whether the person is the last owner of any live
workspace. The platform route, unlike the self-service one, does **not** refuse
for that:

```sql
SELECT t.slug
FROM memberships m
JOIN tenants t ON t.id = m.tenant_id
WHERE m.user_id = :user_id
  AND m.status = 'active'
  AND m.role = 'tenant_owner'
  AND t.deleted_at IS NULL
  AND (SELECT count(*) FROM memberships o
       WHERE o.tenant_id = m.tenant_id
         AND o.status = 'active'
         AND o.role = 'tenant_owner') = 1;
```

Any row returned is a workspace that **will be suspended automatically** when
the deletion runs — an ACTIVE workspace with no owner is unadministrable, so the
platform stops serving it rather than leaving it in that state. The audit entry
records `last_owner_removed_by_platform`, the deletion's own entry lists the
slugs under `orphaned_workspaces`, and a warning line
(`account.deleted_orphaned_workspaces`) names them in the log.

The deletion is deliberately **not** refused: an abusive or compromised account
must not become undeletable by owning a workspace. If the workspace should keep
running, promote somebody there *first* — otherwise expect to run the ownership
repair above afterwards.

### CRM relational invariants (before and after deploying 0068)

Migration 0068 adds tenant-agreed keys to the CRM relations and refuses to run
if existing rows would violate them, naming how many of each. It never repairs:
a crossed row is evidence, and attaching it to a guessed customer or workspace
would destroy it. Run these first, on a replica if you have one; every one must
return 0.

```sql
-- follow-up naming another workspace's lead (CRM-01)
SELECT f.id, f.tenant_id, l.tenant_id AS lead_tenant FROM follow_ups f
JOIN leads l ON l.id = f.lead_id WHERE l.tenant_id <> f.tenant_id;
-- lead whose conversation is with a different customer (CRM-14)
SELECT l.id FROM leads l JOIN conversations c ON c.id = l.conversation_id
WHERE l.contact_id IS NOT NULL AND c.contact_id <> l.contact_id;
-- lead contact / conversation, note and activity in another workspace
SELECT l.id FROM leads l JOIN contacts c ON c.id = l.contact_id WHERE c.tenant_id <> l.tenant_id;
SELECT l.id FROM leads l JOIN conversations c ON c.id = l.conversation_id WHERE c.tenant_id <> l.tenant_id;
SELECT n.id FROM lead_notes n JOIN leads l ON l.id = n.lead_id WHERE l.tenant_id <> n.tenant_id;
SELECT a.id FROM lead_activities a JOIN leads l ON l.id = a.lead_id WHERE l.tenant_id <> a.tenant_id;
```

If any returns rows: look at who created them and when (`created_at`,
`created_by_id`, the audit trail), decide with the workspace owner what the row
should have said, and correct or remove it deliberately. Then run the migration.

These are not blockers for the migration but should be 0 after it, and are worth
reviewing once on existing data because the rules they check were not enforced
before (CRM-11, CRM-07, CRM-10, CRM-02):

```sql
-- conversations / leads owned by somebody no longer an active member
SELECT c.id FROM conversations c JOIN tenants t ON t.id = c.tenant_id AND t.deleted_at IS NULL
WHERE c.assigned_to_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM memberships m
  WHERE m.tenant_id = c.tenant_id AND m.user_id = c.assigned_to_id AND m.status = 'active');
SELECT l.id FROM leads l JOIN tenants t ON t.id = l.tenant_id AND t.deleted_at IS NULL
WHERE l.assigned_to_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM memberships m
  WHERE m.tenant_id = l.tenant_id AND m.user_id = l.assigned_to_id AND m.status = 'active');
-- pending reminders of somebody who has left
SELECT f.id FROM follow_ups f WHERE f.status = 'pending' AND f.created_by_kind = 'user'
  AND f.created_by_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM memberships m
  WHERE m.tenant_id = f.tenant_id AND m.user_id = f.created_by_id AND m.status = 'active');
-- lead status contradicting closed_at
SELECT id FROM leads WHERE status NOT IN ('won','lost') AND closed_at IS NOT NULL;
SELECT id FROM leads WHERE status IN ('won','lost') AND closed_at IS NULL;
-- cancelled follow-up that names a sent message
SELECT id FROM follow_ups WHERE status = 'cancelled' AND (message_id IS NOT NULL OR sent_at IS NOT NULL);
```

Rows from before the remediation are history, not a live defect: the first two
are fixed by assigning the conversation or lead to somebody current (the API,
with `expected_assigned_to_id`), the third by cancelling the follow-up, and the
last three need a person to read the row's activity or message and decide what
really happened. The same queries, scoped to a test's own workspaces, are the
permanent sweep in `tests/integration/crm_invariants.py`.

### Settlement backstop (before and after deploying 0074)

Migration 0074 (DB-001) records which payments an invoice counts
(`payments.applied_at`) and installs two commit-time checks: an invoice's
`amount_paid` is the net of its applied payments, and collected money that is
not applied is held with a billing incident. It backfills `applied_at` for every
collected payment that no refusal incident holds, then **refuses to install**
if the existing ledger still does not balance, naming the counts. It never
repairs: an unbalanced invoice is most likely a customer charged twice by the
concurrent settlement DB-001 describes, and which payment was the duplicate is a
person's decision.

```sql
-- collected payments no incident holds, beyond the first, on an invoice whose
-- amount_paid they do not add up to: the candidates for "the duplicate"
CREATE TEMP TABLE extra AS
SELECT p.id, p.invoice_id FROM (
  SELECT p.*, row_number() OVER (PARTITION BY p.invoice_id ORDER BY p.processed_at, p.id) n
  FROM payments p
  WHERE p.status IN ('succeeded', 'refunded')
    AND NOT EXISTS (SELECT 1 FROM billing_incidents b WHERE b.payment_id = p.id)) p
JOIN invoices i ON i.id = p.invoice_id
WHERE p.n > 1
  AND i.amount_paid <> (SELECT sum(q.amount - q.refunded_amount) FROM payments q
        WHERE q.invoice_id = i.id AND q.status IN ('succeeded', 'refunded')
          AND NOT EXISTS (SELECT 1 FROM billing_incidents b WHERE b.payment_id = q.id));
SELECT e.invoice_id, p.id, p.amount, p.provider, p.provider_reference, p.processed_at
FROM extra e JOIN payments p ON p.id = e.id ORDER BY e.invoice_id, p.processed_at;
```

For each row: check the provider's dashboard for the transactions. The earliest
payment paid the invoice and stays; each listed one is the extra. Raise the
incident settlement would have raised - so the money is held, visible in the
`BillingDuplicatePayment` alert and the operator queue, and refunded through the
platform API:

```sql
INSERT INTO billing_incidents (id, tenant_id, kind, status, dedupe_key, payment_id,
  invoice_id, provider, provider_transaction_id, amount, currency, detail,
  created_at, updated_at)
SELECT gen_random_uuid(), p.tenant_id, 'duplicate_payment', 'open',
       'duplicate_payment:' || p.id || ':' || coalesce(p.provider_reference, ''),
       p.id, p.invoice_id, p.provider, p.provider_reference, p.amount, p.currency,
       'Held by the 0074 reconciliation: a second payment for one invoice.', now(), now()
FROM payments p WHERE p.id IN (SELECT id FROM extra);
```

This procedure was rehearsed on a copy of a 0073 database holding the ten double
settlements the DB-001 reproduction left: 0074 refused naming 10 invoices, the
query listed 10 extra payments, and after the insert 0074 installed.

Then rerun the migration: the extra payment is left unapplied and held. After
0074 the same state cannot be written - a commit that would leave it fails with
SQLSTATE 23000 - and the settlement lock order means an ordinary race never
reaches that check: it is refused and raised as an incident first.

### Financial integrity (before and after deploying 0077)

Migration 0077 (DB-004, DB-005) makes every financial binding name its own
workspace, pins a subscription's version to its own plan, and freezes settled
invoices and collected payments. It counts existing rows each new rule would
refuse and fails, changing nothing, if any exist. Every query below must return
no rows before it can run:

```sql
-- money bound across workspaces (DB-004)
SELECT p.id FROM payments p JOIN invoices i ON i.id = p.invoice_id WHERE i.tenant_id <> p.tenant_id;
SELECT p.id FROM payments p JOIN payment_methods m ON m.id = p.payment_method_id WHERE m.tenant_id <> p.tenant_id;
SELECT i.id FROM invoices i JOIN subscriptions s ON s.id = i.subscription_id WHERE s.tenant_id <> i.tenant_id;
SELECT t.id FROM topup_purchases t JOIN invoices i ON i.id = t.invoice_id WHERE i.tenant_id <> t.tenant_id;
SELECT t.id FROM topup_purchases t JOIN payments p ON p.id = t.payment_id WHERE p.tenant_id <> t.tenant_id;
SELECT b.id FROM billing_incidents b JOIN invoices i ON i.id = b.invoice_id WHERE i.tenant_id IS DISTINCT FROM b.tenant_id;
SELECT b.id FROM billing_incidents b JOIN payments p ON p.id = b.payment_id WHERE p.tenant_id IS DISTINCT FROM b.tenant_id;
SELECT a.id FROM billing_adjustments a JOIN invoices i ON i.id = a.invoice_id WHERE i.tenant_id <> a.tenant_id;
SELECT a.id FROM billing_adjustments a JOIN subscriptions s ON s.id = a.subscription_id WHERE s.tenant_id <> a.tenant_id;
SELECT s.id FROM subscriptions s JOIN plan_versions v ON v.id = s.plan_version_id WHERE v.plan_id <> s.plan_id;
-- undated settled states, and grants without payment (DB-005)
SELECT id FROM invoices WHERE status = 'paid' AND paid_at IS NULL;
SELECT id FROM payments WHERE status IN ('succeeded', 'refunded') AND processed_at IS NULL;
SELECT t.id FROM topup_purchases t LEFT JOIN invoices i ON i.id = t.invoice_id
 WHERE t.source = 'purchase' AND t.status = 'granted' AND (i.id IS NULL OR i.status <> 'paid');
SELECT o.id FROM custom_plan_offers o WHERE o.status = 'active' AND NOT EXISTS
 (SELECT 1 FROM invoices i WHERE i.custom_plan_offer_id = o.id AND i.status = 'paid');
```

A crossed binding is evidence of a defect or of manual repair SQL: find it in
the audit trail and the provider's dashboard, decide with the workspace owner
which workspace the money belongs to, and correct it deliberately. A missing
`paid_at` or `processed_at` is taken from the settling `payment_recorded` audit
entry or the provider's transaction time, never from `now()`. A grant or an
activation without a paid invoice is reversed through the platform API, which
records why. Then run the migration.

After 0077, the same states are refused as they are written: a crossed binding
with SQLSTATE 23503, a rewrite of settled terms with 23000 (a settled invoice
keeps its terms; collected money keeps what it was), and an unpaid grant or
activation at commit with 23000. The documented reversals still work: a refund
lowers `amount_paid` and may reopen or, for an operator's full refund, void
the invoice, and a refunded payment's `refunded_amount` only rises.

### Periods, default cards and addresses (before and after deploying 0078)

Migration 0078 (DB-012, DB-018, DB-025) refuses to run while any of these
return rows, and changes nothing when it refuses:

```sql
SELECT id FROM subscriptions WHERE ended_at IS NULL AND current_period_end <= current_period_start;
SELECT id FROM invoices WHERE period_end < period_start;
SELECT tenant_id, array_agg(id ORDER BY created_at DESC, id DESC) FROM payment_methods
 WHERE is_default AND status = 'active' GROUP BY tenant_id HAVING count(*) > 1;
SELECT lower(email), array_agg(id) FROM users GROUP BY lower(email) HAVING count(*) > 1;
```

- A reversed period: take the true period from the subscription's latest paid
  invoice or its audit trail (`subscription_*` entries) - never guess.
- Two active defaults: nothing about a card is lost by demoting one. Keep the
  default the workspace owner chose last (the newest, first in the list above)
  and demote the rest - `UPDATE payment_methods SET is_default = false WHERE id
  IN (...)` - telling the owner which card renewals will use.
- Two accounts for one address: this is an identity question, not a data one.
  Establish with the owners which account is theirs; the other is closed
  through the account API, never deleted, and its address changed only with
  its owner's agreement.

After 0078, each is refused as written (23514 for a period, 23505 for a second
default or a case-variant address), and a concurrent first-card save keeps
the second card as an ordinary one instead of a second default.

### Plan prices (before and after deploying 0081)

Migration 0081 (ADR-116) gives every priced plan version one `plan_prices` row -
its own published price, currency and interval, nothing invented - and pins
every paid subscription, scheduled change, priced invoice and custom plan offer
to it. It refuses to run, changing nothing, while any of these return rows:

```sql
SELECT i.id FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id
 WHERE i.purpose::text IN ('checkout','renewal','manual') AND v.price > 0
   AND (i.amount_due <> v.price OR i.currency <> v.currency);
SELECT i.id FROM invoices i JOIN plan_versions v ON v.id = i.plan_version_id
 WHERE i.purpose::text IN ('checkout','renewal','manual') AND v.price <= 0 AND i.amount_due > 0;
SELECT o.id FROM custom_plan_offers o JOIN plan_versions v ON v.id = o.plan_version_id
 WHERE v.price <= 0;
```

Each is an invoice or offer whose price cannot be known without guessing. Find
the true terms from the invoice's `lines`, its payments and the audit trail,
and correct the record through the platform (void and reissue), never by
editing a settled invoice. After 0081 a yearly price is added only by the
platform API.

**Downgrading 0081 -> 0080** is refused while any subscription, scheduled
change, invoice or offer names a price other than its version's published terms
(a yearly price added to a monthly version), or any live subscription's usage
cycle differs from its billing term: dropping the columns would re-price them.

### Omnichannel foundation (0082-0084)

Migrations 0082-0084 (ADR-117 to ADR-121) add the channel-neutral primitives and
move WhatsApp onto them without rewriting a conversation or a message:

| Migration | What it does | Locks |
| --- | --- | --- |
| 0082 | `channel_connections` (one per WhatsApp number, **same id**), `contact_identities` (one phone identity per contact, from its own `wa_id`), the mirror and phone-identity triggers, `contacts.wa_id` nullable, nullable `participant_identity_id` / `messages.connection_id`, media position and locator, `whatsapp_events.channel` | Reads for the backfills; metadata-only ALTERs with a 15 s `lock_timeout` |
| 0083 | Batched backfills (5,000 ids per transaction, keyset walk): conversation participants, message connections, media handle locators, campaign recipient identities. Idempotent: re-running finishes a stopped run | Row locks of one batch at a time |
| 0084 | New uniques `CONCURRENTLY`, keys `NOT VALID` then `VALIDATE` each in its own transaction, NOT NULL via validated CHECKs; drops the keys they replace | SHARE UPDATE EXCLUSIVE while validating |

**Before deploying**, on a replica (every statement is a `SELECT`; it runs under
`default_transaction_read_only = on`):

```
INVARIANTS_DATABASE_URL=<replica> python -m scripts.omnichannel_invariants census
```

`q1_*` counts the phone/business-scoped-id pairs Meta asserted in raw payloads
that retention has not yet cleared (30 days after processing, DB-011). Run it
before 0082 if pairing history matters: the pairs it counts are not recoverable
once the payloads are redacted. `q2`-`q8` are the audit's other operator checks.

**0082 refuses, changing nothing,** while any of these return a count - each is a
row the neutral model could map only by guessing:

```sql
SELECT phone_number_id FROM whatsapp_accounts WHERE released_at IS NULL
 GROUP BY phone_number_id HAVING count(*) > 1;            -- a number claimed live twice
SELECT id FROM whatsapp_accounts WHERE phone_number_id = '';
SELECT id FROM contacts WHERE wa_id = '';
SELECT c.id FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM whatsapp_accounts a
 WHERE a.id = c.account_id AND a.tenant_id = c.tenant_id);
SELECT c.id FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM contacts k
 WHERE k.id = c.contact_id AND k.tenant_id = c.tenant_id);
```

**0083 refuses** while a conversation's contact holds no WhatsApp phone identity
(no deterministic address to pin). **0084 refuses** while a row exists that a new
key would refuse; its message lists each count. Fix the data deliberately -
never by deleting conversations or contacts - and re-run.

**After deploying:**

```
python -m scripts.db_preflight verify
python -m scripts.omnichannel_invariants verify
```

The second must print `ok`: every number has its connection, every contact its
phone identity, every conversation a participant of its own contact on its own
channel, every outbound message a route that agrees end to end, no agent turn
triggered by a non-customer message, every file in its message's workspace and
conversation.

**Rolling deploy.** Old application processes keep working against the new
schema during the window: the triggers give every number written the old way
its connection, every contact its phone identity, every conversation its
participant and every message its connection; a media row written with only the
WhatsApp handle is fetched through that handle. One thing an old process cannot
do is reply to a customer WhatsApp knows only by a business-scoped id (a username
sender): it reads `contacts.wa_id`, which such a contact does not have, so that
reply is recorded as undelivered. Keep the overlap short. New processes must not
run against a database before 0084: they read the new columns.

**Downgrades refuse rather than lose data.** 0084 -> 0083 refuses while a
conversation or event sits on a connection that is not a WhatsApp number, or a
message holds more than one file. 0082 -> 0081 refuses while any contact has no
phone (a username sender), any identity is not a contact's own phone, any
connection is not a WhatsApp number, any file is at a position above 0 or
located by URL, or any echo event exists. The 0084 downgrade is one transaction
on purpose: a refusal anywhere rolls the whole run back, so the database is
never left at "0084" with 0084's keys gone.

**Sending allowance.** `CONNECTION_SENDS_PER_MINUTE` (unset by default) holds a
per-connection allowance shared by every sender (ADR-123). When set, a campaign
or follow-up over it waits for the next minute without spending an attempt
(`channel.connection_throttled` in the log); replies are never refused.

### Omnichannel final remediation (0085-0091)

The final omnichannel audit's remediation (ADR-120/121/123 amended, ADR-124 to
ADR-130). Every migration is additive and online:

| Migration | What it does | Locks |
| --- | --- | --- |
| 0085 | `messages.action_source/action_payload/action_title`, `contacts.opt_out_via` and `marketing_resumed_at`, template opt-out payloads; two new enum types | Metadata-only `ADD COLUMN`s, 15 s `lock_timeout` |
| 0086 | `conversations.automation_disclosed_at`, `ai_resumed_at`; `tenants.automation_disclosure` | Metadata-only |
| 0087 | `message_origin` gains `external` | `ALTER TYPE ... ADD VALUE` in its own autocommit block |
| 0088 | `messages.provider_sent_at` | Metadata-only |
| 0089 | `whatsapp_event_kind` gains `preference` | Autocommit block |
| 0090 | `channel_connections.sends_per_minute` and its CHECK (`NOT VALID`, then `VALIDATE`) | SHARE UPDATE EXCLUSIVE while validating |
| 0091 | `ix_conversations_tenant_id_channel_last_message_at`, `CONCURRENTLY`; an INVALID leftover of a failed build is dropped and rebuilt | None blocking writes |

**Downgrades refuse rather than lose data**: 0085, 0086, 0088 and 0090 while any
row holds what they added; 0087 and 0089 while a row carries the new label -
**an enum label cannot be dropped**, so those two downgrades leave the label in
the type. 0091's downgrade drops the index in the run's transaction (MIG-0091,
below), so a refusal further down rolls it back.

**After deploying:**

```
python -m scripts.omnichannel_invariants verify
INVARIANTS_DATABASE_URL=<replica> python -m scripts.omnichannel_invariants census
```

`verify` gained `window_anchor_older_than_newest_inbound` (OMNI-036),
`inbound_tap_without_its_words` (OMNI-030),
`message_event_without_its_projected_message` (OMNI-032),
`collision_evidence_not_failed` (OMNI-043) and
`ai_reply_on_a_disclosure_channel_never_disclosed` (OMNI-041). A non-zero
`window_anchor_older_than_newest_inbound` on the first run measures conversations
the old code already moved backwards; they correct themselves at the customer's
next message. `census` gained `q5_inbound_interactive_without_text`,
`q6_retained_stop_taps_without_opt_out` and `collision_evidence_events`.

#### Recover opt-out taps the old code missed (time-bound)

Before 0085 a customer tapping "Stop promotions" was not opted out. The raw
deliveries are kept for 30 days after processing (DB-011) and then redacted, so
**run this within 30 days of deploying 0085, or the evidence is gone**:

```
python -m scripts.omnichannel_invariants recover-button-opt-outs --dry-run
python -m scripts.omnichannel_invariants recover-button-opt-outs --apply
```

The dry run (the default) is read-only and prints counts per workspace id -
candidates, would apply, skipped because the contact resumed later - and nothing
else: no phone, business-scoped id or message text. `--apply` records each
opt-out through the normal writer with provenance `replay`, dated by the tap. A
second `--apply` changes nothing; a contact re-admitted after the tap is never
opted out again. `q6_retained_stop_taps_without_opt_out` in the census should
read 0 afterwards.

#### Pause a channel (rollback without a deploy of code)

```
PAUSED_CHANNELS=instagram
```

and restart the API and workers. A paused channel's inbound is still stored and
shown in the inbox; sends, agent turns, tool calls and file fetches on it are
refused before anything is staged; follow-ups and campaigns wait (rechecked every
15 minutes) without spending an attempt. Every other channel is untouched.
`wasla_channel_state{state="paused"}` shows it, and `ChannelPausedForADay`
reminds you after a day. To remove a channel for good, remove its adapter from
the registry: its conversations still render, with `reply_policy.state =
"unavailable"`. Never pause by deleting connections.

#### A connection reads permission_missing or rate_limited

`ChannelConnectionUnusable`. Meta's error **code** decides (ADR-130):
`permission_missing` follows codes 3, 10, 200-299, 131005, 368, 131031 or
133010 - re-grant the permission or fix the number in Meta Business Manager;
`rate_limited` follows 4, 80007, 130429, 131056 or 131057 - campaigns wait on
their own, a lasting one needs the number's limit raised. The next successful
send clears either. A connection's own allowance can be set with
`UPDATE channel_connections SET sends_per_minute = <n> WHERE id = <id>`
(null returns it to `CONNECTION_SENDS_PER_MINUTE`).

#### Webhook delivery horizons and signing secrets

Meta retries a failed WhatsApp delivery for **up to 7 days**; the Graph webhooks
of Messenger and Instagram retry for about **36 hours** (OMNI-050). An outage
longer than that loses those channels' events for good, so treat a paused or
failing Instagram/Messenger webhook as same-day work. Each product's signature
is verified with `META_INSTAGRAM_APP_SECRET` / `META_MESSENGER_APP_SECRET` where
set, else `META_APP_SECRET`; which secret signs an Instagram-Login app's
webhooks must be confirmed against a real delivery before that channel ships.
`WEBHOOK_MAX_REQUEST_BYTES` is 3 MiB, Meta's documented maximum; any refusal
fires `WebhookBodyTooLarge`.

#### Graph API version (deadline 2027-01-21)

`META_API_VERSION` defaults to `v21.0`, which Meta serves until **2027-01-21**
(OMNI-055; start-up warns from `META_API_SUNSETS`). Plan: by 2026-12-01 set
`META_API_VERSION=v24.0` (served until 2028-02-18) on staging and re-verify, with
recorded payloads, the contracts the final audit read on 2026-10-02 - the
messages and statuses webhook shapes (including `button`, `interactive`,
`user_preferences`), the send response, media upload and download, template
send and the error envelope and codes ADR-130 classifies - then change the
default in code and deploy before 2027-01-21.

#### O8 - removing the WhatsApp compatibility layer (not applied)

Drops `uq_messages_tenant_id_wa_message_id` and
`uq_whatsapp_events_tenant_id_event_id` (`DROP INDEX CONCURRENTLY`), the mirror
and phone-identity triggers, `contacts.wa_id` and `message_media.wa_media_id`,
and moves lifecycle writes to `channel_connections`. **Only when all hold**: one
release has run on the neutral path; `omnichannel_invariants verify` is 0 on
production; no API client reads `wa_message_id`, `account_id` or
`ContactOptOutRead.wa_id`; and a point-in-time restore has been rehearsed.
Until then, colliding events are kept as failed evidence (ADR-120) and the
`contacts.wa_id` trigger records `source = provider` for any writer - accepted
until O8 (OMNI-047). Restoring a dropped unique later uses
`CREATE UNIQUE INDEX CONCURRENTLY`, which fails loudly if a collision was stored
meanwhile.

### Entitlements and channel capacity (0092-0098)

ADR-131 (ENT-01..ENT-24): one AI allowance per workspace, held at engagement and
charged on a usable outcome; channel capacity instead of a WhatsApp number limit,
with typed and general slots; channel types per plan; the capacity-reduction
grace; per-channel marketing consent; channel-neutral meters; the placeholder
catalogue. Every migration is additive and online:

| Migration | What it does | Locks |
| --- | --- | --- |
| 0092 | `channel_kind` gains `telegram` and `tiktok` - vocabulary, not support | `ALTER TYPE ... ADD VALUE` in its own autocommit block, first |
| 0093 | `topup_entitlement` gains `channel_connections` (autocommit, first); `plan_versions.allowed_channel_types` and the `plans` mirror; `topup_products/purchases.channel_type`; `topup_product_plans`; `channel_connections.disabled_reason/at/by`; triggers refusing the retired `whatsapp_numbers` on new versions, products and purchases. Existing number products become `channel_connections` slots typed `whatsapp`; no stored version or purchase is rewritten | Metadata-only `ADD COLUMN`s on small tables; CHECKs `NOT VALID` then validated; 15 s `lock_timeout` |
| 0094 | `agent_turn_outcome` gains `channel_not_in_plan` (autocommit, first); `agent_turns.charge_state/held_at/charged_at/released_at/charge_release_reason`; `usage_events.channel/connection_id/agent_turn_id`; `ix_agent_turns_held` and `uq_usage_events_tenant_id_agent_turn_id`, `CONCURRENTLY` | Metadata-only `ADD COLUMN`s on `agent_turns` and `usage_events`; validation and index builds in a final autocommit block, holding nothing that blocks writes |
| 0095 | `audit_action` gains three labels (autocommit, first); `channel_capacity_reductions`, `channel_capacity_preselections` | Two new, empty tables |
| 0096 | `usage_event_type` gains `message_received` and `message_sent` (autocommit, first); a CHECK that a neutral message row names its channel, validated in a final autocommit block | No rewrite |
| 0097 | `contact_channel_consents`; every person-level opt-out and resume moved to the contact's WhatsApp row; `contacts` **drops** `marketing_opt_out_at`, `opt_out_source`, `opt_out_via`, `marketing_resumed_at` | One `INSERT ... SELECT`; catalogue-only `DROP COLUMN`s |
| 0098 | `topup_products.price` nullable with `ck_topup_products_priced_when_active`; new versions of `starter`, `pro`, `business`, `enterprise` with channel capacity and channel types; six channel top-ups, inactive and unpriced | Catalogue tables only |

**The previous image cannot run on this schema.** 0093 writes
`channel_connections` into `topup_products.entitlement_key`, a label the older
code cannot read, and 0097 drops the person-level opt-out columns every older
read of a contact selects. Stop the API and the workers before `migrate` for
this release, then start the new image - do not let the old processes serve
between the two. For the same reason a rollback past this release is not
"deploy the previous digest": it is a deliberate downgrade (below), or a fix
forward.

**0097 refuses, changing nothing,** while a contact holds an opt-out with no
`opt_out_source`: the consent row must say who decided, and inventing it would
falsify the record. Find them with
`SELECT id FROM contacts WHERE marketing_opt_out_at IS NOT NULL AND opt_out_source IS NULL;`,
establish the source from the audit trail and the conversation, and re-run.

**Downgrades refuse rather than lose data**, each naming what it found:

| Downgrade | Refused while any exist |
| --- | --- |
| 0098 → 0097 | a subscription, scheduled change, invoice, offer, plan migration, later version or price change on a placeholder version; a purchase of a placeholder product, or an operator's price, activation or eligibility on one; any other product without a price |
| 0097 → 0096 | a consent on a channel other than WhatsApp, or carrying a resume's provenance |
| 0096 → 0095 | a usage row under `message_received` or `message_sent` |
| 0095 → 0094 | a capacity reduction or pre-selection, or an audit entry of one |
| 0094 → 0093 | a turn holding the allowance (`charge_state = 'held'`), or a turn that ended `channel_not_in_plan` |
| 0093 → 0092 | a version stating channel types, a channel purchase, a typed product other than a converted WhatsApp one, plan eligibility, a recorded disable, an audit entry of a neutral connection action |
| 0092 → 0091 | a connection, identity, conversation or event on `telegram` or `tiktok` |

No downgrade in 0092-0098 commits part-way: a refusal anywhere in a run from
head to 0091 rolls the whole run back, and the database is still at head.
**An enum label cannot be dropped**, so a successful downgrade leaves the
labels in their types, where a re-upgrade finds them (`IF NOT EXISTS`).

**If a migration stops half-way.** 0092, 0093, 0094, 0095 and 0096 add their
labels in an autocommit block that runs *first*: a failure there leaves nothing
else applied - rerun normally. 0094 and 0096 also end with one (validation, and
0094's indexes); a failure inside that last block is the *A migration stopped
half-way* case below - confirm the revision's objects, `alembic stamp` it, and
`upgrade head`.

**After deploying:**

```
python -m scripts.db_preflight verify
python -m scripts.omnichannel_invariants verify
```

`verify` runs the entitlement ledger E01-E15 with the omnichannel invariants;
every one must read 0. It reads the clock, `DEFAULT_PLAN_CODE` and
`AI_TURN_HOLD_TTL_SECONDS` from the environment it runs in, so run it with the
deployment's values. E01 and E02 (connections over capacity, or of a type the
plan excludes) are explained, and not counted, while a reduction awaits a
selection, while the subscription is not served, or for one sweep interval
after a channel top-up expires (the sweep has not opened its reduction yet).

The six channel top-ups 0098 seeds are inactive and unpriced; nothing sells
them until staff set a price and activate them (`docs/BILLING_OPERATIONS.md`,
*Channel slots*). The placeholder plan values are staff's to confirm or replace
through the platform API.

#### A refused downgrade leaves the database where it started (MIG-0091)

Alembic runs a whole `downgrade` in one transaction, and every refusal is
raised before its migration changes anything. Until MIG-0091 was fixed, five
downgrades - 0075, 0078, 0079, 0081 and 0091 - dropped their indexes
`CONCURRENTLY` in an autocommit block, which **commits every step above them**.
A downgrade from head that a lower migration then refused stopped stamped at
the last of those revisions it had passed, without that revision's indexes, and
a plain `alembic upgrade head` never rebuilt them. Measured on migration-built
databases (2026-10-08, before the fix):

| Refused at | `alembic_version` after | Lost |
| --- | --- | --- |
| 0090 (a connection with its own sending allowance) | 0091 | `ix_conversations_tenant_id_channel_last_message_at` |
| 0081 (a price its version never published) | 0091 | the same index |
| 0069 (a stored card) | 0075 | 0075's four purge indexes; 0078's, 0079's and 0081's dropped with the run |

All five now drop their indexes in the run's transaction (`SET LOCAL
lock_timeout = '15s'`, as 0094's has since 9695ec0). A refusal anywhere below
rolls the whole run back: the stamp stays at the starting head and every index
is present and valid (`tests/integration/test_migration_downgrade_atomicity.py`).
The drops take a brief ACCESS EXCLUSIVE lock on their tables, which is why a
downgrade runs with the application stopped. Only the downgrades changed; a
database at head is unaffected.

**Finding a database damaged before the fix.** Any database that was ever
downgraded across one of those revisions and refused below it may be missing
indexes. `python -m scripts.db_preflight verify` now compares the database with
every table, index and named constraint the models declare and names each one
missing (`index missing: conversations.ix_conversations_tenant_id_channel_last_message_at`),
exiting 1. Run it on every such database.

**Repairing one.** Build each index it names with the statement in its
migration (each is `CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`; 0078's are
unique - check for duplicates first, as that migration does), then run `verify`
again until it prints `ok`. If the damage is found before `upgrade head` ran -
the stamp still names the revision whose indexes are gone - `alembic stamp
<the revision below it>` followed by `alembic upgrade head` rebuilds them too.

#### AI turn holds are not being released

**Alert:** `AITurnHoldsStuck`. **Metric:** `wasla_ai_turn_holds_past_ttl`.

A hold older than `AI_TURN_HOLD_TTL_SECONDS` (900) already stopped counting
against the allowance by the clock, so no customer is being refused because of
it; what has stopped is the billing sweep that marks it released. The sweep runs
in the billing worker: check the worker is up and completing passes
(`billing.sweep_completed`, `billing.ai_turn_holds_expired`) - if it is not,
renewals, top-up expiry and reductions have stopped too, which matters more.

```sql
SELECT tenant_id, count(*), min(held_at)
  FROM agent_turns
 WHERE charge_state = 'held' AND held_at < now() - interval '15 minutes'
 GROUP BY tenant_id;
```

Do not release holds by hand: the sweep does it idempotently, under the turn's
row lock, recording `hold_expired`.

#### AI turns are being charged late

**Alert:** `AITurnLateChargeSpike`. A turn settled after the sweep had released
its hold: it is still charged (`late_charge`), because usage that happened is
never refused afterwards, and the workspace may end the cycle over its
allowance. A handful means turns are taking longer than
`AI_TURN_HOLD_TTL_SECONDS` - raise it above the longest a turn really takes
(provider latency times the turn's rounds). No customer is harmed; the
allowance is briefly undercounted.

`agent.turn_hold_contended` in the AI worker's log is something else: a turn
waited 20 s for its workspace's allowance lock and was put back to be retried
before engagement - nothing was held, charged or sent. Occasional under a burst
of one workspace's customers; sustained, it means the database is slow (*The
database is struggling*).

#### Many connections were disabled automatically

**Alert:** `ChannelCapacityAutoDisableSpike` (more than twenty in an hour).
**Metrics:** `wasla_channel_capacity_reduction_disables_total{actor="system"}`,
`wasla_channel_capacity_reductions_total{cause,resolution}`.

Each was a connection whose company let a capacity reduction's grace
(`CHANNEL_CAPACITY_GRACE_DAYS`, 7) run out without choosing which to keep; the
fallback disabled channel types the plan no longer allows first, then the
newest. Many at once is usually one cause applied to many companies - a plan
migration, a withdrawn product, an expiring grant:

```sql
SELECT cause, status, count(*), min(effective_at), max(resolved_at)
  FROM channel_capacity_reductions
 WHERE resolved_at > now() - interval '2 hours'
 GROUP BY cause, status;
```

Nothing was released or deleted: history, conversations, contacts, credentials
and each number's claim are kept, and an owner re-enables a connection as soon
as there is a slot. If the capacity change itself was the mistake, restore the
capacity (a platform grant, or the right plan) and tell the owners they can
re-enable; there is no platform action that re-enables a connection or bypasses
the guard. Owners were told through the email outbox when a downgrade that
would not hold their connections was scheduled, at the boundary, and 48 hours
before the fallback - check *Email is not being delivered* if they say they
were not.

#### Reading a company's channel capacity

`GET /api/v1/platform/billing/tenants/{tenant_id}/summary` gives the slots in
force (general and typed, with what each came from), the active connections by
channel, the AI turns used, held and by channel, and the latest capacity
reduction with its cause, status and grace end. By hand:

```sql
SELECT channel, status, disabled_reason, ownership_started_at
  FROM channel_connections
 WHERE tenant_id = '<tenant id>' AND released_at IS NULL
 ORDER BY ownership_started_at;
SELECT cause, status, target_general, target_typed, target_allowed_types,
       effective_at, grace_ends_at, resolved_at
  FROM channel_capacity_reductions
 WHERE tenant_id = '<tenant id>' ORDER BY created_at DESC LIMIT 3;
```

What to tell a customer during a grace is in `docs/BILLING_OPERATIONS.md`
(*Channel capacity reductions*).

### Downgrading past the billing migrations

0071, 0072 and 0073 hold commercial records their downgrades would drop with
their tables. Each downgrade counts them first and refuses, changing nothing,
naming what it found (DB-019):

| Downgrade | Refused while any exist |
| --- | --- |
| 0073 → 0072 | custom plan offers (open, declined, expired, cancelled or active), invoices naming an offer |
| 0072 → 0071 | top-up invoices, top-up purchases and platform grants, top-up products, custom plans |
| 0071 → 0070 | billing incidents, billing adjustments, plan version migrations, plan versions after the first, scheduled plan changes |

There is no flag to override this. A release that must be rolled back past one
of these is rolled back by redeploying the previous image against the current
schema where that image can run on it (each migration's docstring says what it
changes for older code), and otherwise fixed forward - never by dropping the
ledger. If a
downgrade really is intended (a scratch or staging database), export the
records first, delete them deliberately, and then downgrade.
`tests/integration/test_migration_recovery.py` walks each refusal.

### A migration stopped half-way

Migrations that add enum labels (0059, 0063, 0064, 0067, 0068, 0071, 0072,
0073) or build indexes concurrently (0039, 0040, 0075, 0077, 0078, 0079, 0094)
end with an `autocommit_block()`; 0096 ends with one that validates a CHECK. **Alembic commits the migration's DDL before that
block and records the version after it**, because `ALTER TYPE ... ADD VALUE`
and `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. A failure
inside the block - a lost connection, a lock timeout, a killed container -
therefore leaves the DDL committed and `alembic_version` still naming the
previous revision. Rerunning fails on "already exists". Alembic does not make
enum additions transactional, and nothing here pretends it does.

1. Read the failure: which revision, and which statement in its block.
2. Confirm the revision's objects are present - its tables, columns,
   constraints and triggers (`\d table` in psql), and each enum label it adds
   (`SELECT enumlabel FROM pg_enum JOIN pg_type t ON t.oid = enumtypid WHERE
   typname = '...'`). Enum additions use `ADD VALUE IF NOT EXISTS` and index
   builds drop an `INVALID` leftover before rebuilding, so the block itself is
   safe to repeat.
3. If everything before the block is present, record it:
   `alembic stamp <revision>`, then `alembic upgrade head`, which reruns the
   rest.
4. `python -m scripts.db_preflight verify` must then report no unvalidated
   constraint, invalid index or disabled trigger, and nothing the models
   declare missing.

If step 2 finds the DDL only partly present, the failure was before the block,
the transaction rolled back, and nothing was committed: rerun normally.
`tests/integration/test_migration_recovery.py` reproduces the half-applied
state for 0073 and takes it through this path.

### The database is struggling

The API publishes the server's own view at each scrape (DB-023):
`wasla_db_connections{state}`, `wasla_db_max_connections`,
`wasla_db_lock_waiting_sessions`, `wasla_db_oldest_transaction_age_seconds`,
`wasla_db_deadlocks_total`, `wasla_db_size_bytes` and
`wasla_db_dead_tuples{table}`. The server log carries the detail: statements
over a second, lock waits over `deadlock_timeout` with the blocking process,
and long autovacuums.

- **`DatabaseDeadlocks`** - the settlement lock order (payment, then invoice,
  then subscription, then offer or top-up) is designed not to deadlock, so
  one is a code path taking locks in another order. The server log names both
  statements; find their call sites.
- **`DatabaseConnectionsNearLimit`** - compare each process's
  `wasla_db_pool_checked_out` with the budget in docs/DEPLOYMENT.md. Scaling
  replicas without resizing pools is the usual cause.
- **`DatabaseLockWaits` / `DatabaseLongTransaction`** -

  ```sql
  SELECT pid, usename, state, now() - xact_start AS open_for, wait_event_type,
         pg_blocking_pids(pid) AS blocked_by, left(query, 120)
    FROM pg_stat_activity WHERE datname = current_database() ORDER BY xact_start;
  ```

  Runtime sessions are bounded by `DATABASE_STATEMENT_TIMEOUT_MS`,
  `DATABASE_LOCK_TIMEOUT_MS` and the idle-in-transaction timeout (DB-007), so
  an old transaction is almost always a purge, a migration or somebody's psql.
  `pg_cancel_backend(pid)` first; `pg_terminate_backend(pid)` only if it will
  not stop.
- **`DatabaseDeadTuplesHigh`** - check `last_autovacuum` in
  `pg_stat_user_tables` and whether a long transaction is holding vacuum back;
  a manual `VACUUM (ANALYZE) <table>` is safe at any time.

## What to watch

**Start with the metrics.** `/metrics` publishes request rates and latency,
dependency readiness, queue depth and age, dead-letter depth, worker heartbeats
and provider outcomes; [OBSERVABILITY.md](OBSERVABILITY.md) has the catalogue
and a set of alert expressions to point a scraper at. Nothing here ships a
*configured* alert — there is no Alertmanager in this stack — so those
expressions are recommendations until somebody wires them up.

The events below are the log lines worth alerting on as well, for the failures
that are more specific than a counter can be.

| Event | Means | Urgency |
| --- | --- | --- |
| `whatsapp.invalid_signature` | Rotated secret, or somebody probing. Counted too — `WhatsAppWebhookSignatureFailures` | High if sustained |
| `agent.enqueue_failed` | Redis unreachable; messages stored but unanswered. Recoverable — see *Inbound stored but never answered* | High |
| `whatsapp.event_without_owner` | An event on a number no workspace held at that instant. Dropped rather than misrouted | Medium if sustained |
| `whatsapp.ambiguous_number_ownership` | Two claims on one number overlap. A data-integrity repair, not a routing decision | High |
| `whatsapp.credential_refused` | Meta rejected a number's token; that workspace cannot send | High |
| `whatsapp.outbound_uncertain` | A send Meta never confirmed. **Never resend automatically** | Medium, High as a rate |
| `whatsapp.template_withdrawn` | Meta refused a template; the local registry has been marked | Medium |
| `whatsapp.payload_rejected_by_database` | A delivery PostgreSQL cannot store. Acknowledged rather than retried for ever | High if sustained |
| `whatsapp.api_version_expiring` | `META_API_VERSION` is inside its last 90 days | Plan it now |
| `inbound_recovery.swept` | The sweeper finished work a queue outage lost | Informational; High if it never stops |
| `ingestion_recovery.swept` | Documents a queue outage stranded were re-queued | Informational; High if it never stops |
| `agent.turn_already_answered` | A duplicate job found the turn already owned, and did nothing | Informational — this is WQ-01's guard working |
| `follow_up.cancelled_on_handoff` | A colleague took a conversation over, so its nudge was cancelled | Informational |
| `billing.ai_allowance_exhausted` | A workspace is out of AI turns; the conversation was handed to a person (`AI_QUOTA_EXHAUSTED`) | Commercial, not operational |
| `agent.turn_hold_contended` | A turn waited past `AI_TURN_HOLD_LOCK_WAIT` for its workspace's allowance lock and was put back before engagement; nothing was held or sent | Low alone, Medium as a rate |
| `agent.turn_hold_release_failed` | A failed turn could not give its hold back; the sweep releases it at its TTL | Medium |
| `billing.ai_turn_holds_expired` | The sweep released holds past their TTL - a worker died mid-turn. `AITurnHoldsStuck` if they are not being released | Low |
| `billing.channel_activation_refused` | A connect or enable was refused: capacity or channel type | Commercial, not operational |
| `billing.channel_capacity_reduction_opened` | A company's channel capacity fell below its active connections; its grace started | Informational |
| `billing.channel_capacity_reduction_resolved` | A reduction ended - by the owner, automatically, or no longer needed | Informational; Medium as an automatic rate (`ChannelCapacityAutoDisableSpike`) |
| `agent.turn_outcome` | How every turn ended, with `outcome`. Counted by `wasla_agent_turn_outcomes_total` | Informational |
| `agent.reply_suppressed` | A reply was ready and not sent, because the workspace, agent, conversation or number changed while the model was composing | Informational; Medium if sudden |
| `agent.empty_response` | The provider answered with no words; the customer was told a colleague will follow up. `AgentEmptyResponses` | Medium, High as a rate |
| `agent.tool_crashed` | A tool handler raised; its work was rolled back to the call's savepoint and the turn continued. Carries the exception class and never its message or parameters. `AgentToolFailures` | Medium, High as a rate |
| `agent.tool_not_granted` | A model asked for a tool this agent does not have an enabled grant for. `AgentToolDenials` | Low, Medium as a rate |
| `agent.tool_not_served` | A tool was refused because the workspace, the agent, the conversation or the number stopped allowing it mid-turn | Informational |
| `agent.tool_budget_exhausted` | A model asked for more tool calls than one response or one turn may make | Low |
| `agent.tool_call_duplicate` | The provider repeated a call id inside one turn; the handler did not run again | Informational |
| `agent.turn_unkeyed` | An agent job with no trigger message; dead-lettered rather than run, because it has no identity | Medium |
| `agent.tool_execution_unrecorded` | A tool execution record could not be written; the call itself was unaffected | Medium |
| `agent.reply_truncated` | A reply over WhatsApp's limit was shortened at a sentence, with an offer to continue | Low; a rate means an agent's prompt invites long answers |
| `sentiment.persistence_failed` | A sentiment reading could not be stored; the turn continued without it | Medium if sustained |
| `ratelimit.unavailable` | Redis down; limiting is failing open | High |
| `credential.decryption_failed` | A stored credential is unreadable — check key configuration | High |
| `worker.heartbeat_failed` | A loop cannot reach Redis | High |
| `worker.job_dead_lettered` | A job stopped being retried and is waiting for an operator | High if sustained |
| `worker.job_retry_scheduled` | A job failed transiently and will be tried again | Low alone, High as a rate |
| `worker.dead_letters_replayed` | Somebody re-queued dead-lettered work | Informational, and worth an audit read |
| `metrics.collection_failed` | The scrape could not read Redis; queue signals are absent, not zero | Medium |
| `recovery.reservation_reclaimed` | A worker died holding a job. `action=requeued` is routine; `action=quarantined` needs a person | Medium / High |
| `backup.status_unreadable` | The backup status file is corrupt; `wasla_backup_*` is absent, not stale | Medium |
| `campaign.sweep_failed` | A broadcast is stalled | Medium |
| `email.sweep_failed` | The email worker's sweep threw; nothing is being delivered | High |
| `email.failed_permanently` | A message will never be sent — check `error_code` | Medium, High if sustained |
| `email.webhook_invalid_signature` | Rotated Resend secret, or somebody probing | High if sustained |
| `email.webhook_unconfigured` | `RESEND_WEBHOOK_SECRET` is unset — **no bounce or complaint is being recorded** | High |
| `email.stuck_recovered` | A worker died mid-send; the message is being re-sent | Medium |
| `email.suppressed_skipped` | A message was not sent because its address is suppressed | Low, High if sudden and widespread |
| `request.timed_out` | A handler exceeded its budget while holding a connection | Medium |

Authentication incidents should start with
`wasla_auth_security_events_total`, whose labels contain categories rather than
identities. A `refresh/replay` sample means the account-wide token-version
teardown already ran; direct the person to sign in again and investigate token
exposure. Repeated `password_reset_request/suppressed` means the per-account
budget is stopping distributed mailbox abuse; do not raise the limit until a
client loop is ruled out. `account_deleted` or `account_inactive` attempts are
expected immediately after an operator action but suspicious if sustained.
OAuth callback failures should be split by login/link and checked against
provider status, callback URI, state/nonce expiry, and browser-cookie policy.
Never search metrics by email or user id; use request/audit correlation under
the incident-access policy when an individual investigation is authorized.

Every log line carries `request_id`, and `tenant_id`, `user_id` and `conversation_id` where they apply. Fields whose names suggest secrets are redacted before serialisation, so a token cannot reach the logs even when a payload is logged whole.

---

## What this runbook cannot tell you

Stated plainly, because a runbook that pretends to cover everything is one that gets trusted where it should not be:

- **No production deployment exists yet.** Every procedure here has been executed against local containers and real PostgreSQL. None has been run under load, against real customer traffic, or during an actual incident.
- **One message has been delivered by Resend; the webhook half never has.**
  On 2026-08-27 an `EMAIL_VERIFICATION` message was sent with a real
  `RESEND_API_KEY` to a real mailbox, reported `delivered` by Resend's own API,
  and the code in the delivered body verified the account over HTTP - see
  [EMAIL.md](EMAIL.md) for what that send confirmed. What has *not* happened is
  an inbound delivery event: no `resend.email.*` webhook has arrived from
  Resend's infrastructure, because that needs a publicly reachable URL this
  environment does not have. Suppression, bounce and complaint handling are
  exercised only against synthesised, correctly-signed payloads, so the first
  production deployment is still the first test of the webhook endpoint and of
  the sending reputation.
- **A wedged event loop is invisible.** Lease renewal and the worker heartbeat both assert the same thing — the process is up and scheduling — so a loop blocked by a genuinely synchronous call keeps renewing its leases and is never reclaimed. Every loop here is I/O-bound async, so that is a bug rather than a state, but it is one this design cannot detect.
- **Backups cover PostgreSQL and nothing else.** [BACKUP.md](BACKUP.md) has the scripts, the schedule, the retention policy and a restore drill that was actually executed — against synthetic data on local containers, never against production, because there is no production. **Redis is deliberately not backed up** (queued work is late rather than lost; the messages themselves are in PostgreSQL), and **media is not in the dump and is not meant to be**: the rows and the storage keys are, the bytes are the object store's to keep ([ADR-077](../DECISIONS.md), [ADR-078](../DECISIONS.md)). On `MEDIA_STORAGE_BACKEND=local` that means nothing keeps them, and losing the host loses every attachment.
- **The media recovery check ran against MinIO, not a provider.** A synthetic object was stored, its metadata recorded, the runtime and the local volume destroyed, and the same bytes and canonical type read back by a fresh process against the same off-host store. That proves the application half — the client, the signing, the key, the metadata — and not any provider's IAM, TLS chain, replication lag or lifecycle rules.
- **Nothing verifies that a bucket's lifecycle rule outlives this backup's retention.** Restoring a database from one date against a bucket that has already expired the objects it references gives a working product with missing files, and no check anywhere compares the two windows.
- **There is no configured alerting.** [OBSERVABILITY.md](OBSERVABILITY.md) gives concrete expressions against metrics that now exist, and the table above lists the log events worth watching. Neither is a running alert: no Alertmanager, no monitoring vendor, nobody paged.
- **Auth alert delivery is externally unverified.** The API emits bounded
  auth/security counters and the observability guide supplies alert expressions,
  but no production scraper, rule evaluator, receiver, or paging drill exists
  in this repository.
- **Media durability depends on `MEDIA_STORAGE_BACKEND`** ([ADR-077](../DECISIONS.md)). On `local` the volume is the only copy and losing the host loses every attachment. On `s3` the bytes outlive the host, and their durability, versioning and lifecycle are the bucket's - the PostgreSQL backup carries the rows and the keys, never the files, and is not meant to ([BACKUP.md](BACKUP.md)).
- **`usage_events` and `audit_logs` grow without bound.** Neither is swept, deliberately — retention for billing records and audit trails is a legal question, not a disk-space one.
- **Raw WhatsApp webhook payloads age out; the events do not.** A processed event's payload is cleared after `WHATSAPP_EVENT_PAYLOAD_RETENTION_DAYS` (30) by the `retention` worker, in batches of `WHATSAPP_EVENT_REDACTION_BATCH_SIZE`, each its own short transaction (DB-011). The rows stay, so the table's row count still grows with traffic; what stops growing is its size per row. `wasla_webhook_payload_retention_total{outcome="pending"}` that keeps rising across daily passes means the sweep is failing — look for `retention.webhook_redaction_failed` in the worker's log.
- **The media store grows until a retention period is set.** `MEDIA_RETENTION_DAYS` defaults to zero, which keeps everything ([ADR-078](../DECISIONS.md)). Watch `wasla_media_retention_total{outcome="pending"}`: a number that stays above zero across sweeps is a store refusing deletions, which is otherwise invisible — the rows are claimed, the sweep reports itself as having run, and the volume does not shrink.
- **An attachment that is in the bucket and invisible.** An object's key is committed before the object exists, so a worker killed between the two leaves a row in `pending` naming exactly what it was writing ([ADR-087](../DECISIONS.md)). The `uploads` worker settles those every five minutes. If `wasla_media_upload_reconciliation_total{outcome="pending"}` keeps rising while `finalized` stays flat, that loop is not running — check `WORKER_KINDS` — or the store is not answering, which shows up as `unreachable`.
- **A collection attempt whose outcome nobody knows.** A charge is committed before Paymob is asked to move money, so a worker killed between the two leaves a row saying a card may already have been debited ([ADR-088](../DECISIONS.md)). The billing sweep asks Paymob about those every ten minutes — but only if `PAYMOB_API_KEY` is set, which is a *different* credential from `PAYMOB_SECRET_KEY`. Without it nothing is ever charged twice and nothing is ever resolved either: watch `wasla_payment_reconciliation_total{outcome="pending"}` and `wasla_oldest_pending_payment_age_seconds`.
- **`mismatched` needs a person, and nothing else will do.** An object is at a key Wasla owns and its contents are not what Wasla wrote. It is left in place deliberately: serving it would hand a colleague a file the row does not describe, and deleting it would destroy the only evidence of how it got there. Find the row by media id in the `media.upload_mismatch` log line, and look at the object by hand before deciding. Nothing in the system will clear this state on its own.

### A renewal is not being collected and the invoice looks due

Check the last attempt before checking the schedule. An invoice whose last
collection attempt is unresolved is deliberately not collectible, however far
`next_collection_at` has passed ([ADR-088](../DECISIONS.md)) — a due date is not
a licence to charge a card whose last outcome nobody knows.

```sql
SELECT p.id, p.status, p.collection_state, p.created_at, p.reconciled_at,
       i.id AS invoice_id, i.collection_attempts, i.next_collection_at
FROM payments p JOIN invoices i ON i.id = p.invoice_id
WHERE p.collection_state IN ('claimed', 'requested')
ORDER BY p.created_at;
```

| What you see | What it means |
| --- | --- |
| Nothing returned | Not this. The refusal is an ordinary one — no card, cancelled subscription, budget spent — and `billing.recurring_skipped` names it |
| `requested`, `reconciled_at` empty | Reconciliation has not reached it yet, or cannot: `PAYMOB_API_KEY` is unset |
| `requested`, `reconciled_at` moving | It is being asked about and Paymob is still saying pending or nothing |
| `claimed`, old | A worker died before it built the request. The next pass abandons it and hands the attempt back |

**Do not clear these by hand.** A row in `requested` means a card may already
have been debited, and the reason it blocks the invoice is that nobody yet knows
whether it did. If Paymob's dashboard shows the transaction, let the reconciler
settle it — or replay the callback — rather than editing the row, so the invoice
is settled by the path that also grants the plan and writes the ledger entry.

If the deployment has no `PAYMOB_API_KEY`, that is the fix: set it, and the next
sweep resolves the backlog. Until then nothing is charged twice and nothing is
collected.

### A refund was issued and the customer says it never arrived

Asking for a refund and the money arriving are separate events, and the gap
between them is the state to look for.

```sql
SELECT id, status, amount, refunded_amount,
       refund_requested_at, refunded_at, refund_reference
FROM payments WHERE tenant_id = '<tenant-id>' AND refund_requested_at IS NOT NULL
ORDER BY refund_requested_at DESC;
```

| What you see | What it means |
| --- | --- |
| `refund_requested_at` set, `refund_reference` NULL | The provider never accepted it. Look for `billing.refund_failed`; the refund can simply be requested again |
| `refund_reference` set, `refunded_at` NULL | Paymob accepted the reversal and no callback has confirmed it. If it is more than a day old, the **callback URL is the first thing to check** — the same cause as a payment that never landed |
| `refunded_at` set | Confirmed here. The delay from here is the customer's bank, typically several working days, and nothing in this system will change it |

`refund_reference` is the reversal's own transaction id, which is a *different*
transaction from the one being refunded. Search Paymob's dashboard by it.

**Do not** issue a second refund to make a stuck one move. Reversing the same
money twice is the failure this subsystem is most careful about, and the
service refuses it — recording a payment by hand to compensate would make the
ledger disagree with the bank.

### A workspace was marked past due

```sql
SELECT s.status, i.id, i.amount_due, i.amount_paid, i.issued_at
FROM subscriptions s JOIN invoices i ON i.subscription_id = s.id
WHERE s.tenant_id = '<tenant-id>' AND i.status = 'open' ORDER BY i.issued_at;
```

The sweep marks a workspace behind when an invoice it was *sent* goes unpaid
for `GRACE_DAYS` (7) from `issued_at`. The workspace is still served —
`past_due` is a serving status — so this is a conversation, not an outage.

It resolves itself when the invoice is paid: the customer opens
`POST /billing/checkout {"invoice_id": ...}` and the settling callback moves
the subscription back to `active`. That is the only thing that does; changing
`subscriptions.status` from a SQL prompt records a payment that did not happen.

The audit trail says when and why:

```sql
SELECT created_at, meta FROM audit_logs
WHERE tenant_id = '<tenant-id>' AND action = 'subscription_past_due';
```
