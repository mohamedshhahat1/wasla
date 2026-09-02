# Observability

Before this phase the honest answer to "how would you know Wasla was broken?"
was "a customer would tell you". The logs were good, the health probes were
honest, and there was no metric, no alert and no way to see that the
dead-letter list — the one place failed customer work accumulates — was
growing.

This page is what changed, what it costs, and what it still does not cover.

---

## The three layers, and what each is for

| Layer | Answers | Where |
| --- | --- | --- |
| **Structured logs** | *Why* did this one thing fail? | JSON on stdout, correlated by `request_id` |
| **Health probes** | Should this container be restarted, or taken out of the load balancer? | `/health/live`, `/health/ready`, `worker-health` |
| **Metrics** | Is something wrong *right now*, and has it been wrong for long? | `/metrics` |

They are not substitutes. A metric says the agent queue is 4,000 deep; the log
line with the `tenant_id` says whose messages they are. A readiness probe says
this replica cannot serve; the `wasla_dependency_up` gauge says every replica
cannot serve, which is a different incident.

---

## The exposition

Prometheus text format 0.0.4, at `GET /metrics`, written by hand rather than
pulled in ([ADR-072](../DECISIONS.md)). Anything that speaks that format
scrapes it: Prometheus, an OpenTelemetry collector, Grafana Agent,
VictoriaMetrics. Nothing here commits the deployment to one.

### Who can read it

**Nobody from the internet, and that is the access control.**

- The API container publishes no port. In `docker-compose.prod.yml` only nginx
  is reachable from outside the `internal` network.
- `nginx.conf` answers `404` for `/metrics` on the public listener rather than
  proxying it. Without that block the catch-all `location /` would forward it.
- A scraper reaches it by sitting on the internal network, which is the same
  access control everything else the API serves already relies on.
- `METRICS_ENABLED=false` turns the endpoint off entirely — `404`, not `403`,
  because "this deployment serves no metrics" is the true answer and a `403`
  would confirm the endpoint exists.

A shared bearer token was considered and rejected ([ADR-070](../DECISIONS.md)):
every scraper in a deployment would hold the same one, and it would protect a
document that carries no customer data by construction. A credential whose loss
costs nothing gets treated as if losing it costs nothing.

### Where the numbers come from

Two mechanisms, and which one a signal uses follows from one question: does the
process that produces it serve HTTP?

- **In-process**, for the API's own request path — HTTP rate, latency,
  dependency health, unhandled errors. Each API replica is its own scrape
  target, which is the ordinary Prometheus model.
- **Through Redis**, for everything the worker produces — job outcomes and
  provider calls. The worker deliberately serves no HTTP; its health probe is a
  *command* for exactly that reason. Giving it a metrics listener would hand an
  attack surface to the container holding the Meta token and the OpenAI key
  ([ADR-069](../DECISIONS.md)). Queue depths and heartbeats are read live from
  Redis at scrape time, because they are gauges of what is true now.

Counters written into Redis survive a restart and reset if Redis is flushed. A
scraper handles that natively — it notices the value fell.

---

## The catalogue

### HTTP (in-process, per replica)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_http_requests_total` | counter | `method`, `route`, `status` |
| `wasla_http_request_duration_seconds` | histogram | `method`, `route` |
| `wasla_http_requests_in_flight` | gauge | — |
| `wasla_unhandled_errors_total` | counter | — |

`route` is the **matched route template** — `/api/v1/leads/{lead_id}` — never
the requested path. A request matching no route is counted as `__unmatched__`,
so a scanner cannot name a time series. `status` is a class (`2xx`…`5xx`), not
a code. `method` collapses anything outside the seven real verbs to `OTHER`.

### Dependencies (in-process, written by the readiness probe)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_dependency_up` | gauge | `dependency` (`postgresql`, `redis`) |
| `wasla_dependency_check_failures_total` | counter | `dependency` |

The gauge is what the last probe found; the counter is how many have failed.
The pair distinguishes "PostgreSQL is down now" from "PostgreSQL has been
flapping all morning", and only the second explains the latency somebody is
asking about.

### Queues and workers (read live from Redis)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_queue_pending_jobs` | gauge | `queue` (`agent`, `ingestion`, `media`) |
| `wasla_queue_inflight_jobs` | gauge | `queue` |
| `wasla_queue_delayed_jobs` | gauge | `queue` |
| `wasla_queue_dead_letter_jobs` | gauge | `queue` |
| `wasla_queue_oldest_pending_age_seconds` | gauge | `queue` |
| `wasla_worker_heartbeat_alive` | gauge | `kind` (the seven worker loops) |

