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
| **Traces** | *Where* did this piece of work spend its time, across processes? | OTLP to a collector, off by default |

They are not substitutes. A metric says the agent queue is 4,000 deep; the log
line with the `tenant_id` says whose messages they are. A readiness probe says
this replica cannot serve; the `wasla_dependency_up` gauge says every replica
cannot serve, which is a different incident. A trace says the four minutes a
customer waited were three retries and one slow inference, which no single log
line in either process can say.

The division of labour between logs and traces is deliberate and is a privacy
decision, not an accident of implementation. **Logs carry identifiers and stay
on your infrastructure. Traces carry structure and go to a third party.** A log
line names the workspace, the user and the conversation because an operator
with database access is already inside the trust boundary. A span names a route
template, a queue and a provider, and never a workspace — because whoever runs
the trace backend is not.

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

### The database pool (in-process, read at scrape time)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_db_pool_checked_out` | gauge | `process_role` |
| `wasla_db_pool_checked_in` | gauge | `process_role` |
| `wasla_db_pool_size` | gauge | `process_role` |
| `wasla_db_pool_max_overflow` | gauge | `process_role` |

Saturation is `checked_out / (size + max_overflow)`, which is why all four are
published rather than only the first.

**`process_role` is `api`, and only `api`.** `/metrics` is served by the API,
and a connection pool is a process-local object: the API can see its own and
has no way at all to see the worker's. Without the label this metric would read
as "the deployment's database pool", which is the one thing it is not. The
label makes the worker's absence visible rather than implied, and leaves room
to add it later without renaming anything.

`pool.overflow()` is deliberately **not** published. SQLAlchemy defines it as
`open_connections - pool_size`, so it reads `-5` on a cold pool of five, and an
operator alerting on "overflow above zero" would be alerting on warmth.

This metric is the visible half of [ADR-080](../DECISIONS.md): an agent turn
releases its connection before calling the provider, and
`tests/integration/test_provider_session_lifetime.py` now scrapes the
exposition while two turns are parked inside a provider call and asserts the
published gauge is the number `checkedout()` reports at that instant.

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
| `wasla_queue_expired_reservations` | gauge | `queue` |
| `wasla_queue_oldest_pending_age_seconds` | gauge | `queue` |
| `wasla_worker_heartbeat_alive` | gauge | `kind` (the eight worker loops) |

`wasla_queue_expired_reservations` is the crash signal. An in-flight job whose
worker has stopped renewing its lease is one nobody is working on, and before
leases existed it was indistinguishable from one somebody *was* working on -
which is how a job could sit in-flight for ever without anything looking wrong.
A steady zero is healthy; anything sustained means a worker is dying faster
than recovery is reclaiming.

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
| `wasla_jobs_total` | counter | `queue`, `outcome` (`succeeded`, `retried`, `dead_lettered`, `recovered`, `quarantined`) |
| `wasla_job_failures_total` | counter | `queue`, `category` |
| `wasla_media_retention_total` | counter | `outcome` (`purged`, `failed`, `pending`) |
| `wasla_media_upload_reconciliation_total` | counter | `outcome` (`finalized`, `missing`, `mismatched`, `unreachable`, `pending`, `quarantined`) |

`wasla_media_retention_total` carries one label with three fixed values, so its
cardinality is three for ever - no tenant, media id, filename, MIME type or
storage key goes anywhere near it (ADR-078). `pending` is a *level* written to a
counter deliberately: what an operator asks of it is "is it going up?", which a
rising sum answers as well as a gauge would while needing no fourth mechanism
for cross-process gauges.

**`pending` is the one worth an alert.** A store that has stopped accepting
deletions is otherwise invisible - the rows are claimed, the sweep reports
itself as having run, and the media store simply does not shrink.

`wasla_media_upload_reconciliation_total` is the same shape for the *write* seam
(ADR-087): one label, six fixed values, no tenant, media id, object key,
filename, hash or bucket. An object's key is committed before the object can
exist, so a process that dies between the two leaves a row naming exactly what
it was writing, and this counter is what that recovery reports.

