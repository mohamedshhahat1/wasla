# Observability

**Status: Implemented** — structured logging, request IDs, health endpoints, a
Prometheus exposition, a scraper, alert rules, Alertmanager and a receiver
configuration.

Scope: what the platform emits, what reads it, and what wakes somebody up.

## The gap this closes

`/metrics` has served a correct Prometheus document since ADR-070, and until
recently **nothing scraped it**. That is not monitoring — it is a number in a
file. The consequence was concrete: six account-security endpoints returned 500
with a full rollback for as long as they did (AUTH-01) while
`wasla_http_requests_total{status="5xx"}` counted every one of them, because no
scraper read the counter and no rule evaluated it.

The pipeline is now complete end to end:

```
application /metrics  →  Prometheus (scrape + rules)  →  Alertmanager  →  receiver
```

Each arrow is configured in `deploy/monitoring/` and wired in
`docker-compose.prod.yml`. What is *verified* and what is not is stated at the
bottom of this document, deliberately.

## What is exposed

`GET /metrics`, in Prometheus text format. Unauthenticated, and that is
argued rather than overlooked (ADR-070): the API container publishes no port,
and `nginx.conf` refuses `/metrics` on the public listener, so a scraper reaches
it by being on the internal network — the same access control everything else
the API serves already relies on.

**No metric carries an identifier as a label.** `app/core/metrics.py` refuses
one, and there is a test that proves it. No tenant id, no user id, no email
address, no provider reference. The cardinality of every series here is bounded
by the number of routes, operations and outcomes, not by the number of
customers.

| Metric | Type | Labels | What it answers |
|---|---|---|---|
| `wasla_http_requests_total` | counter | `method`, `route`, `status` | Is a route failing? `route` is the FastAPI *template*, never the path. |
| `wasla_http_request_duration_seconds` | histogram | `method`, `route` | Is it slow? |
| `wasla_unhandled_errors_total` | counter | — | Did something raise that nobody expected? |
| `wasla_auth_security_events_total` | counter | `event`, `outcome`, `reason` | Login, refresh, token and verification outcomes. |
| `wasla_lifecycle_operations_total` | counter | `operation`, `outcome` | Account and workspace lifecycle: creation, deletion, transfer, suspension, purge, re-authentication, billing wind-down. |
| `wasla_orphaned_workspaces` | gauge | — | **A state invariant, counted at scrape time**: active workspaces with no active owner. Should always be zero. |
| `wasla_db_pool_*` | gauge | `process_role` | Connection pool depth. |
| `wasla_jobs_total`, `wasla_job_failures_total` | counter | `queue`, `outcome` / `category` | Worker throughput and failures. |
| `wasla_provider_requests_total` | counter | `provider`, `operation`, `outcome` | External calls. |
| `wasla_queue_*` | gauge | `queue` | Pending, in-flight, delayed, dead-lettered, expired reservations, and the age of the oldest waiting job. |
| `wasla_unprocessed_inbound_events` | gauge | — | **A state invariant**: inbound stored whose agent or media handoff never reached a queue. Should be zero. |
| `wasla_unprocessed_inbound_oldest_age_seconds` | gauge | — | Whether that backlog is being drained or is stuck. |
| `wasla_unresolved_outbound_messages` | gauge | — | **A state invariant**: sends Meta may have delivered, whose outcome is unknown. |
| `wasla_oldest_unresolved_outbound_age_seconds` | gauge | — | Whether the oldest is a send in flight or one that broke an hour ago. |

`wasla_orphaned_workspaces` is worth singling out, and the two messaging
gauges beside it are the same shape. Every other metric here counts an *event* —
a path somebody instrumented was taken. These run a query and count a *state*,
so they notice an invariant violation however it was produced, including by a
defect nobody anticipated and including by somebody's SQL. That is the
difference between "the code we wrote reported a problem" and "the world has a
problem".

The two messaging gauges share their cutoff with the code that acts on them:
`wasla_unprocessed_inbound_events` uses `InboundRecoveryWorker`'s own grace
period, imported rather than restated, so an operator alerting on a backlog and
a sweeper draining one are looking at the same set of events.

