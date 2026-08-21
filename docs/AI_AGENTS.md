# AI Agents

**Status: Planned** — no agent code exists yet. See [../TASKS.md](../TASKS.md) phase 5. Provider decision: ADR-007.

Scope: agent configuration, orchestration, tool calling, and conversation memory.

## Agent configuration

An agent is an AI employee configuration, not a bare prompt: `name`, `description`, `personality`, `language`, `tone`, `instructions`, `model`, knowledge sources, allowed tools, triggers, routing rules, handoff rules, enabled state, fallback behaviour, metadata. Agents are tenant-owned; multiple agents per tenant are supported.

Examples: a sales agent with product and pricing knowledge plus lead tools; a support agent with complaint handling, order lookup, ticketing, and escalation; a booking agent with availability, create, cancel, and reschedule tools.

## Orchestrator flow

```
Incoming message -> load tenant -> load conversation -> determine mode
  -> if HUMAN: stop (no AI) -> determine agent -> load agent config
  -> load conversation memory -> retrieve knowledge -> prepare allowed tools
  -> OpenAI Responses API -> handle tool calls -> execute business tools
  -> continue interaction if needed -> final response
  -> send via WhatsApp -> persist -> record usage
```

The orchestrator is testable independently of FastAPI, Meta, and OpenAI.

## Provider integration

All inference goes through `app/integrations/openai/` using the current Responses API: configurable models, developer instructions, conversation context, structured outputs where useful, tool calling, token usage tracking, retries, timeouts, and error handling. AI failures never crash the webhook path. API keys are never logged, and sensitive customer content is not logged unnecessarily.

## Conversation memory

Context is assembled from a recent message window, a rolling conversation summary, relevant retrieved knowledge, and the current message. Full history is never resent; context assembly is token-aware.

## Tools

Planned tool surface: `create_lead`, `update_lead`, `get_lead`, `assign_lead`, `get_product`, `get_price`, `search_knowledge`, `send_media`, `handoff_to_human`, `create_ticket`, `get_order`, `check_availability`, `create_appointment`, `cancel_appointment`, `reschedule_appointment`, `schedule_follow_up`.

Rules: arguments are schema-validated, every tool enforces tenant isolation, agents receive only explicitly allowed tools, and model output can never trigger arbitrary execution. Retrieval details in [RAG.md](RAG.md); escalation in [CRM.md](CRM.md).