| Outcome | Means |
| --- | --- |
| `finalized` | An interrupted write was verified and adopted. The attachment is readable |
| `missing` | The object never arrived. The row now owns nothing |
| `mismatched` | An object is at a key Wasla owns and is not what Wasla wrote |
| `unreachable` | The store would not answer. Nothing was decided |
| `pending` | Intents still outstanding, after this pass. A level |
| `quarantined` | Rows still in `mismatched`, after this pass. A level |

**`mismatched` should be zero always, not usually.** It is the one outcome that
cannot be resolved automatically: the object stays where it is, because deleting
it destroys the only evidence of how a foreign object reached that key, and it
is not served, because it is not what the row describes.

`wasla_payment_reconciliation_total` is the same shape again, for the seam where
money moves (ADR-088): one label, seven fixed values, and no workspace, invoice,
payment, provider reference or amount anywhere near it. A collection attempt is
committed before Paymob can be asked to debit a card, so a worker that dies
leaves a row saying a charge may have happened, and this counter is what that
recovery reports.

| Outcome | Means |
| --- | --- |
| `settled` | The provider confirmed the charge. The invoice is paid |
| `failed` | The provider confirmed the decline. The attempt is spent |
| `abandoned` | The provider has no record of it after a full day. The attempt was returned |
| `still_pending` | The provider has not finished. Asked again next sweep |
| `not_found` | No record *yet*, and too soon to believe. Left alone |
| `unreachable` | The provider would not answer. Nothing was decided |
| `pending` | Attempts still unresolved, after this pass. A level |

`wasla_oldest_pending_payment_age_seconds` is the histogram beside it, and it is
the one to alert on. A backlog of one is a callback in flight; a backlog of one
that is a day old is an invoice nobody can collect and possibly a customer who
has already paid.

```
histogram_quantile(1, rate(wasla_oldest_pending_payment_age_seconds_bucket[1h])) > 3600
  an attempt has gone unanswered for an hour: callbacks are probably not arriving

increase(wasla_payment_reconciliation_total{outcome="unreachable"}[15m]) > 0
  Paymob cannot be asked. Nothing is being re-charged, and nothing is settling

increase(wasla_payment_reconciliation_total{outcome="abandoned"}[1d]) > 0
  a charge request never reached Paymob. Worth knowing why

wasla_payment_reconciliation_total{outcome="pending"} rising while
wasla_payment_reconciliation_total{outcome="settled"} stays flat
  PAYMOB_API_KEY is probably unset, so nothing can be resolved at all
```

`not_found` and `abandoned` are the same provider answer read at two different
ages, and keeping them apart is the point: the first is "Paymob has not caught
up", the second is "Paymob never received it". Only elapsed time distinguishes
them, so a fresh `not_found` decides nothing.

**Both of these were written to Redis and never rendered, and the alerts above
could not have fired.** The scrape iterates a declared catalogue, and
`wasla_payment_reconciliation_total` was not in it, so the hash accumulated and
nothing read it. `wasla_oldest_pending_payment_age_seconds` was declared and
still rendered no samples, for a different reason with the same effect: it is
the only cross-process metric with *no labels*, and the field parser read the
empty label string as unparseable. The exposition carried its `# HELP` and
`# TYPE` and never a bucket, which is indistinguishable from "nothing has
happened yet".

That is closed, and it is now closed structurally rather than by adding two
entries: `tests/integration/test_metric_catalogue.py` parses `telemetry.py` for
every metric name the recording paths pass and asserts the catalogues cover
them in both directions — a name written but not declared is a hash nobody
reads, and a name declared but never written is an alert that can never fire.
The discovery asserts what it found before it asserts completeness, because the
failure mode of a discovery test is finding nothing and passing.

**Confirm both render before enabling saved-card renewal.** Not as a formality:
ADR-088 accepts a stated risk — an attempt that can never be resolved keeps a
workspace served indefinitely — on the grounds that the backlog is alertable on
its age, and until these rendered that compensating control did not exist.