## Alert rules

`deploy/monitoring/alerts.yml`, in three groups.

| Alert | Fires when | Severity |
|---|---|---|
| `LifecycleEndpointErrors` | Any lifecycle or account-security route returns 5xx for 10m | critical |
| `OrphanedWorkspace` | `wasla_orphaned_workspaces > 0` for 15m | critical |
| `LifecycleOperationFailureRate` | >50% of lifecycle operations failing for 15m | warning |
| `WorkspaceBillingWindDownFailing` | Subscription cancellation fails during deletion | critical |
| `WorkspacePurgeFailing` | The retention sweep errors for 2h | warning |
| `ReauthenticationFailureSpike` | Google re-auth proofs repeatedly refused | warning |
| `ApplicationUnhandledErrors` | Unhandled exceptions above a low rate | warning |
| `ScrapeTargetDown` | Prometheus cannot reach the API for 5m | critical |

### Messaging

Added after an audit found that *nothing in this file concerned WhatsApp* —
although every metric below already existed and was already scraped. The
asymmetry with the email webhook, which did have an alert, is what marked it as
an oversight rather than a decision.

| Alert | Fires when | Severity |
|---|---|---|
| `WhatsAppInboundStopped` | No inbound webhook for 30m, on a deployment that had traffic today | critical |
| `WhatsAppWebhookSignatureFailures` | Sustained signature refusals | critical |
| `WhatsAppSendFailureRate` | >20% of sends failing for 15m | warning |
| `WhatsAppRateLimited` | Sustained 429s from Meta | warning |
| `UnprocessedInboundBacklog` | Stored inbound still owing work for 15m | critical |
| `UnresolvedOutboundSends` | A send unconfirmed for over an hour | warning |
| `QueueJobsStuck` | The oldest unclaimed job is over 15m old | warning |
| `DeadLetterGrowth` | Jobs are being dead-lettered | warning |

**`WhatsAppInboundStopped` is the one with no other symptom.** If Meta disables
the subscription, everything looks healthy from inside: no errors, no queue
depth, no failed jobs, just an inbox that quietly stops filling. It is guarded
on the deployment having received traffic in the last 24 hours, which is the
honest version of "during business hours" for a product that does not know its
customers' hours — a new or dormant deployment does not page on an empty night.

`UnresolvedOutboundSends` fires on the *age* of the oldest unresolved send
rather than on existence, because a handful of rows seconds old is every send
currently in flight. What it is reporting is deliberately never fixed
automatically: see the runbook, and [ADR-093](../DECISIONS.md) for why a retry
is the one action that cannot be taken back.

**`ScrapeTargetDown` is what makes every other rule mean anything.** Without it,
"no alerts firing" and "nothing being scraped" look identical from the outside —
which is precisely the state this whole document exists to get out of. It also
inhibits the rest: when the scraper cannot reach the application, every other
rule is evaluating absent data, and suppressing them keeps the one actionable
alert from being buried under the alerts it caused.

**Every rule is tested twice** — once under its threshold and once over it.
A rule only ever shown firing has not been shown to discriminate, and a
threshold nobody tested from below is a threshold that pages on ordinary
traffic. `promtool test rules deploy/monitoring/tests/alerts_test.yml` runs
them, and CI runs it.

Every `for:` duration is deliberately non-zero. A single 5xx during a deploy is
not an incident, and a rule that pages on one teaches people to ignore the
pager, which is worse than having no rule.

## Receiver

`deploy/monitoring/alertmanager.yml`. Two receivers split by what somebody
should do: `critical` (look now, repeat hourly) and `warning` (investigate
during the day, repeat every four hours).

**The Slack webhook is read from a file, not an environment variable.** An
earlier draft used `${ALERTMANAGER_SLACK_WEBHOOK_URL}` and would have shipped a
container that refused to start — Alertmanager does not expand environment
variables in its configuration, and `amtool check-config` rejects it with
`unsupported scheme ""`. A file is the better answer anyway: an environment
variable is visible in `docker inspect` and in `/proc/<pid>/environ` to anything
that can read the process.