`oldest_pending_age_seconds` is **absent** for an empty queue rather than zero:
zero would read as "nothing has been waiting long", which is a claim about
latency rather than about emptiness, and it is the claim an alert would act on.
A retried job carries its *original* enqueue time forward, so this measures how
long the customer has waited rather than how long since the last attempt failed.

`wasla_worker_heartbeat_alive` reads the same keys the container's
`worker-health` command reads, so the health column and the alert cannot
disagree. It proves the process is up and its event loop is scheduling — not
that any particular loop is making progress. Progress is what the queue depth
and age gauges are for, and needing both is why both exist.

### Jobs (through Redis, written by the worker)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_jobs_total` | counter | `queue`, `outcome` (`succeeded`, `retried`, `dead_lettered`) |
| `wasla_job_failures_total` | counter | `queue`, `category` |

`category` is a `FailureCategory` member — eleven values, fixed in code. Never
an exception message: provider error text can echo a customer's phone number or
a fragment of the request that produced it.

### Providers (through Redis, written by whichever process called out)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_provider_requests_total` | counter | `provider`, `operation`, `outcome` |

| Provider | Operations | Recorded at |
| --- | --- | --- |
| `openai` | `respond` | `ResponsesClient._post` |
| `whatsapp` | `send_message`, `fetch_media`, `inbound_webhook` | `WhatsAppClient`, the webhook route |
| `paymob` | `checkout`, `moto_intention`, `saved_card_charge`, `refund` | `PaymobProvider._post` |
| `email` | `deliver`, `suppress` | `EmailWorker` |

`outcome` is one of `success`, `failure`, `rate_limited`, `unavailable`. Four
values and no more — a provider's own error catalogue is unbounded, changes
without notice, and can echo the request.

Recorded inside each client's retry loop, because that is where the outcomes
are already distinguished: a 429, a 5xx, a timeout and a refused destination
are four different operational problems and only the loop can tell them apart.

**Billing and AI spend are not here.** Token usage, AI request counts and
message counts are metered into `usage_events` and read through the platform
analytics API, which is the number a bill is computed from. Publishing a second
tally would be a second number to reconcile, and the first one to be wrong
during an argument about an invoice.

---

## Cardinality and privacy

The rule is enforced in code, not by convention. `app/core/metrics.py` refuses
a label value that looks like an identifier — a UUID in any spelling, anything
containing `@`, a phone-shaped run of digits, or a value longer than 96
characters — at the moment a sample is recorded. It raises; every call site
reaches metrics through `app.core.telemetry`, which swallows, so the guard is
testable and can never fail a request.

Label *names* are fixed at metric declaration, so a well-meaning
`extra={"tenant_id": ...}` habit cannot reach a series either.

**These never appear as a label, anywhere:** workspace, user, membership,
conversation, message, contact, lead, document, invoice, payment or media
identifiers; an email address; a phone number; a URL path parameter; a provider
error string; a model's output; a plan price; a payment amount.

`tests/unit/test_metrics.py` proves the guard refuses each shape and admits
every legitimate category. `tests/integration/test_metrics_endpoint.py` scans
the whole rendered exposition for anything UUID-shaped after driving real
requests through the application, which is the check that survives somebody
adding an instrumentation call two years from now.

### Cost

The bounded worst case is roughly: 108 route templates × 7 methods × 5 status
classes for the request counter, plus the same route/method pairs for the
histogram's ten buckets. In practice a deployment sees a few hundred series,
because most routes take one or two methods and answer two or three status
classes. Everything else is single digits: three queues, seven worker kinds,
eleven failure categories, four providers.

---

## The retry safety matrix

Which asynchronous work may be repeated, and what makes it safe. This is the
table that decided the policies in `app/workers/retry.py`; it is here rather
than only in an ADR because it is the first thing to check when adding a job
type.

