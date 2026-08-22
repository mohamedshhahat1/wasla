# Analytics and Usage

**Status: In Progress** — usage metering, the analytics event model and the tenant dashboard APIs are Implemented; the platform surface is Planned. See [../TASKS.md](../TASKS.md) phase 12. The decision behind how usage is written is ADR-027.

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

Total, active, trial, and suspended companies; active WhatsApp numbers; messages today and this month; AI requests; input, output, and total tokens; leads and conversations created; human handoffs; RAG queries; voice minutes; media processed; storage usage; MRR; ARR; subscription revenue; estimated AI, infrastructure, and margin figures; growth; churn; plan distribution.

All analytics and usage endpoints support date-range filtering and pagination. Aggregation is designed to run in workers rather than on the request path.
