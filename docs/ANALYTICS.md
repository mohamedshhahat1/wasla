# Analytics and Usage

**Status: Planned** — no analytics or usage code exists yet. See [../TASKS.md](../TASKS.md) phase 12.

Scope: analytics events, usage metering, and dashboard data contracts.

## Usage events

Append-only records: `tenant_id`, `event_type`, `quantity`, `unit`, `metadata`, `created_at`. Planned event types: `WHATSAPP_MESSAGE_RECEIVED`, `WHATSAPP_MESSAGE_SENT`, `AI_REQUEST`, `AI_INPUT_TOKEN`, `AI_OUTPUT_TOKEN`, `RAG_QUERY`, `MEDIA_PROCESSING`, `VOICE_TRANSCRIPTION`, `STORAGE_USED`, `LEAD_CREATED`, `CONVERSATION_CREATED`, `CAMPAIGN_MESSAGE`, `API_REQUEST`.

Usage is tenant-isolated, recorded from services rather than routes, and aggregated for dashboards and billing.

## Analytics events

Planned events: `message_received`, `message_sent`, `conversation_created`, `lead_created`, `lead_qualified`, `handoff`, `appointment_created`, `follow_up_sent`, `agent_response`, `customer_angry`, `campaign_sent`, `campaign_delivered`.

`customer_angry` has a head start. Phase 10 already writes one timestamped, tenant-scoped row per classified message to `message_sentiments`, carrying the label, the score, the intent and whether it escalated ([SENTIMENT.md](SENTIMENT.md)). That is the series this event would count, so no second write was added here: doing so now would mean migrating two shapes later.

## Tenant metrics

Conversations, messages, leads, qualified leads, conversion rate, human handoffs, AI resolution rate, average response time, unhappy customers, agent performance, campaign performance. Tenant-level usage counters: `messages_received`, `messages_sent`, `ai_requests`, `input_tokens`, `output_tokens`, `total_tokens`, `rag_queries`, `media_processed`, `voice_minutes`, `leads_created`, `conversations_created`, `storage_used`, API requests, campaign messages.

## Platform metrics

Total, active, trial, and suspended companies; active WhatsApp numbers; messages today and this month; AI requests; input, output, and total tokens; leads and conversations created; human handoffs; RAG queries; voice minutes; media processed; storage usage; MRR; ARR; subscription revenue; estimated AI, infrastructure, and margin figures; growth; churn; plan distribution.

All analytics and usage endpoints support date-range filtering and pagination. Aggregation is designed to run in workers rather than on the request path.