```
curl -s localhost:8000/metrics | grep wasla_payment_reconciliation_total
curl -s localhost:8000/metrics | grep wasla_oldest_pending_payment_age_seconds_bucket
```

`unreachable` is deliberately not the same as `missing`, or as `not_found`. A
store or a provider that is down, read as "it is gone", would abandon every
upload - or re-charge every card - in flight during the outage - and an outbound attachment's bytes arrived in a request body that no
longer exists, so abandoning is final.

The last two outcomes are the crash-recovery pair, and they are separate
because an operator reads them differently. `recovered` is the system healing
itself: a worker died holding a job that had done nothing anybody could see, so
it went back for another attempt. `quarantined` is a job recovery *refused* to
heal - an agent turn that had already begun talking to Meta, where trying again
might send a customer a second reply. The first is routine; the second wants a
person to read the conversation.

Their categories are `worker_crashed` and `uncertain_delivery` respectively, so
`wasla_job_failures_total{category="uncertain_delivery"}` is the count of
conversations somebody should look at.

`category` is a `FailureCategory` member — eleven values, fixed in code. Never
an exception message: provider error text can echo a customer's phone number or
a fragment of the request that produced it.

### Providers (through Redis, written by whichever process called out)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_provider_requests_total` | counter | `provider`, `operation`, `outcome` |
| `wasla_provider_request_duration_seconds` | histogram | `provider`, `operation` |

| Provider | Operations | Recorded at |
| --- | --- | --- |
| `openai` | `respond` | `ResponsesClient._post` |
| `whatsapp` | `send_message`, `fetch_media`, `inbound_webhook` | `WhatsAppClient`, the webhook route |
| `paymob` | `checkout`, `moto_intention`, `saved_card_charge`, `refund`, `inquiry_auth`, `transaction_inquiry` | `PaymobProvider._post` |
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

#### How long the call took

**The histogram has no `outcome` label, and it records failures.** Those two
facts belong together. A call that timed out after twenty seconds is the most
important latency this metric holds, and a histogram of successes alone would
report a system getting *faster* as it broke. Splitting the distribution by
outcome would then quadruple the series to answer a question the counter above
already answers — so "how slow is this provider" and "how often does it fail"
each come from the metric shaped for it.

Buckets are `0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60` seconds, chosen from
the timeouts actually configured: JWKS 5 s, WhatsApp and Google token 10 s,
Resend 15 s, Paymob 20 s, OpenAI 60 s. `+Inf` collects an inference that ran
past its timeout with retries above it. The HTTP set is not reused because it
starts at 5 ms and no call across the internet lands there — three of its ten
buckets would be permanently empty.

The duration covers the **whole operation**, including any retries the client
made inside it, because that is what the work actually waited on: a send that
succeeded on its third attempt was slow for the customer however fast the third
attempt was.

Two calls are counted and deliberately *not* timed. `whatsapp`/`inbound_webhook`
is Meta calling us, and has no duration this process could honestly measure.
The email worker's `suppress` outcome, and its permanent failure on a template
that will not render, are counted because that is how an operator reads them —
but neither made a call, and timing a decision not to send would put a
microsecond in the same distribution as a fifteen-second timeout.

**Written non-cumulatively.** A Prometheus histogram wants buckets that each
include everything below them; written straight to Redis that would be one
command per bucket beside every provider call. Instead the bucket an
observation lands in is incremented and the scrape accumulates — two Redis
commands per call instead of eleven, and an identical exposition. A bucket
bound that this release no longer declares is dropped at scrape time rather
than folded into a neighbour, because quietly moving observations between
buckets makes a quantile computed across a bucket change look like an answer.

---

## The data inventory

The three signals leave the process by three different routes, to three
different audiences, under three different contracts. Reviewing them one at a
time is how a field ends up in the one that was not looking.