| Operation | Retried? | Bounded by | Why it is (or is not) safe |
| --- | --- | --- | --- |
| **Ingestion job** | Yes, 5 attempts | `IDEMPOTENT_RETRY`, 2s→60s | Re-ingesting *replaces* a document's chunks rather than appending. A duplicate costs embedding calls and changes nothing. |
| **Media job** | Yes, 5 attempts | `IDEMPOTENT_RETRY`, 2s→60s | A file already stored is not downloaded again and one already read is not read again. The row is the idempotency key. |
| **Agent turn, before the provider** | Yes, 3 attempts | `AGENT_RETRY`, 2s→30s | Loading a workspace, reading an allowance and looking up an agent happen inside a transaction that rolls back. Nothing has left the process. |
| **Agent turn, after the provider** | **No** | `NO_RETRY` | May have reserved an allowance, called OpenAI, or sent a WhatsApp message. The Cloud API takes no idempotency key, so a repeat is a second answer to one question. `_TurnProgress.engaged` is the boundary. |
| **Meta send (HTTP)** | Only 429 and connect errors | `WhatsAppClient`, 3 attempts | Those two prove nothing was accepted. A 5xx or a read timeout may have landed, and a duplicate reply is worse than a failure. Unchanged by this phase. |
| **Meta media fetch (HTTP)** | Everything transient | `WhatsAppClient`, 3 attempts | A repeated read has no customer-visible effect. |
| **OpenAI call (HTTP)** | 429, 5xx, transport | `ResponsesClient`, 3 attempts | A duplicated inference costs money and is never seen. `store: false`, so nothing accumulates provider-side. |
| **Paymob intention / checkout** | Caller's decision, marked `retryable` | `ProviderError.retryable` | Keyed on Wasla's own unique reference, so the provider collapses a repeat. A refused *destination* is explicitly not retryable — nothing was sent and the same aim gets the same refusal. |
| **Paymob recurring charge** | Yes, bounded | `MAX_COLLECTION_ATTEMPTS` + `RETRY_BACKOFF` | A payment row is claimed under `UNIQUE(tenant_id, idempotency_key)` keyed on invoice *and attempt number*, and the attempt is counted **before** the provider is called — so a request that times out has still been made, and is still counted. Untouched by this phase. |
| **Paymob callback** | Provider retries; we dedupe | `UNIQUE(provider, provider_event_id)` | Settlement is claimed inside a savepoint. A redelivery is recognised as a duplicate and answers 200. |
| **Email delivery** | Yes, `EMAIL_MAX_ATTEMPTS` | Outbox row, 30s→1h | At-least-once by design (ADR-042). Rows are claimed `FOR UPDATE SKIP LOCKED`, the claim commits before any network call, and each message sends in its own transaction. |
| **Billing sweep** | Next sweep | The row's own state | A period end is a row whose moment has arrived; re-reading it is the operation. |
| **Follow-up sweep** | Next sweep | `FOR UPDATE SKIP LOCKED` | Two replicas step over each other rather than sending the customer the same nudge twice. |
| **Campaign sweep** | Next sweep | Recipients claimed under lock | The rate limit lives on the row, not in the process, so a restart cannot double it. |
| **WhatsApp webhook** | Meta retries | `UNIQUE` on Meta's message id | Idempotent event storage; a redelivery stores nothing new. |

**Nothing in this phase changed how money moves.** The Paymob rows are here for
completeness: recurring collection already had bounded attempts, backoff and an
attempt-numbered idempotency key before P1-C, and the queue retry work does not
touch it. Financial settlement runs on the billing worker's PostgreSQL sweep,
not on any Redis queue, so no queue retry can execute it twice.

---

## Alerting

**Nothing here ships a configured alert.** There is no Alertmanager in this
stack and no monitoring vendor, so what follows is a set of expressions
against the metrics that actually exist, for whatever an operator points at
`/metrics`. They are recommendations, not something that is running.

### Availability

| Alert | Condition | Severity | What it means |
| --- | --- | --- | --- |
| API 5xx rate | `sum(rate(wasla_http_requests_total{status="5xx"}[5m])) / sum(rate(wasla_http_requests_total[5m])) > 0.02` for 5m | **page** | More than 2% of requests are failing. |
| API latency | `histogram_quantile(0.95, sum by (le) (rate(wasla_http_request_duration_seconds_bucket[5m]))) > 2` for 10m | warn | p95 above two seconds. |
| Unhandled errors | `increase(wasla_unhandled_errors_total[15m]) > 0` | warn | Something raised that nobody anticipated; find it in the logs by `request.unhandled_error`. |
| API absent | `absent(wasla_http_requests_total)` for 5m | **page** | No replica is being scraped. |

### Dependencies

| Alert | Condition | Severity | What it means |
| --- | --- | --- | --- |
| PostgreSQL not ready | `min(wasla_dependency_up{dependency="postgresql"}) == 0` for 2m | **page** | Readiness is failing; the API is serving 503s. |
| Redis not ready | `min(wasla_dependency_up{dependency="redis"}) == 0` for 2m | **page** | Queues are not draining and refresh/logout answers 503 ([ADR-064](../DECISIONS.md)). |
| Dependency flapping | `increase(wasla_dependency_check_failures_total[30m]) > 5` | warn | Intermittent, which is worse to diagnose than down. |

### Workers and queues

