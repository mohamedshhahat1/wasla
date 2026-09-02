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
- **redis down** — the API keeps serving. Rate limiting fails **open** (requests are allowed), refresh-token revocation cannot be checked, and no new background jobs can be enqueued. Inbound messages are still stored: the webhook logs `agent.enqueue_failed` and returns 200 so Meta does not retry. Those conversations wait for a person until somebody requeues them.

### Customers' messages are not arriving

The webhook is the one path that must never be refused. Work backwards:

1. **Is Meta still delivering?** Check the Meta app dashboard for a disabled subscription. Meta disables a webhook that keeps failing — the most likely cause is a run of non-2xx responses.
2. **Signature failures?** `grep whatsapp.signature_invalid`. A rotated `META_APP_SECRET` that reached only some replicas looks exactly like this.
3. **Unknown number?** `grep whatsapp.unknown_phone_number_id`. The number is not connected to any workspace, or was disconnected. Not an error Meta can fix by retrying.
4. **Stored but unanswered?** Rows in `whatsapp_events` but silence from the agent means the queue, not the webhook — see below.

Nothing rate-limits this path and nothing times it out ([ADR-032](../DECISIONS.md)). If you are considering adding either, read that record first.

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
- **in-flight non-empty and static** — jobs were reserved by a worker that died. **Nothing reaps them** ([TASKS.md](../TASKS.md), phase 8). Moving one back is an operator decision, because re-running an agent job sends the customer a second reply:
  ```bash
  docker compose -f docker-compose.prod.yml exec redis \
    redis-cli LMOVE agent:jobs:inflight agent:jobs:pending RIGHT LEFT
  ```
  Ingestion and media jobs are genuinely idempotent — a stored file is not fetched twice — so those can be requeued freely.
- **dead growing** — work that stopped being retried. Read the records before doing anything with them:
  ```bash
  docker compose -f docker-compose.prod.yml exec worker \
    python -m app.workers.queues dead-letters agent --limit 5
  ```
  Each record carries the job type, the workspace, the attempt count, the first and last attempt times and a failure *category* — never an exception string, and never message content. Find the rest in the logs by `worker.job_dead_lettered` and the `tenant_id`.

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

The agent queue is **refused** without `--force`, because an agent turn ends in
a WhatsApp message that carries no idempotency key: replaying a job whose
failure came after the provider was engaged sends a second answer to a question
that already has one. Read the conversation first, decide whether answering it
again is right, and then:

```bash
docker compose -f docker-compose.prod.yml exec worker \
  python -m app.workers.queues replay agent --limit 1 --force
```

Replayed jobs go back as fresh first attempts, and the dead-letter records are
kept — so if the replay fails too, comparing the new record with the old one is
what tells you whether anything changed.

### Nobody can log in

- **429 on `/auth/login`** — the authentication limiter, ten attempts per minute per client address. Behind a proxy that does not set `X-Forwarded-For`, *every* user shares one identity and ten attempts total. Check that nginx is forwarding it.
- **401 for everyone, suddenly** — `JWT_SECRET` changed. Every issued token is now invalid. There is no recovery except issuing new ones: users log in again.
- **401 for one user** — their account was disabled, or their refresh token was rotated and the old one replayed. Access tokens are not revocable by design ([AUTH.md](AUTH.md)).

### A workspace says it is being refused

**402 `plan_limit_exceeded`** is a plan limit, not a bug. What they hit and where they stand:

```
GET /api/v1/billing/entitlements     # every limit, used and remaining
GET /api/v1/usage                    # the meters behind the period limits
```

Resource limits (numbers, agents, colleagues, documents) count rows that exist now; period limits (messages, AI requests, campaign messages) count the current billing period. Nothing on the *inbound* path is ever refused for a limit ([ADR-030](../DECISIONS.md)), so a workspace over its message allowance still receives its customers' messages — it is charged for the overage rather than cut off.

**403** is a role problem, not a plan problem. **429** is the rate limiter.

### The agent has stopped replying

In order of likelihood:

1. **The conversation was handed to a person.** `mode = 'human'`. The orchestrator refuses to answer those, by design. `GET /api/v1/analytics/conversations/{id}/events` says who took it and why.
2. **The workspace is out of AI requests.** `grep billing.ai_allowance_exhausted`. The message is stored and the conversation waits for a person; the job is *not* dead-lettered.
3. **No active default agent.** `grep agent.no_active_default`.
4. **The provider is failing.** `grep openai` in the worker logs. The client retries three times with backoff before giving up.

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

### Rotate a secret