| Signal | Destination | Read by | Carries | Customer data | Secrets | Bounded |
| --- | --- | --- | --- | --- | --- | --- |
| **Metrics** | `GET /metrics`, Prometheus text 0.0.4 | anything that can reach the port | `route` (template), `method`, `status`, `queue`, `kind`, `provider`, `operation`, `outcome`, `dependency`, `worker` | no | no | yes — closed enumerations, guarded at record time |
| **Logs** | stdout, JSON, shipped by the platform | whoever operates the log store | `event`, `request_id`, `method`, `path`, `status_code`, `duration_ms`, per-event ids, and `error` — a full traceback on any `logger.exception` | **yes, deliberately** — `path`, and the ids an event names | no | no — a log is a stream, not a series |
| **Traces** | OTLP/HTTP to a collector | a trace backend, usually a third party | the ten attributes in `ALLOWED_ATTRIBUTES`, span names, and W3C context | no | no | yes — allowlisted, and names come from route templates |

**Metrics and traces have the same contract; logs deliberately do not.** A log
answers "what happened to *this* request", which is a question that cannot be
answered without an identifier. `app/core/middleware.py` records
`request.url.path` for that reason, and a path segment is usually a lead id or
a conversation id. That is the log store's job and the reason it is a different
system with different access control.

What is *not* in a log, and the distinction that matters: no query string
(`request.url.path`, never `request.url`), no header, no request or response
body, no credential. `tests/integration/test_telemetry_privacy.py` asserts both
halves of this — that the path arrives and the query string does not — so a
change from `.path` to the full URL fails rather than ships.

The `error` field deserves naming on its own, because it is the one that grows
without anybody deciding to add a field. `app/core/logging.py` funnels every
`exc_info` through `formatException`, so any `logger.exception` writes a whole
traceback into the log store — fifteen call sites do. That is the right default
for a signal whose job is to make an incident answerable, and it is safe for a
specific reason worth writing down: `traceback.format_exception` renders each
frame's *source line*, never its locals. A credential held in a local variable
does not reach the log. This is not a property of logging in general — `rich`,
`better_exceptions` and several error-reporting SDKs render locals by default —
so it is pinned by a test rather than left to hold by accident. What a
traceback does carry is the exception's own message, and that is chosen at the
raise site; the guarantee is that a raise site is the only way a value gets
there.

### The canaries

Five strings, each standing for a category that must not be exported, driven
through the paths that would carry them and then looked for in all three
destinations at once:

| Canary | Stands for | Enters through |
| --- | --- | --- |
| `SECRET-JWT-CANARY` | a bearer token, the signing secret | `Authorization`, `Cookie` |
| `SECRET-PAYMOB-CANARY` | a payment provider API key | a header, and a provider exception's message |
| `CUSTOMER-EMAIL-CANARY@example.test` | an end user's email | a query string, a provider exception |
| `CUSTOMER-PHONE-CANARY` | an end user's phone number | a path segment, a job payload |
| `PROMPT-CANARY` | model input or output | a query string, a job payload |

The exception cases are the ones worth having. A payment gateway's error
message is where a merchant reference, a masked card and occasionally a key
actually live, and the SDK's default is to export `str(exception)` as a span
status description and the whole traceback as a span event. `app/core/tracing.py`
passes `record_exception=False` and `set_status_on_exception=False` on every
span it opens and sets the status to `type(error).__name__` — a class name
chosen at the raise site — which is the whole reason that function exists
rather than call sites using the SDK directly.

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

## Backups (from a file, not from this process)

| Metric | Type | Labels |
| --- | --- | --- |
| `wasla_backup_last_success_timestamp_seconds` | gauge | — |
| `wasla_backup_age_seconds` | gauge | — |
| `wasla_backup_failures_total` | counter | `stage` |

**These do not come from an in-process counter, and they could not.** The
backup runs in its own container, on its own schedule, and exits; a counter
incremented inside it vanishes with it. So `backup_postgres.sh` writes a small
JSON status file after its artifact is verified at the off-host destination,
and the API reads that file and publishes the age (ADR-075).