```
ALERTMANAGER_SLACK_WEBHOOK_FILE=/run/secrets/wasla_slack_webhook
```

A Slack webhook URL **is a credential** — anybody holding it can post to the
channel — so it is handled as one and never committed.

**A deployment with no receiver configured still works.** The default is a
committed *empty* placeholder, because Docker Compose refuses to start a stack
whose secret file is missing. Alertmanager starts, Prometheus scrapes, every
rule evaluates, and firing alerts are visible in Alertmanager's own UI and API;
only the final delivery fails, and it says so in its own logs. That ordering is
deliberate: an operator who has not yet chosen a chat tool gets working
monitoring rather than a container that will not boot.

## Reaching the dashboards

Neither Prometheus nor Alertmanager publishes a port. A published Prometheus is
an unauthenticated read of the deployment's operational shape; a published
Alertmanager is an unauthenticated way to *silence* alerts. Reach them by
tunnelling:

```bash
ssh -L 9090:localhost:9090 -L 9093:localhost:9093 <host>
docker compose -f docker-compose.prod.yml exec prometheus \
  wget -qO- http://localhost:9090/-/healthy
```

## What has actually been verified

Stated exactly, because "alerting is configured" and "somebody finds out" are
different claims and this repository has been burned by the difference.

**Verified:**

- `promtool check config` — the Prometheus configuration parses and its rule
  file loads (8 rules).
- `promtool test rules` — every rule was driven against synthetic series:
  `LifecycleEndpointErrors`, `OrphanedWorkspace` and `ScrapeTargetDown` each
  stay inactive before their `for:` duration, **enter firing state** once it
  passes, and **clear** when the condition stops.
- `amtool check-config` — the Alertmanager configuration is valid: 2 receivers,
  1 inhibit rule.
- **A real scrape.** A Prometheus container was run against the real application
  container on a shared network; the target reported `health: up`, all 8 rules
  loaded into the running server, and `up{job="wasla-api"}` returned `1`.
- `docker compose config` — the production stack including both services
  resolves.

All five run in CI (`monitoring` job, plus the existing suites).

**Not verified: delivery to a real receiver.** No Slack webhook credential was
available, and none was used. The configuration is valid and the routing is
declared; whether a message arrives in a particular workspace's channel has not
been demonstrated. An operator should confirm it once, with:

```bash
docker compose -f docker-compose.prod.yml exec alertmanager \
  amtool --alertmanager.url=http://localhost:9093 alert add \
  alertname=DeliveryTest severity=critical component=lifecycle
```

and then resolve it. Until somebody does that, treat delivery as unproven.

## Logging

Structured JSON, one object per line, with `request_id` on every line a request
produced. Never contains passwords, tokens, reset codes, OAuth state or customer
message content — `docs/SECURITY.md` states the rule and there are tests for it.

Lifecycle operations log at `info`, except two that log at `warning` because
they need somebody to do something afterwards:
`account.deleted_orphaned_workspaces` (a workspace was left ownerless by a
platform deletion) and `workspace.ownership_repaired` (staff reached into a
customer's roster).

## Health

| Endpoint | Asserts |
|---|---|
| `GET /health/live` | The process is running. Depends on nothing. |
| `GET /health/ready` | PostgreSQL and Redis are reachable. |
| `GET /health` | Both, with detail. |

Liveness deliberately does not touch PostgreSQL: a database blip must not cause
an orchestrator to restart every application container.

## What is still absent

- **No Grafana.** The metrics are dimensioned for it and no dashboards are
  shipped. Alerting was the gap worth closing first: a dashboard tells somebody
  who is already looking, and an alert tells somebody who is not.
- **No tracing backend.** OpenTelemetry is wired (ADR-083) and exports to an
  OTLP endpoint when one is configured; no collector is part of this stack.
- **No log aggregation.** Logs are JSON on stdout, which is what a collector
  wants, and nothing collects them here.
