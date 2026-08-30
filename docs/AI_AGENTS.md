# AI Agents

**Status: Implemented** — an agent answers a customer end to end, in a worker of its own, grounded in the workspace's own documents, and every provider call it makes is metered. Decisions: ADR-007, ADR-014, ADR-015, ADR-027.

Scope: agent configuration, orchestration, tool calling, and conversation memory. What an agent sees of an attached file is covered in [MEDIA.md](MEDIA.md); when an agent is stopped from replying at all, in [SENTIMENT.md](SENTIMENT.md).

## Agent configuration

An agent is a row in `agents`, owned by one workspace, with a unique name inside it. What is actually stored:

| Field | Meaning |
| --- | --- |
| `name`, `description` | Identification inside the workspace |
| `status` | `DRAFT`, `ACTIVE` or `DISABLED` |
| `model` | A model named in `OPENAI_ALLOWED_MODELS`; defaults to `OPENAI_MODEL`, which is always permitted |
| `system_prompt` | The developer instructions sent on every turn |
| `temperature`, `max_output_tokens` | Sampling and output bounds. `max_output_tokens` defaults to `OPENAI_MAX_OUTPUT_TOKENS` and may not exceed it |
| `memory_message_limit`, `memory_token_budget` | How much history the agent may see |
| `is_default` | The agent that answers when nothing more specific applies |

Personality, language, tone, triggers, routing rules and fallback behaviour were originally sketched as separate fields. They are not columns: all of them are expressible in the system prompt, and a column per stylistic knob would have to be assembled back into that same prompt anyway. Routing beyond a single default has no second selector to route to yet, so it waits for one.

Agents are created as drafts whatever the request says. An agent that began answering the moment it was created would be live before anyone had read its prompt. Only an `ACTIVE` agent marked default will answer, so promotion is the deliberate act that puts one in front of customers.

Tool access is a grant per agent in `agent_tools`: a tool name, an `enabled` flag, and optional JSON configuration. Grants are validated against the registry when made, so a typo fails immediately; a grant naming a tool a later release removed still reads back rather than breaking the screen. Revoking disables the grant instead of deleting it, so turning a tool back on does not discard its configuration.

### API

| Method | Path | Who |
| --- | --- | --- |
| `GET` | `/api/v1/agents` | Any workspace member |
| `POST` | `/api/v1/agents` | Admin or owner |
| `GET` | `/api/v1/agents/available-tools` | Any workspace member |
| `GET` | `/api/v1/agents/{agent_id}` | Any workspace member |
| `PATCH` | `/api/v1/agents/{agent_id}` | Admin or owner |
| `POST` | `/api/v1/agents/{agent_id}/default` | Admin or owner |
| `GET` | `/api/v1/agents/{agent_id}/tools` | Any workspace member |
| `PUT` | `/api/v1/agents/{agent_id}/tools` | Admin or owner |
| `DELETE` | `/api/v1/agents/{agent_id}/tools/{name}` | Admin or owner |

Reading is open to members because staffing an inbox means seeing what the agent is configured to do. Changing what customers are told is an administrative act.

## Orchestrator flow

```
Webhook stores + projects the message -> enqueue one job per conversation
  -> worker reserves the job -> open one database session
  -> load conversation -> HUMAN mode? stop
  -> resolve the agent (requested, or the active default)
  -> build the memory window -> collect granted tools
  -> Responses API -> tool calls? run them, feed results back (max 3 rounds)
  -> reply text -> worker sends it through the messaging service -> commit
```

The split in the last two lines is the important one. `AgentOrchestrator.answer()` returns an `AgentOutcome` — reply text, whether a handoff was requested, which tools ran, token usage, how many rounds it took — and sends nothing. The worker decides to send. That keeps the orchestrator testable with no WhatsApp account and no database, and it means a bug in sending cannot be reached by a bug in reasoning.

Three guards stop a turn before it costs anything:

- **`HUMAN` mode.** A conversation a colleague has taken over is never answered by an agent, logged as `agent.skipped_human_mode`.
- **No active default.** If nothing is configured to answer, the turn ends rather than falling back to some built-in prompt.
- **A round limit.** The tool loop runs at most three rounds. A model that keeps asking for tools stops being useful long before it stops being expensive, and `agent.round_limit_reached` says so.

A tool that raises is not an outage. A rejected argument becomes tool output the model can read and retry against (`agent.tool_rejected`), and a domain error becomes "That did not work: …" (`agent.tool_failed`). Only unexpected exceptions escape, and they belong to the worker.

## Conversation memory

The window is assembled from the conversation's own messages, newest first, and stops at whichever bound is reached first: `memory_message_limit` turns or `memory_token_budget` estimated tokens. Dropped turns are counted and logged, so a truncated context is visible rather than silent.