**The age is of the last *durable* backup, not the last dump.** That is the
whole value of the distinction. Alerting on "the newest file in the backup
directory" would call a deployment healthy whose `pg_dump` succeeds nightly and
whose upload has been failing for a week - it has plenty of dumps and no
recovery point. `last_success_at` advances only after the upload is verified,
so an object store that has been unreachable for two days reads as two days
stale, which is what it is.

`stage` is one of `dump`, `validate`, `upload`, `retention` - a bounded set the
script chooses. Never a message and never a bucket name: the status file is
mounted **read-only** into the API and is exactly the sort of thing that gets
pasted into a support ticket.

Absent series are meaningful here. No `BACKUP_STATUS_PATH` configured, or no
status ever written, publishes nothing at all - which says "this deployment
cannot tell you about its backups", a truer alert than a zero that looks like a
fresh one.

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
| **Agent turn, worker crashed before engaging** | Yes, budget permitting | `AGENT_RETRY` | The reservation records stage `reserved`, which means nothing had left the process. Recovery requeues it as the next attempt (ADR-074). |
| **Agent turn, worker crashed after engaging** | **No** | quarantine | The reservation records stage `engaged`, written to Redis *before* the HTTP client was built - so the crash cannot take that knowledge with it. Dead-lettered as `uncertain_delivery` for a person to read. |
| **Ingestion or media, worker crashed** | Yes, budget permitting | `IDEMPOTENT_RETRY` | Safe at any stage: re-ingesting replaces chunks and a file already read is not read again. |
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

**A crash spends an attempt.** A recovered job goes back as `next_attempt`
carrying its history, so a job that kills a worker every time runs out of
budget rather than looping for ever - and one already on its last attempt is
quarantined rather than given a hidden extra one.

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
| Media retention stuck | `increase(wasla_media_retention_total{outcome="pending"}[3d]) > 0 and increase(wasla_media_retention_total{outcome="purged"}[3d]) == 0` | warn | The sweep is claiming files and removing none. The store is refusing deletions and the volume is not shrinking. |
| **A foreign object at our key** | `increase(wasla_media_upload_reconciliation_total{outcome="mismatched"}[1h]) > 0` | **page** | An object exists at a key Wasla owns whose contents are not what Wasla wrote. It is quarantined rather than served or deleted, and only a person can decide what it is. |
| Quarantine not clearing | `wasla_media_upload_reconciliation_total{outcome="quarantined"} > 0` for 24h | warn | A mismatch nobody has looked at. The attachment is unavailable until somebody does. |
| Upload intents growing | `increase(wasla_media_upload_reconciliation_total{outcome="pending"}[1h]) > 0 and increase(wasla_media_upload_reconciliation_total{outcome="finalized"}[1h]) == 0` | warn | Writes are starting and not finishing, and recovery is settling none of them. Usually the object store. |
| Recovery cannot reach the store | `increase(wasla_media_upload_reconciliation_total{outcome="unreachable"}[30m]) > 0` | warn | The pass stopped rather than guessing. Correct behaviour; the store still needs looking at. |
| Objects that never arrived | `increase(wasla_media_upload_reconciliation_total{outcome="missing"}[1h]) > 3` | warn | Writes are being accepted by the application and not landing. Attachments are being lost. |
| **Reservations expiring** | `sum(wasla_queue_expired_reservations) > 0` for 10m | warn | Workers are dying while holding jobs, faster than recovery reclaims them. |
| Jobs being recovered | `increase(wasla_jobs_total{outcome="recovered"}[1h]) > 0` | warn | Something is killing workers. The work is not lost; find out why. |
| **Uncertain deliveries** | `increase(wasla_jobs_total{outcome="quarantined"}[1h]) > 0` | **page** | A worker died mid-send. A customer may or may not have been answered, and only a person can decide. |

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