| Secret | Effect | Procedure |
| --- | --- | --- |
| `JWT_SECRET` | Every session ends immediately | Change, restart the API. Users log in again. No staged rotation is possible — the tokens carry no key id |
| `CREDENTIAL_ENCRYPTION_KEYS` | None, if done correctly | **Prepend** the new key, keep the old: `NEW,OLD`. New credentials use the new key, old ones keep decrypting. Removing the old key before rewriting the rows makes those credentials unreadable ([ADR-034](../DECISIONS.md)) |
| `META_APP_SECRET` | Webhook signatures fail until every replica has it | Change everywhere, then restart. A partial rollout looks like an attack in the logs |
| `POSTGRES_PASSWORD` | Everything stops | Change in the database and the environment in the same maintenance window |

Nothing automatically rewrites credentials onto a new encryption key. `CredentialCipher.needs_rotation` identifies the stragglers; rewriting them is manual.

### Add or remove worker capacity

`WORKER_KINDS` selects which loops a container runs — empty means all six (`media`, `agent`, `ingestion`, `follow_up`, `campaign`, `billing`).

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
| `whatsapp.signature_invalid` | Rotated secret, or somebody probing | High if sustained |
| `agent.enqueue_failed` | Redis unreachable; messages stored but unanswered | High |
| `billing.ai_allowance_exhausted` | A workspace is out of AI requests | Commercial, not operational |
| `ratelimit.unavailable` | Redis down; limiting is failing open | High |
| `credential.decryption_failed` | A stored credential is unreadable — check key configuration | High |
| `worker.heartbeat_failed` | A loop cannot reach Redis | High |
| `worker.job_dead_lettered` | A job stopped being retried and is waiting for an operator | High if sustained |
| `worker.job_retry_scheduled` | A job failed transiently and will be tried again | Low alone, High as a rate |
| `worker.dead_letters_replayed` | Somebody re-queued dead-lettered work | Informational, and worth an audit read |
| `metrics.collection_failed` | The scrape could not read Redis; queue signals are absent, not zero | Medium |
| `campaign.sweep_failed` | A broadcast is stalled | Medium |
| `email.sweep_failed` | The email worker's sweep threw; nothing is being delivered | High |
| `email.failed_permanently` | A message will never be sent — check `error_code` | Medium, High if sustained |
| `email.webhook_invalid_signature` | Rotated Resend secret, or somebody probing | High if sustained |
| `email.webhook_unconfigured` | `RESEND_WEBHOOK_SECRET` is unset — **no bounce or complaint is being recorded** | High |
| `email.stuck_recovered` | A worker died mid-send; the message is being re-sent | Medium |
| `email.suppressed_skipped` | A message was not sent because its address is suppressed | Low, High if sudden and widespread |
| `request.timed_out` | A handler exceeded its budget while holding a connection | Medium |

Every log line carries `request_id`, and `tenant_id`, `user_id` and `conversation_id` where they apply. Fields whose names suggest secrets are redacted before serialisation, so a token cannot reach the logs even when a payload is logged whole.

---

## What this runbook cannot tell you

Stated plainly, because a runbook that pretends to cover everything is one that gets trusted where it should not be:

- **No production deployment exists yet.** Every procedure here has been executed against local containers and real PostgreSQL. None has been run under load, against real customer traffic, or during an actual incident.
- **No message has ever been delivered by Resend from this repository.** The
  email procedures above are written from the code and exercised against a
  fake provider and a mock transport. No real API key has been used and no
  delivery event has arrived from Resend's own infrastructure, so the first
  production send is also the first test of the DNS, the webhook endpoint and
  the sending reputation.
- **Backups cover PostgreSQL and nothing else.** [BACKUP.md](BACKUP.md) has the scripts, the schedule, the retention policy and a restore drill that was actually executed — against synthetic data on local containers, never against production, because there is no production. **Redis is deliberately not backed up** (queued work is late rather than lost; the messages themselves are in PostgreSQL), and **media is not backed up at all**: attachments live on one host's volume and losing it loses them.
- **No off-host copy is configured.** The backup script writes to a directory. Getting that directory somewhere else, encrypted, is the deployment's job and this repository does not automate it.
- **There is no configured alerting.** [OBSERVABILITY.md](OBSERVABILITY.md) gives concrete expressions against metrics that now exist, and the table above lists the log events worth watching. Neither is a running alert: no Alertmanager, no monitoring vendor, nobody paged.
- **Media files are not replicated.** They live on one host's volume ([ADR-023](../DECISIONS.md)). Losing it loses every attachment customers sent.
- **`usage_events` and `audit_logs` grow without bound.** Neither is swept, deliberately — retention for billing records and audit trails is a legal question, not a disk-space one.

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