Twice the message limit is fetched to fill it, because failed outbound messages are skipped: a message Meta rejected was never seen by the customer, so replaying it as something the agent said would make the agent reason about a conversation that did not happen.

Token counts are an estimate, not a tokenisation: four characters per token for ASCII, two for non-ASCII, which keeps Arabic from being wildly under-counted. This is deliberate — a real tokeniser means a new dependency and a model-specific vocabulary, for a number used only to decide where to cut history. The estimate is compared against a budget, never billed against.

### What an agent sees of an attachment

A media message contributes up to two things, and the window keeps them apart:

```
how much is this one?
[image] A blue three-seat sofa with a price tag reading 4,500 EGP.
```

The first line is the customer's caption — their own words, rendered as ordinary text. The second is what Wasla concluded the file says, and it is labelled, so the model is never in a position to quote a machine transcription back to someone as their own sentence. A file that could not be read still produces a line with its reason, because silence would let the agent answer as though nothing had been sent.

Attachments are fetched for the whole window in one query and passed into `build_window`, rather than reached through a relationship: a lazy load inside an async session does not merely cost a query per message, it raises. See [MEDIA.md](MEDIA.md).

A rolling conversation summary is still planned. Long conversations currently lose their oldest turns rather than compressing them.

## Provider integration

All inference goes through `app/integrations/openai/`, over HTTP with no vendor SDK, using the Responses API (ADR-007, ADR-014). Requests set `store: false` and never thread turns provider-side: the conversation lives in the workspace's own tables.

Retries are the inverse of the WhatsApp client's — 429, transport errors and 5xx are all retried, three attempts with linear backoff — because a duplicated inference costs tokens and reaches no customer, while a duplicated send reaches one. Provider error prose is never logged, only its `code` and `type`, because that prose can quote the request and the request contains a customer's conversation.

## Model and cost policy (ADR-053)

Two settings decide what a workspace may spend per call, and both are enforced in `AgentService` rather than in the schema — a schema cannot see configuration, and the refusal has to be a `422` an administrator can act on rather than a failed inference a customer waits for.

| Setting | Effect |
| --- | --- |
| `OPENAI_ALLOWED_MODELS` | Comma-separated. A model outside it is refused on agent create *and* update. Empty means no restriction |
| `OPENAI_MAX_OUTPUT_TOKENS` | The ceiling on `max_output_tokens`, and its default. A higher request is refused, not silently clamped |

`OPENAI_MODEL` is always permitted whatever the allowlist says: it is what an agent naming no model is given, so a list omitting it would make an ordinary agent unbuildable.

Empty-means-unrestricted is the right default for a developer's container and the wrong one for anything paying a provider bill. The plan caps the *number* of AI requests; only these settings cap what each one costs, so a deployment without them lets a workspace administrator name the most expensive model on offer.

**Neither is reachable from a prompt or a tool.** Model choice, token ceiling, temperature and system prompt are configuration, and no tool declares an argument by any of those names — `tests/integration/test_ai_security.py` asserts that structurally over the whole registry, so a tool added later cannot quietly expose one.

### The allowance bound (ADR-054)

One turn is up to three provider calls and each is metered as a request. The worker therefore reads the *balance* rather than a yes/no, and caps the turn's rounds at what remains, so a workspace cannot be billed past a limit the system had already read.

**Known limitation, not fixed here.** Two workers answering the same workspace at the same instant both read the balance before either records against it, so *N* concurrent workers can still spend *N* rounds beyond the limit. Closing that needs an atomic reservation, which the entitlement system has for no limit — it is a platform-wide property rather than an AI one, and half-fixing it here would leave the same race everywhere else while implying it was solved.

## What leaves Wasla (ADR-055)

Every AI feature calls OpenAI. This is the complete list of what is sent, and it is written from the request builders rather than from intent.

| Call | Endpoint | What is sent |
| --- | --- | --- |
| Agent turn | Responses | The agent's `system_prompt`; the memory window — customer messages, previous agent replies, media descriptions and voice transcripts; tool names, descriptions and JSON schemas; tool results, which include retrieved knowledge-base passages; `temperature` and `max_output_tokens` |
| Knowledge search | Embeddings | The search query, in the customer's words |
| Document ingestion | Embeddings | The extracted text of the uploaded document, in chunks |
| Voice note | Transcription | The audio file itself |
| Image | Responses (vision) | The image itself |
| Sentiment | Responses | Recent conversation text, capped |

