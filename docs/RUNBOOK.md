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
# Redis: pending vs in-flight for each queue
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN agent:jobs:pending
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN agent:jobs:inflight
docker compose -f docker-compose.prod.yml exec redis redis-cli LLEN agent:jobs:failed
```

- **pending growing, in-flight empty** — nothing is consuming. Check `WORKER_KINDS`: a typo fails at startup, but a *valid* subset silently runs only those loops.
- **in-flight non-empty and static** — jobs were reserved by a worker that died. **Nothing reaps them** ([TASKS.md](../TASKS.md), phase 8). Moving one back is an operator decision, because re-running an agent job sends the customer a second reply:
  ```bash
  docker compose -f docker-compose.prod.yml exec redis \
    redis-cli LMOVE agent:jobs:inflight agent:jobs:pending RIGHT LEFT
  ```
  Ingestion and media jobs are genuinely idempotent — a stored file is not fetched twice — so those can be requeued freely.
- **failed growing** — the dead-letter list. Read one before requeueing anything: `redis-cli LINDEX agent:jobs:failed 0`.

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

## What to watch

Nothing here ships a metrics stack ([ARCHITECTURE.md §20](../ARCHITECTURE.md) — OpenTelemetry and Prometheus are Planned). These are the log events worth alerting on with whatever aggregator is in front of them.

| Event | Means | Urgency |
| --- | --- | --- |
| `whatsapp.signature_invalid` | Rotated secret, or somebody probing | High if sustained |
| `agent.enqueue_failed` | Redis unreachable; messages stored but unanswered | High |
| `billing.ai_allowance_exhausted` | A workspace is out of AI requests | Commercial, not operational |
| `ratelimit.unavailable` | Redis down; limiting is failing open | High |
| `credential.decryption_failed` | A stored credential is unreadable — check key configuration | High |
| `worker.heartbeat_failed` | A loop cannot reach Redis | High |
| `campaign.sweep_failed` | A broadcast is stalled | Medium |
| `request.timed_out` | A handler exceeded its budget while holding a connection | Medium |

Every log line carries `request_id`, and `tenant_id`, `user_id` and `conversation_id` where they apply. Fields whose names suggest secrets are redacted before serialisation, so a token cannot reach the logs even when a payload is logged whole.

---

## What this runbook cannot tell you

Stated plainly, because a runbook that pretends to cover everything is one that gets trusted where it should not be:

- **No production deployment exists yet.** Every procedure here has been executed against local containers and real PostgreSQL. None has been run under load, against real customer traffic, or during an actual incident.
- **There is no backup or restore procedure**, because there is no backup system. `postgres-data` is a Docker volume. Before any real traffic: scheduled `pg_dump`, tested restores, and a documented recovery objective.
- **There is no alerting.** The table above lists what to alert on, not a configured alert.
- **Media files are not replicated.** They live on one host's volume ([ADR-023](../DECISIONS.md)). Losing it loses every attachment customers sent.
- **`usage_events` and `audit_logs` grow without bound.** Neither is swept, deliberately — retention for billing records and audit trails is a legal question, not a disk-space one.
