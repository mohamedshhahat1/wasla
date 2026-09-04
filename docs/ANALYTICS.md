# Analytics and Usage

**Status: Implemented** — usage metering, the analytics event model, the tenant dashboard APIs and the cross-workspace platform surface. Revenue, MRR, ARR and churn are deliberately absent rather than pending; the reasoning is below. See [../TASKS.md](../TASKS.md) phase 12. The decision behind how usage is written is ADR-027.

Scope: analytics events, usage metering, and dashboard data contracts.

## Usage events

`usage_events` is append-only: `tenant_id`, `event_type`, `quantity`, `unit`, `metadata`, `occurred_at`. There is no `updated_at`, because nothing updates a row — a correction is another row, which is what keeps a past month's figure reproducible.

| Meter | Unit | Counted when |
| --- | --- | --- |
| `whatsapp_message_received` | count | An inbound message is stored for the first time |
| `whatsapp_message_sent` | count | An outbound message leaves for Meta |
| `ai_request` | count | One provider call inside an agent turn |
| `ai_input_token` | token | Prompt tokens the provider reported |
| `ai_output_token` | token | Completion tokens the provider reported |
| `rag_query` | count | One tenant-scoped vector search |
| `media_processing` | count | One attachment read |
| `voice_transcription` | second | Audio transcribed |
| `storage_used` | byte | Bytes written to the file store |
| `lead_created` | count | A lead captured from a conversation |
| `conversation_created` | count | A conversation opened |
| `campaign_message` | count | One campaign message sent |
| `api_request` | count | Reserved for request metering |

**The unit is a property of the meter, never of the caller.** `EVENT_UNITS` in `app/db/models/usage.py` is the single declaration, and the recorder applies it; a caller cannot pass one. A meter added to the enum without a unit fails at import rather than producing a total in mixed units.

**A row is staged in the transaction that consumed the thing.** `UsageRecorder` performs no I/O and never commits: it stages through the session its caller already holds, so a rolled-back turn is not billed and a committed message is always counted (ADR-027). Nothing deduplicates here — every metered path has an idempotency key upstream.

Zero and negative quantities are dropped rather than stored. A model reporting no output tokens is an absence, not a row; a negative quantity is the only way an append-only total could go down, so it has to be a deliberate correction rather than a rounding accident.

Reads are aggregates only, over a half-open window `[since, until)` so two adjacent months sum to the pair. `UsageService.summary()` returns the named counters below plus the unabridged totals; `UsageService.series()` returns a daily point per meter. Days are UTC, which is a real limitation for a workspace elsewhere and the honest one until a workspace can state its timezone.

## Analytics events

**Analytics are derived from the domain tables; `analytics_events` records only what the domain forgets** (ADR-028).

Almost every event the product specification lists is already a timestamped, tenant-scoped row. A message received is a row in `messages`. A lead qualified is a row in `lead_activities`. An angry customer is a row in `message_sentiments`, carrying the label, the score and whether it escalated ([SENTIMENT.md](SENTIMENT.md)). A campaign delivered is a recipient row joined to its message's status. Writing a second copy of those would be two shapes to migrate, two places to fix a count, and two answers to the same question the moment they drift — and deriving is retroactive, so a metric defined next month can still be computed for last month.

The handoff is the exception. `conversations.mode` says who has a conversation now; it cannot say when it moved, how often, or who decided — and the three causes are indistinguishable afterwards while being the most important distinction on the dashboard.

| Column | Meaning |
| --- | --- |
| `event_type` | `handoff` or `handoff_resumed` |
| `source` | `agent` (the agent asked to hand over), `sentiment` (a reading escalated it), `user` (a colleague took it), `system` |
| `conversation_id` | The conversation it happened to |
| `actor_id` | The person who did it, when a person did. `SET NULL` on delete: the handoff still happened after they left |
| `metadata` | The reason, copied short so a historical row explains itself |
| `occurred_at` | When |

Only a real change is an event: setting `human` on a conversation a colleague already owns is somebody editing a reason, not a second handoff. Events and *conversations* are counted separately — a conversation handed over three times is three handoffs but one conversation, and a resolution rate built on the first number would punish it repeatedly.

A member is added to this enum by one test: does anything else already record it? A type added to mirror a count `messages` already answers is a bug.

## Tenant metrics