**What is never sent.** No `tenant_id`, `conversation_id`, `user_id`, agent id or any other internal identifier appears in a provider payload — verified against every request builder in `app/integrations/openai/`. No API key but OpenAI's own, which travels in the `Authorization` header and never in a body. Nothing from `.env` reaches a prompt: prompts are assembled from database rows only.

**What is persisted, and where.** Replies are stored as ordinary messages in the workspace's own tables. Prompts are not stored: the memory window is rebuilt from messages on every turn and discarded afterwards, so there is no prompt archive to leak or to delete. Requests set `store: false` and never use `previous_response_id`, so no conversation state is threaded provider-side.

**Provider retention is an operator responsibility, and is not asserted here.** `store: false` is what this application controls. What OpenAI retains beyond that — abuse-monitoring windows, zero-retention eligibility, whether a data-processing agreement is in force — depends on the account the key belongs to and on terms this repository cannot inspect. An operator running Wasla for third-party businesses should confirm the posture on their own account and record it, and should not infer it from this document.

**Deletion.** Deleting a workspace's documents removes their chunks and the text that was embedded; deleting a conversation removes the messages a future prompt would have been built from. Neither reaches back to the provider, because nothing was stored there under `store: false`.

## Tools

Implemented:

- `request_human_handoff` — hands the conversation to a person with a reason of at most 200 characters and stops the loop. A conversation can also be handed over without the agent asking: see [SENTIMENT.md](SENTIMENT.md), where the classifier decides before the agent composes anything.
- `search_knowledge` — searches this workspace's own documents and returns the matching passages, or an explicit statement that nothing was found. The tenant id comes from the tool context, never from an argument: a tenant id a model could supply is a tenant id a model could change. Details in [RAG.md](RAG.md).
- `schedule_follow_up` — arranges to message the customer again later if they go quiet. Names no follow-up: the nudge belongs to the conversation the turn is already in, and calling it again reschedules rather than queueing a second message. A delay outside the permitted bounds comes back as text the model can correct on its next turn. Details in [CRM.md](CRM.md).
- `record_lead_details` — saves what the customer said about themselves onto their lead. Every argument is optional, because extraction is partial by nature: a name arrives in one message and a budget three messages later, and a required field would push the model into inventing one. The tool offers no way to name a lead, set a status or set a score — it reports what it heard, and the service resolves which lead that is from the conversation's contact. Fields a person entered are never overwritten. Details in [CRM.md](CRM.md).

Planned, in the phase that gives each one something to act on: `create_lead`, `update_lead`, `get_lead`, `assign_lead` (Phase 7), `schedule_follow_up` (Phase 8), `send_media` (Phase 9), and later `get_product`, `get_price`, `create_ticket`, `get_order`, `check_availability`, `create_appointment`, `cancel_appointment`, `reschedule_appointment`.

Every tool that mutates writes an audit row after — never before — the mutation succeeds, with `AuditActorKind.AGENT` as the actor and the tenant and conversation taken from `ToolContext`. The model cannot influence who the row says acted, and a refused or failed tool leaves no row. `meta` carries shapes only — which lead fields were filled, how long a follow-up delay was — never the customer's words, so the trail does not become a second copy of the conversation with a different retention story.

Rules the registry enforces now: every argument is validated against a declared schema before a handler runs; a handler receives a `ToolContext` carrying the tenant id, the conversation id, the session and — where one is configured — an embeddings client, so a tool cannot reach outside the workspace it was called in; an agent is offered only the tools it has been granted; and a name the registry does not know is never dispatched, so model output cannot name its way into arbitrary execution.

A tool that cannot work says so in its own output rather than failing the turn. `search_knowledge` without a configured provider returns a sentence telling the agent not to guess and to offer a handoff, which is a usable instruction; an exception would only end the turn silently.

Retrieval details in [RAG.md](RAG.md); escalation in [CRM.md](CRM.md).

## Queue and worker

Jobs move through three Redis lists — `agent:jobs:pending`, `agent:jobs:inflight`, `agent:jobs:failed` — reserved with a blocking `BLMOVE` so a job survives the death of the worker holding it (ADR-015). A job carries the tenant id, the conversation id, and optionally a specific agent.

The webhook enqueues one job per conversation that received a message, however many arrived in the delivery, and swallows a queue failure after logging `agent.enqueue_failed`: the messages are already stored, and a Redis outage must not make Meta retry the whole delivery.

The worker owns the transaction. It opens one session per job, runs the orchestrator inside it, sends any reply through `MessagingService`, and commits once — so the outbound message row and the conversation timestamps land together or not at all.

What the worker does not have is a process to run in. `AgentWorker.run_forever()` exists and nothing calls it; the entrypoint and its container service arrive with the Phase 8 worker. Nothing reaps the in-flight list yet either, so a job abandoned by a killed worker stays visible but stalled.