| Alert | Condition | Severity | What it means |
| --- | --- | --- | --- |
| **Backup stale** | `wasla_backup_age_seconds > 129600` | **page** | No backup has reached off-host storage in 36 hours. The daily schedule allows 24; 36 tolerates one miss and no more. |
| Backup never succeeded | `absent(wasla_backup_age_seconds)` for 1h | **page** | Either no status path is configured or no backup has ever completed. Misconfiguration, not breakage - a different thing to go and look at. |
| Backup failing | `increase(wasla_backup_failures_total[24h]) > 0` | warn | Read `stage` for where: `upload` means the database is fine and the copies are not leaving the host. |
| Upload failing specifically | `increase(wasla_backup_failures_total{stage="upload"}[24h]) > 0` | **page** | Dumps are succeeding and none of them is durable. This is the failure that looks healthy from every other angle. |

Alert on the scheduler too - `systemctl is-failed wasla-backup.service`, or
whatever the platform offers. The metrics say a backup has not *succeeded*; the
unit status says whether anything even tried.

---

## Tracing

Off by default, and the opposite default from metrics for a concrete reason:
metrics are served from the process on request and cost nothing when nobody
scrapes, whereas tracing needs somewhere to send spans and most deployments
have nowhere.

```
TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
OTEL_SERVICE_NAME=            # blank: wasla-api / wasla-worker
OTEL_TRACES_SAMPLER_ARG=1.0   # fraction of traces kept
```

Both processes must get the same values. The whole point is one trace across
both, and a deployment that traced the API but not the worker — or sampled them
differently — would produce traces with their middle missing.
`tests/integration/test_deployment_configuration.py` fails if either compose
service is missing any of the four.

`TRACING_ENABLED=true` with no endpoint **refuses to start**. Exporting into a
void is the failure a deployment discovers during the incident the traces were
for.

### What a trace looks like

```
HTTP POST /webhooks/whatsapp          SERVER   wasla-api
├── db.session                        INTERNAL
└── queue.publish agent               PRODUCER      ── traceparent into the job
                                                       envelope, in Redis
worker.agent                          CONSUMER wasla-worker   attempt 1
├── db.session                        INTERNAL   ← ends before the provider
├── provider.openai.respond           CLIENT       starts. That is ADR-080,
├── db.session                        INTERNAL     visible rather than trusted.
└── provider.whatsapp.send_message    CLIENT
```

A retry is a **new span in the same trace**, carrying `wasla.job_attempt=2`.
Reusing one span across attempts would overwrite the history of the first;
starting a new trace per attempt would lose the connection to the request that
queued the work. Neither is what "why did this customer wait four minutes"
needs.

The trace context travels in the job envelope as W3C `traceparent` and
`tracestate`, and nothing else — not a header bag, not baggage, not an
application field. It survives retry, delay, crash recovery and the dead-letter
list, because it rides on the envelope those all preserve.

### It is never load-bearing

The trace context is **not** the job's identity, its retry budget or its
deduplication key. Those are the payload, `attempt`, and a unique constraint in
PostgreSQL, and none of them consults the carrier. A missing, truncated,
hostile, or older-release carrier means the attempt starts its own trace and
runs exactly as it would have.

Likewise the exporter. A collector that is down, slow or refusing loses spans
and logs about it, and cannot fail a request, fail a job, or alter a payment —
`BatchSpanProcessor` exports on its own thread and contains its own failures.
`tests/integration/test_trace_isolation.py` asserts this with an exporter that
*raises*, which is the harsher case.

### What a trace backend can see, and what it cannot

Whoever operates the collector reads span names, timings and attributes for
every request, indefinitely, in a system with none of Wasla's tenant isolation.
So the attribute set is an allowlist, and it is short:

| Attribute | Domain |
| --- | --- |
| `http.request.method` | seven verbs, or `OTHER` |
| `http.route` | a route template, or `__unmatched__` |
| `http.response.status_code` | an integer |
| `wasla.queue` | `agent`, `ingestion`, `media` |
| `wasla.job_attempt` | a small integer |
| `wasla.job_outcome` | `succeeded`, `retried`, `dead_lettered`, `lost` |
| `wasla.provider` | `openai`, `whatsapp`, `paymob`, `email` |
| `wasla.provider_operation` | a call-site constant |
| `wasla.provider_outcome` | the four `CallOutcome` values |
| `db.system` | `postgresql` |