| Alert | Condition | Severity | What it means |
| --- | --- | --- | --- |
| Worker loop stopped | `min by (kind) (wasla_worker_heartbeat_alive) == 0` for 3m | **page** | A loop is not beating. Nothing on its queue is being done. |
| Queue backing up | `wasla_queue_pending_jobs{queue="agent"} > 500` for 10m | **page** | Customers are waiting. Check the worker first, then the provider. |
| Queue latency | `wasla_queue_oldest_pending_age_seconds{queue="agent"} > 300` for 5m | **page** | Somebody has been waiting five minutes for a reply. |
| Ingestion backlog | `wasla_queue_oldest_pending_age_seconds{queue="ingestion"} > 1800` for 10m | warn | Documents are not becoming searchable. |
| **Dead letters appearing** | `increase(wasla_queue_dead_letter_jobs[1h]) > 0` | warn | Work stopped being retried. **This is the signal that did not exist before.** |
| Dead letters accumulating | `wasla_queue_dead_letter_jobs > 20` | **page** | A systemic failure, not a bad job. |
| Retry storm | `sum(rate(wasla_jobs_total{outcome="retried"}[10m])) > sum(rate(wasla_jobs_total{outcome="succeeded"}[10m]))` for 15m | warn | More work is being retried than finished. |

### Providers

| Alert | Condition | Severity | What it means |
| --- | --- | --- | --- |
| OpenAI failing | `sum(rate(wasla_provider_requests_total{provider="openai",outcome!="success"}[10m])) / sum(rate(wasla_provider_requests_total{provider="openai"}[10m])) > 0.2` for 10m | **page** | One in five inferences is failing; agents are not answering. |
| Meta send failing | `sum(rate(wasla_provider_requests_total{provider="whatsapp",operation="send_message",outcome!="success"}[10m])) > 0.1` for 10m | **page** | Replies are not reaching customers. |
| Meta inbound silent | `sum(increase(wasla_provider_requests_total{provider="whatsapp",operation="inbound_webhook"}[30m])) == 0` | warn | Meta has stopped calling. Tune the window to the deployment's quietest hour. |
| Paymob failing | `sum(rate(wasla_provider_requests_total{provider="paymob",outcome="failure"}[15m])) > 0` for 15m | **page** | Money is not being collected. |
| Email failing | `sum(rate(wasla_provider_requests_total{provider="email",operation="deliver",outcome="failure"}[30m])) > 0.05` | warn | Verification codes and password resets are not arriving. |
| Rate limited | `sum by (provider) (rate(wasla_provider_requests_total{outcome="rate_limited"}[10m])) > 0` for 15m | warn | Sustained throttling; the backoff is working and the throughput is not. |

### Backups

Not a metric — the backup runs outside the application, so its signal is the
scheduler's. Alert on the cron job's exit status, and on the age of the newest
file in `BACKUP_DIR` exceeding a day and a half.

---

## Error monitoring

**No external provider has been added.** Structured logs and metrics are the
integration point.

The reasoning: an error tracker earns its place by grouping and deduplicating
exceptions across a fleet, and this deployment has one API container and one
worker. What it *costs* is a third party receiving stack frames from a process
that holds the Meta token, the OpenAI key, the Paymob secret and every
customer's conversation — and the redaction that keeps those out of the logs
today is written against this application's own log records, not against an
SDK's automatic context capture. Installing a vendor to satisfy a checklist,
and then having to prove it does not exfiltrate a card token, is worse than not
installing one.

What exists instead:

- Every unhandled exception is logged as `request.unhandled_error` with the
  request id, method and path, and the traceback — with the redaction pass
  applied ([docs/SECURITY.md](SECURITY.md)).
- `wasla_unhandled_errors_total` makes them alertable.
- Worker failures are `worker.job_retry_scheduled` and
  `worker.job_dead_lettered`, both carrying `tenant_id` and a bounded failure
  category, and both counted.

**If a provider is added later**, the requirements are: disabled by default;
DSN optional; `send_default_pii=False`; no request bodies; no local variables;
a `before_send` that runs the existing redaction; and initialisation that
cannot fail a request or a worker loop if the provider is unreachable. The
place to hook it is `register_exception_handlers` in `app/core/exceptions.py`
and `run_forever` in each worker — both already funnel every unhandled failure
through one function.

---

## What this still does not give you

- **No tracing.** A request that crosses API → Redis → worker → OpenAI → Meta
  is correlatable by `request_id` only as far as the enqueue; the worker leg
  does not carry it. OpenTelemetry is P2.
- **No provider latency histograms.** Provider calls are counted by outcome,
  not timed. The time is in the log line for the call and, for AI, in
  `usage_events`.
- **No per-workspace metrics, and there will not be any.** That is the
  cardinality rule, not an oversight. Per-workspace usage is a database
  question and the platform analytics API answers it.
- **No dashboards in this repository.** The metric names are stable and
  documented above; what an operator draws with them belongs with the
  monitoring stack they chose.
- **None of this has run in production**, because there is no production. It
  has been exercised against local containers and by the test suite.
