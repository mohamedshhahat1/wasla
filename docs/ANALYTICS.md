# Analytics and Usage

**Status: In Progress** — usage metering is Implemented and wired into every path that consumes something; the analytics event model and the dashboard APIs are Planned. See [../TASKS.md](../TASKS.md) phase 12. The decision behind how usage is written is ADR-027.

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

Conversations, messages, leads, qualified leads, conversion rate, human handoffs, AI resolution rate, average response time, unhappy customers, agent performance, campaign performance. Tenant-level usage counters: `messages_received`, `messages_sent`, `ai_requests`, `input_tokens`, `output_tokens`, `total_tokens`, `rag_queries`, `media_processed`, `voice_minutes`, `leads_created`, `conversations_created`, `storage_used`, API requests, campaign messages.

## Platform metrics

Total, active, trial, and suspended companies; active WhatsApp numbers; messages today and this month; AI requests; input, output, and total tokens; leads and conversations created; human handoffs; RAG queries; voice minutes; media processed; storage usage; MRR; ARR; subscription revenue; estimated AI, infrastructure, and margin figures; growth; churn; plan distribution.

All analytics and usage endpoints support date-range filtering and pagination. Aggregation is designed to run in workers rather than on the request path.
