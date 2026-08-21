# CRM, Leads, and Conversations

**Status: Planned** — no CRM code exists yet. See [../TASKS.md](../TASKS.md) phases 4, 7, 8, and 10.

Scope: contacts, conversations, human handoff, leads, and follow-ups.

## Conversations

Fields: `tenant_id`, `contact_id`, `assigned_to`, `current_agent_id`, `mode`, `status`, `summary`, `sentiment`, `priority`, `last_message_at`, `metadata`. Messages: `tenant_id`, `conversation_id`, WhatsApp message ID, `direction`, `type`, `content`, media metadata, `status`, timestamps, `metadata`. All messages are stored.

## Human handoff

Every conversation is in `AI` or `HUMAN` mode. In `HUMAN` mode automatic AI replies stop, team members reply through the system, ownership and handoff reason are tracked, and the conversation can be assigned. Resuming AI switches the mode back.

Triggers: explicit customer request, low AI confidence, angry or highly negative sentiment, sensitive requests, agent rules, tool failure, or a business escalation rule.

## Leads

Fields: `tenant_id`, `name`, `phone`, `email`, `source`, `status`, `interest`, `budget`, `score`, `assigned_to`, `notes`, `metadata`, timestamps. Statuses: `NEW`, `CONTACTED`, `QUALIFIED`, `PROPOSAL`, `WON`, `LOST`.

Agents create, update, and qualify leads only through validated tenant-scoped tools. Example extraction from "My name is Ahmed, I want to finish a 150m apartment and my budget is 500k": `name=Ahmed`, `interest=apartment finishing`, `area=150m`, `budget=500000`.

## Follow-ups

Soft signals such as "I'll think about it" schedule a follow-up. Before sending, the system re-checks whether the customer replied; pending follow-ups are cancelled or re-evaluated when they do. Follow-ups are cancellable, idempotent, and respect the WhatsApp service window and template rules ([WHATSAPP.md](WHATSAPP.md)).

## Sentiment and priority

Stored per conversation and message where relevant: sentiment (`positive`, `neutral`, `negative`, `angry`), sentiment score, priority, detected intent, and confidence. Strongly negative sentiment raises priority, may flag the conversation, may trigger handoff, and emits an analytics event ([ANALYTICS.md](ANALYTICS.md)).
