# CRM, Leads, and Conversations

Scope: contacts, conversations, human handoff, leads, and follow-ups.

Conversations, contacts and handoff are implemented (Phase 4). Leads are implemented (Phase 7). Follow-ups and sentiment are **Status: Planned** — see [../TASKS.md](../TASKS.md) phases 8 and 10.

## Conversations

Fields: `tenant_id`, `contact_id`, `account_id`, `assigned_to_id`, `mode`, `status`, `handoff_reason`, `last_message_at`, `last_inbound_at`. Messages: `tenant_id`, `conversation_id`, `wa_message_id`, `direction`, `kind`, `body`, template metadata, `status`, timestamps. All messages are stored.

A conversation is scoped by connected WhatsApp number as well as by contact: a business with a sales number and a support number is holding two genuinely separate conversations with the same person.

## Human handoff

Every conversation is in `AI` or `HUMAN` mode. In `HUMAN` mode automatic AI replies stop, team members reply through the system, and ownership and handoff reason are tracked. Resuming AI switches the mode back and clears the reason.

Implemented triggers: an explicit customer request, or anything else that leads the agent to call `request_human_handoff`. Sentiment-driven escalation is planned for Phase 10.

## Leads

### Model

`leads` carries `tenant_id`, `contact_id`, `conversation_id`, `name`, `phone`, `email`, `interest`, `budget_amount`, `budget_currency`, `status`, `source`, `score`, `assigned_to_id`, `tags`, `custom_fields`, `human_verified_fields`, `qualified_at`, `closed_at`, `last_activity_at`, and timestamps.

`contact_id` and `conversation_id` are nullable and record where the lead came from, not a live link — deleting either nulls the reference and leaves the lead standing.

Budgets are `NUMERIC(14,2)`, not floats: money compared or summed as binary floating point eventually disagrees with the customer's own arithmetic.

### Lifecycle

Statuses are `NEW`, `CONTACTED`, `QUALIFIED`, `PROPOSAL`, `WON`, `LOST`. The permitted moves are declared as a graph in `app/db/models/lead.py` rather than scattered through the service:

```
NEW ──→ CONTACTED ──→ QUALIFIED ──→ PROPOSAL ──→ WON
 │           │             │            │
 └───────────┴─────────────┴────────────┴──────→ LOST ──→ NEW
```

`WON` is terminal: a returning customer is a new lead, and rewriting the old row would destroy the record of the deal that closed. `LOST` reopens to `NEW` only. Setting the status a lead already has succeeds and changes nothing, so a retried job is safe.

### One open lead per customer

At most one lead per contact is in a non-terminal status, enforced by a partial unique index rather than a service check — two webhook deliveries can be in flight at once, and only a constraint settles that race. See [ADR-020](../DECISIONS.md).

An agent never names a lead. It reports what it heard and the service resolves which lead that belongs to from the conversation's contact, which makes `record_lead_details` idempotent by construction.

### Extraction, and what it may not touch

Agents capture leads through the `record_lead_details` tool. Two rules bound it, both covered by [ADR-021](../DECISIONS.md):

- **A human edit is sticky.** Any field a person sets is recorded in `human_verified_fields`, and extraction skips it. The AI fills blanks and corrects its own earlier guesses; it never overwrites what someone confirmed. A field deliberately cleared stays cleared.
- **Extraction never touches judgement.** Only `name`, `email`, `phone`, `interest`, `budget_amount` and `budget_currency` are agent-writable. Status, score, assignment and tags are decisions.

A value the model produces that fails validation is dropped rather than raising, so one bad phone number does not lose the rest of the capture. A person's bad input is reported instead, because someone typing into a form deserves to be told.

Budgets must arrive as plain numbers. `"500k"` is refused rather than guessed: it means 500,000 to most people and 500 to a parser that gives up, and reading it wrong silently reprioritises a real pipeline.

Extraction stops if the conversation has been handed to a colleague — a job queued before the handoff can still run after it.

Example, from "My name is Ahmed, I want to finish a 150m apartment and my budget is 500000":
`name=Ahmed`, `interest=150m apartment finishing`, `budget_amount=500000`.

### Notes and activity

`lead_notes` holds internal text written by a person or an agent; notes are never sent to the customer. `lead_activities` is an append-only log of what changed, who changed it (`user`, `agent` or `system`), and the previous value. There is no route that edits or removes an entry: an audit trail the application can rewrite does not answer the question it exists to answer.

### Assignment

Assignment goes through the existing membership system — the assignee must hold a membership in the workspace, verified rather than assumed, because the id arrives in a request body.

## API

All routes are workspace-scoped through the active workspace dependency; another workspace's lead id answers 404 rather than 403, which would confirm it exists.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/api/v1/leads` | any member |
| POST | `/api/v1/leads` | any member |
| GET | `/api/v1/leads/statistics` | administrator |
| GET | `/api/v1/leads/{id}` | any member |
| PATCH | `/api/v1/leads/{id}` | any member |
| POST | `/api/v1/leads/{id}/status` | any member |
| POST | `/api/v1/leads/{id}/assignment` | administrator |
| POST | `/api/v1/leads/{id}/score` | any member |
| GET, POST | `/api/v1/leads/{id}/notes` | any member |
| GET | `/api/v1/leads/{id}/activity` | any member |

Assignment and statistics require an administrator: handing someone a deal and reading across every rep's pipeline are management actions. This is a different line from the one drawn on conversations, where any member may assign — grabbing an unanswered conversation is triage.

Listing supports filtering by status, source, assignee, unassigned-only, tag, free-text search, contact and conversation. Filters intersect. Pagination is by keyset cursor, because a pipeline is written to while it is being read ([API.md](API.md)).

`PATCH` distinguishes an omitted field from an explicit null: omitted is left alone, null clears the value. Anything touched becomes human-verified.

## Follow-ups

**Status: Planned.** Soft signals such as "I'll think about it" will schedule a follow-up. Before sending, the system re-checks whether the customer replied; pending follow-ups are cancelled or re-evaluated when they do. Follow-ups will be cancellable, idempotent, and will respect the WhatsApp service window and template rules ([WHATSAPP.md](WHATSAPP.md)).

## Sentiment and priority

**Status: Planned.** Sentiment (`positive`, `neutral`, `negative`, `angry`), sentiment score, priority, detected intent and confidence, stored per conversation. Strongly negative sentiment will raise priority, may flag the conversation, may trigger handoff, and will emit an analytics event ([ANALYTICS.md](ANALYTICS.md)).