**Never exported, anywhere:** a JWT or any bearer token; an `Authorization` or
`Cookie` header; any header at all; a query string; a request or response body;
a prompt, a model's answer or tool arguments; an email address; a phone number;
a message body; an OAuth code or state; Paymob or S3 credentials; a media
storage key or filename; a workspace, user, conversation, contact, lead,
invoice, payment or media identifier; a SQL statement or its parameters; an
exception message or stack trace.

Three things enforce that rather than three people remembering it:

1. **No auto-instrumentation.** Not `opentelemetry-instrumentation-fastapi`,
   `-httpx` or `-sqlalchemy`. FastAPI's exports the requested path and query
   string, httpx's exports full request URLs, SQLAlchemy's exports statement
   text. In each case the privacy control would be a setting in a package this
   repository does not own. The four span kinds are written by hand instead.
2. **`record_exception=False` and `set_status_on_exception=False`** on every
   span. Both default to *true* and both put `str(exception)` into the exported
   span. What is recorded instead is the exception's class name, which is a
   code-defined identifier. A failing span is `ERROR` with a description of
   `RuntimeError` and no events.
3. **`tests/unit/test_trace_privacy.py`** checks every attribute a realistic
   flow produces against the allowlist — so a new attribute fails whatever it
   is called — and separately pushes distinctive canary values through the
   parts of the system that handle secrets and customer content and searches
   the exported spans for them verbatim.

**No inbound trace context is honoured.** Every API request starts a new trace.
Wasla's HTTP callers are a browser frontend and Meta, Paymob and Resend
webhooks; none participates in Wasla's traces and all are outside the trust
boundary. Accepting a `traceparent` from the internet would let a stranger
choose trace identifiers, merge unrelated requests into one trace, and write up
to 512 bytes of `tracestate` into every span they produced.

### What the resource says about your machines

`Resource.create` attaches `service.name`, a per-process `service.instance.id`
(a random UUID, so replicas are distinguishable without being named), and the
SDK's own name, language and version. **No hostname, no IP address, no process
id, no command line** — OpenTelemetry's host, process and container detectors
are opt-in and are deliberately not opted into.

That is worth knowing rather than assuming, because a trace backend is usually
somebody else's, and a deployment's topology is not something to hand a third
party by default. If a deployment *wants* host attributes, `OTEL_RESOURCE_ATTRIBUTES`
is read by the SDK and is the place to add them deliberately.

### Sampling

`ParentBased(TraceIdRatioBased(OTEL_TRACES_SAMPLER_ARG))`. The decision is made
once per trace and inherited downstream, so the API and the worker never
disagree about whether a trace is being kept — without that, one could sample a
request in and the other sample the same trace out, producing a trace missing
its middle.

One number for the whole deployment. Sampling that varied by workspace, route
or payment would put two populations in one dataset with no way to tell which
one you were looking at, and would make traces a per-tenant signal, which is
the same cardinality mistake the metrics rule exists to prevent.

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

- **No worker database-pool metric.** The worker holds its own pool and
  publishes no HTTP; `process_role="api"` is the whole of what is exported. A
  worker running out of connections is visible as queue depth and job latency
  rather than directly.
- **No trace backend is shipped or verified here.** The spans are produced and
  proved against an in-memory exporter; whether a particular collector accepts
  them is a deployment question this repository has not answered.
- **No per-workspace metrics, and there will not be any.** That is the
  cardinality rule, not an oversight. Per-workspace usage is a database
  question and the platform analytics API answers it.
- **A blocked event loop looks like a live worker.** Lease renewal and the
  heartbeat both assert the same thing - the process is up and scheduling - so
  a loop wedged by a genuinely blocking call keeps renewing and is never
  reclaimed. Every loop here is I/O-bound async, so that is a bug rather than a
  state, but it is a bug this design cannot detect.
- **No dashboards in this repository.** The metric names are stable and
  documented above; what an operator draws with them belongs with the
  monitoring stack they chose.
- **None of this has run in production**, because there is no production. It
  has been exercised against local containers and by the test suite.