Derived from the domain tables, over the same half-open window usage uses, so a figure on the analytics page and one on the usage page cover the same period.

| Group | Figures |
| --- | --- |
| Conversations | created, handed off, escalated, AI-resolved, AI resolution rate |
| Messages | received, sent, failed, average response seconds, unanswered |
| Leads | created, qualified, won, lost, conversion rate, by status |
| Sentiment | readings, unhappy conversations, by label |
| Campaigns | sent, delivered, failed, skipped |
| Handoffs | count by source |

Two definitions decide what several of these numbers mean, and both are stated here because a metric whose definition is unwritten is one two people will read differently.

**Average response time** is the time from a customer message that *started a burst* to the next business message in that conversation. A burst is a message whose predecessor was not also inbound: a customer who sends four messages in a row waited once, not four times, and measuring each of them would divide the same wait by four — flattering the figure exactly when service is worst. A customer still waiting contributes nothing rather than an infinity, and is reported separately as `unanswered`. A failed send is not a reply.

**AI resolution rate** is the share of conversations *created in the window* that were never handed to a person. Conversations, not handoff events: one conversation that bounced between agent and colleague three times is one conversation the AI did not resolve, and counting events could drive the rate negative.

Two are deliberately naive and say so. Conversion rate is wins over leads created *in the window*, so a lead created in August and won in September counts in neither month's rate — cohort accounting is a product decision. And `delivered` is read from the message rows rather than the recipient rows, because delivery is Meta's word and arrives later as a status webhook; conflating it with a successful send would report a hundred per cent delivery forever.

Rates are always returned beside the counts they were computed from. A rate on its own cannot be checked, cannot be re-aggregated across two windows, and hides the difference between nine of ten and nine hundred of a thousand.

## Platform metrics

**Implemented:** workspaces total, active and suspended; WhatsApp numbers connected and live; every usage meter summed across the platform; and the same counters per workspace, for the page being displayed.

**Not implemented, and each for a reason rather than for want of time.** Revenue, MRR, ARR, subscription revenue and churn are questions about subscriptions, and there are none until phase 13. Estimated AI cost needs per-model prices that are stored nowhere: token counts are real, a cost derived from invented prices is not, and a plausible figure on a dashboard is worse than an absent one because somebody eventually believes it. Trial companies and plan distribution wait on plans for the same reason.

All analytics and usage endpoints take an optional UTC window and report the one they applied. Aggregation runs on the request path today: each query is an indexed range scan over one workspace's rows, and moving it to a worker before that is the bottleneck would add a component that has to be running for a figure to be right. When it does become the bottleneck, a rollup is added *beside* the rows rather than instead of them — a sum over rows can be recomputed for any window, and a drifted counter cannot.

### What was measured, and what it says about when that day comes

3.9 million events across 50 workspaces over nine months (ADR-081):

| query | 1.3M rows | 3.9M rows |
| --- | --- | --- |
| entitlement period check | 8.8ms | 9.4ms |
| workspace dashboard totals | 20ms | 33ms |
| workspace daily series | 63ms | 66ms |
| platform `by_tenant` | 64ms | 172ms |

**The entitlement check does not grow with the table.** It sums one meter over one workspace's billing period, and the rows in that sum are bounded by the plan limit rather than by how much history exists — a workspace that has spent its 25,000 AI requests stops making them. It scanned the same 26,035 rows at both sizes.

What it did do was visit the table once per row to read two narrow columns, so `quantity` and `unit` are now INCLUDE columns on `ix_usage_events_tenant_id_event_type_occurred_at` and the sum is an `Index Only Scan`. 9.4ms to 7.1ms, and — more to the point — indifferent to the table growing around it: the same check on a 1.3GB table whose heap pages had been evicted measured 50ms before the change.

**The platform roll-up is the one that grows without bound**: 64ms to 172ms for a 3× table, linear in total rows. It is a SaaS-owner dashboard and 172ms is nobody's problem, so no rollup exists yet. The trigger is written down rather than guessed at: at roughly 50 million rows in the window, or a dashboard past a second, add a daily rollup keyed `(tenant_id, day, event_type)` with `INSERT ... ON CONFLICT DO UPDATE`, serve dashboards from it, and leave entitlements reading raw rows. Money should not come to depend on an aggregation that can drift.
