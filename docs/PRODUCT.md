# Product

**Status: Planned** — this document describes intended product scope. No product feature is implemented yet.

Scope: what Wasla is, who it serves, and the customer journey. Architecture lives in [../ARCHITECTURE.md](../ARCHITECTURE.md); task status lives in [../TASKS.md](../TASKS.md).

## Positioning

Wasla — AI employees for WhatsApp. "Connect businesses with their customers through AI employees." Wasla is a customer engagement platform, not a scripted chatbot: agents understand intent, use company knowledge, act through tools, and escalate to humans.

## Personas

| Persona | Needs |
| --- | --- |
| Platform owner (Wasla operator) | Tenant administration, usage, revenue, system health, audit trail |
| Tenant owner | Connect WhatsApp numbers, configure agents, invite team, manage billing |
| Tenant admin | Agents, knowledge base, campaigns, team and conversation management |
| Member (sales/support) | Inbox, human replies, lead follow-up |
| End customer | Fast, accurate answers on WhatsApp |

## Customer journey

```
Customer message -> webhook -> tenant resolved -> contact resolved
  -> history loaded -> agent selected -> intent understood
  -> knowledge searched -> tools called -> response generated
  -> WhatsApp reply -> conversation stored -> lead created/updated
  -> sentiment analysed -> follow-up scheduled -> human handoff if needed
```

## Capability areas

| Capability | Status |
| --- | --- |
| WhatsApp Business Cloud API messaging | Implemented |
| Configurable AI agents (sales, support, booking) | Implemented |
| Knowledge base with tenant-scoped RAG | Implemented |
| Conversation inbox and human handoff | Implemented |
| Automatic lead capture and qualification | Implemented |
| Follow-ups | Implemented |
| Attachments: images, voice notes, documents | Implemented |
| Sentiment, priority and automatic escalation | Implemented |
| Team management with tenant-scoped roles | Implemented |
| Campaigns and templates | Planned |
| Analytics and usage dashboards | Planned |
| Plans, subscriptions, billing | Planned |

## Out of scope for now

Frontend applications, additional channels (Instagram, Messenger, Telegram, web chat, email), CRM connectors, e-commerce and payment integrations, white-labeling, and enterprise SSO. The architecture keeps clean boundaries for them; see `claude.md` section 54.
