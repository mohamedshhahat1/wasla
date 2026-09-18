# AI Agents

**Status: Implemented** — an agent answers a customer end to end, in a worker of its own, grounded in the workspace's own documents; every provider call it makes is metered, every customer turn is charged once, and every turn records how it ended. Decisions: ADR-007, ADR-014, ADR-015, ADR-027, ADR-104, ADR-105.

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
Webhook stores + projects the message (each gets its position) -> enqueue a job
  -> worker reserves the job -> open one database session
  -> load conversation (missing yet? retry)
  -> claim the logical turn          [lost? another attempt owns it: stop]
  -> plan: HUMAN mode? no answering agent?          [complete, no charge]
  -> lifecycle gate: workspace suspended or deleted? conversation closed?
     number disabled?                               [complete, no charge]
  -> charge one AI turn AND engage the turn, one transaction
     [no turn left: hand to a person as AI_QUOTA_EXHAUSTED, complete]
  -> load history (by sequence) -> sentiment on its newest customer message
     [connection released for the call; escalated? complete]
  -> build the memory window -> collect granted tools
  -> per round, up to 3:
       release the connection -> record one provider request -> Responses API
       -> reacquire -> run any tool calls, feed results back
  -> commit the turn's token usage
  -> read workspace, agent, conversation and number again, as columns
     [anything changed: suppress, complete with why]
  -> words?    shorten to fit WhatsApp if needed -> send one message
     no words? send a holding message, hand to a person -> complete
```

**No provider call happens while this turn holds a database connection**
(ADR-080). The session is committed and the connection returned to the pool
before each inference and before each per-round meter write, so the number of
turns a worker can run at once is the depth of the queue rather than
`pool_size + max_overflow`. Two consequences are worth knowing: the turn is not
one transaction, so a turn that dies partway leaves the work it finished; and a
commit ends a snapshot, so everything a reply depends on is read again before it
is sent — the workspace's status and deletion, the agent's status, the
conversation's mode and status, and the number. They are read as column values
(`app/agents/lifecycle.py`), never as the objects already in the session, which
would hand back the snapshot. A workspace suspended, an agent disabled, a
conversation closed or taken over while the model was composing gets no AI
answer arriving underneath it, and an old turn never reopens a closed
conversation (AI-06, AI-07).

The split in the last two lines is the important one. `AgentOrchestrator.answer()` returns an `AgentOutcome` — reply text, whether a handoff was requested, which tools ran, token usage, how many rounds it took — and sends nothing. The worker decides to send. That keeps the orchestrator testable with no WhatsApp account and no database, and it means a bug in sending cannot be reached by a bug in reasoning.

These stop a turn before it costs the customer a turn or the platform a provider call:

- **`HUMAN` mode.** A conversation a colleague has taken over is never answered by an agent, logged as `agent.skipped_human_mode`.
- **No active default.** If nothing is configured to answer, the turn ends rather than falling back to some built-in prompt.
- **A workspace no longer served.** A suspended workspace, and a deleted one for the whole of its retention window, gets no inference, no tool and no message. That includes the media that arrives in front of a turn: a file for a suspended or deleted workspace, or on a released number, is not downloaded, stored or read by a paid model - the media worker checks the same lifecycle fresh before Meta, the object store and every paid read, and records the file `SKIPPED` (MEDIA-08, [MEDIA.md](MEDIA.md)). Retention decides when data is erased; it does not keep the AI serving in the meantime. The guarantee holds *during* a turn as well as before it: the executor re-reads the workspace, the agent, the conversation and the number immediately before every tool call, so a workspace suspended while a model was composing writes no CRM record and schedules no customer message (TOOL-03). A follow-up a tool scheduled earlier is cancelled at the transition and refused again at send time, so nothing automated leaves a workspace that is not being served (TOOL-04).
- **A closed conversation, or a disabled number.** A colleague closed it on purpose; a disabled number cannot send.
- **No AI turn left.** The conversation is handed to a person with reason `AI_QUOTA_EXHAUSTED`. The customer is not told about the business's plan.

And one bounds the turn once it runs:

- **A round limit.** The tool loop runs at most three rounds. A model that keeps asking for tools stops being useful long before it stops being expensive, and `agent.round_limit_reached` says so.

## How a turn ends

Every turn the worker completes writes `agent_turns.outcome`, counted by `wasla_agent_turn_outcomes_total`, so "did this customer get an answer, and if not, why?" is a column rather than an investigation:

| `outcome` | Meaning |
| --- | --- |
| `replied` | One reply was sent |
| `handed_off` | The agent's handoff tool ran — the tool genuinely executed, not merely its name appearing in the model's output (AI-03) |
| `escalated` | The sentiment classifier handed the conversation over before a word was composed |
| `empty_response` | The provider answered with no words; the customer was sent a holding message in their own language and the conversation was handed to a person (`AI_EMPTY_RESPONSE`) |
| `quota_blocked` | No AI turn was left in the plan |
| `suppressed_workspace` / `_agent` / `_closed` / `_human` / `_channel` | Something a reply depends on stopped allowing it, before or during the turn |
| `nothing_to_answer` | The conversation held nothing an agent could answer |

`agent_turns.provider_response_id` keeps the provider's id for the turn's last response, for correlating a support question with the provider's records. It is an id, never a body.

**A reply is always one WhatsApp message within the limit.** WhatsApp refuses a body over 4,096 characters, and a model cannot be relied on to stay under it: the default output ceiling of English is roughly twice that. `app/agents/reply.py` sends a reply that fits exactly as written; a longer one is cut at the last paragraph, else sentence, else line or word break inside a 3,800-character target, followed by a short offer to continue in the reply's own language. It is never split across several messages — that decision (MSG-25) stands.

**Every provider request carries an output ceiling** — the smaller of the agent's `max_output_tokens` and `OPENAI_MAX_OUTPUT_TOKENS`, so a deployment that lowers its ceiling holds existing agents to it. `agents.max_output_tokens` is NOT NULL since migration 0060.

**A sentiment failure never costs a reply.** A classifier that cannot be reached, answers unreadably, or whose reading cannot be stored is logged and the turn continues without a reading. Two turns that classify the same message at once store one reading atomically, and the second follows the first's decision.

A tool that raises is not an outage, and since TOOL-01 that includes the ones nobody expected. A rejected argument becomes tool output the model can read and retry against (`agent.tool_rejected`); a domain error becomes "That did not work: …" (`agent.tool_failed`); and **any other exception is contained too** (`agent.tool_crashed`): the call's work is rolled back to its own savepoint, the execution is recorded as failed, the model is given a fixed sentence, and the turn goes on to reply or hand over. It used to escape to the worker, which past engagement has no honest response but to give up — so a NUL in a handoff reason, a lone surrogate in a lead field, an out-of-range delay or an ordinary concurrent duplicate each ended the customer's turn in silence. The model can cause all four, so "unexpected" was never the right word for them.

## Conversation memory

**The conversation is read in the order it happened, by position.** Every message has `messages.sequence`, assigned at insert by a database trigger doing one atomic update of the conversation's counter, so concurrent writers serialise on the conversation row and every producer — inbound, agent, colleague, campaign, follow-up — takes part. The window, the inbox and the sentiment subject all read that one order. `created_at` is not an order: it is the transaction's start, so every message one webhook delivery wrote shares it, and sorting on it handed the model `FOURTH, FIFTH, SECOND, THIRD, FIRST` (AI-01). Migration 0058 numbered existing history by `created_at`, Meta's `sent_at`, then `id` — deterministic, and not a reconstruction of true order for historical rows that shared both timestamps.

**No single message may exceed 100,000 characters in a prompt.** The newest message is admitted whatever its token cost, because an agent with no context cannot answer; that exception had no ceiling. Past it, the message keeps two thirds from its start and one from its end — where a customer's question usually is — around an explicit note that the middle was left out.

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

**Verified against the real Responses API**, not only against a fake transport: authentication and endpoint, the request shape this application builds, the four tool schemas, a returned tool call and its parsing, two-round tool results, the `input_tokens`/`output_tokens`/`total_tokens` fields the meter reads, and the handling of an unknown model, invalid credentials and a timeout. `tests/real_provider/` holds those and is skipped without `OPENAI_API_KEY`, so CI stays green without credentials. What remains unverified against the real provider is listed in the same file's docstring.

Retries are the inverse of the WhatsApp client's — 429, transport errors and 5xx are all retried, three attempts — because a duplicated inference costs tokens and reaches no customer, while a duplicated send reaches one. A `Retry-After` header, as seconds or as an HTTP date, is honoured up to 30 seconds; a longer hint ends the retries rather than spending attempts on refusals. Without a hint the backoff is linear with equal jitter, so workers that met one rate limit do not retry in the same instant (AI-10). Every attempt is counted in `wasla_provider_attempts_total`, so throttling a retry absorbed is still visible. Provider error prose is never logged, only its `code` and `type`, because that prose can quote the request and the request contains a customer's conversation — and the provider's message for a bad key quotes the key.

**What is read and trusted is bounded.** A response body is streamed and read up to 1 MiB: an oversized success body is refused after one attempt and never held whole (AI-14). Token counts are validated where they are parsed — a boolean, a non-integer, a negative or anything past two million per call is recorded as zero and logged, rather than reaching a `BIGINT` column in the transaction that commits a reply (AI-11). A malformed HTTP 200 is a failed call, never a reply.

## Model and cost policy (ADR-053)

Two settings decide what a workspace may spend per call, and both are enforced in `AgentService` rather than in the schema — a schema cannot see configuration, and the refusal has to be a `422` an administrator can act on rather than a failed inference a customer waits for.

| Setting | Effect |
| --- | --- |
| `OPENAI_ALLOWED_MODELS` | Comma-separated. A model outside it is refused on agent create *and* update. Empty means no restriction |
| `OPENAI_MAX_OUTPUT_TOKENS` | The ceiling on `max_output_tokens`, and its default. A higher request is refused, not silently clamped |

`OPENAI_MODEL` is always permitted whatever the allowlist says: it is what an agent naming no model is given, so a list omitting it would make an ordinary agent unbuildable.

Empty-means-unrestricted is the right default for a developer's container and the wrong one for anything paying a provider bill. The plan caps the *number* of AI turns; only these settings cap what each call costs, so a deployment without them lets a workspace administrator name the most expensive model on offer.

**Neither is reachable from a prompt or a tool.** Model choice, token ceiling, temperature and system prompt are configuration, and no tool declares an argument by any of those names — `tests/integration/test_ai_security.py` asserts that structurally over the whole registry, so a tool added later cannot quietly expose one.

### A turn is charged once, when it engages (ADR-104)

**What a customer's plan counts is AI turns, not provider requests.** One turn is a sentiment classification and one to three inference rounds. When the allowance was written in provider requests, the classifier — which nothing checked — spent the last unit of every period and the customer was never answered; four concurrent turns against an allowance of one produced four classifications and no replies (AI-02).

`PERIOD_AI_TURNS` is reserved through `EntitlementService.consume` exactly once per turn, **in the same transaction that engages the turn**. A duplicate job that lost the claim therefore spends nothing, and a reservation that finds the turn no longer engageable rolls its charge back. A turn that will not reach a provider — a person owns the conversation, the workspace is suspended — is never charged.

**Provider requests are still recorded, as cost.** Every call writes `ai_request` with its tokens, tagged `purpose=agent` or `purpose=sentiment`: one row before each inference round, one for the classification. Nothing checks those against a limit. They are what the platform pays for, and the plan is what the customer bought; neither is derived from the other.

**The concurrent race is closed, and the fix is platform-wide rather than AI-only.** `consume` takes a PostgreSQL advisory lock keyed on (workspace, limit), re-checks under it, records the meter, and flushes before releasing — so two workers cannot both spend the last permitted turn. It is a general primitive on `EntitlementService`: any limit fed by a usage meter can use it, and messages and campaigns are free to adopt it without a second mechanism being invented.

The race was real and larger than an estimate suggested. With the lock removed, ten concurrent reservations against an allowance of three **all ten succeeded**; with it, exactly three do. `tests/integration/test_ai_security.py::test_concurrent_reservations_cannot_oversell_the_allowance` runs that on ten real connections, and it is mutation-tested: deleting the lock fails it.

**What the lock does not do.** It is held only for the reservation's own short transaction — never across an inference, which would serialise every conversation a busy workspace is having. The worker therefore reserves on a separate session and commits before calling the provider. The consequence is deliberate and is the safe direction: a crash between reserving and calling bills a request that did not happen, where the alternative would give away requests that did.

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

Those four are the whole tool surface. There is no payment, refund, order, invoice, booking, ticket, campaign, outbound-message, email, calendar, webhook or generic-HTTP tool, and a grant can only name what `app/agents/registry.py` registers.

Planned, in the phase that gives each one something to act on: `create_lead`, `update_lead`, `get_lead`, `assign_lead` (Phase 7), `send_media` (Phase 9), and later `get_product`, `get_price`, `create_ticket`, `get_order`, `check_availability`, `create_appointment`, `cancel_appointment`, `reschedule_appointment`. **None of the high-risk ones may be added yet.** A tool with an external or financial effect needs a provider-level idempotency key derived from a durable execution identity, reconciliation for an outcome nobody can determine, risk classification and an approval path — and the framework has the first of those and not the rest (see `TOOLS_TOOL_EXECUTION_FINDINGS_REMEDIATION.md`, §Future).

### What bounds a tool call

- **Arguments carry their bounds, and the bounds are published.** Length, range and pattern are declared once on the parameter, emitted into the provider schema as `minimum`/`maximum`/`minLength`/`maxLength`/`pattern`, and enforced server-side on the way in. The schema is advice to the model; `validate_arguments` is what holds (TOOL-16).
- **Text the database cannot store is refused at the boundary.** A NUL or a lone surrogate is legal JSON and is not storable text, so it comes back as a tool rejection the model can correct rather than a `DBAPIError` after the write was staged. Arabic, RTL marks and emoji are ordinary text and pass untouched (TOOL-01).
- **Calls are counted.** At most eight per model response and twelve per turn, counting every call the provider asked for — including the ones about to be refused, so a malformed call cannot buy a bypass. Past the limit the call is recorded and not run (TOOL-08).
- **A provider call id is one execution per turn.** A repeat is recorded as a duplicate and the handler does not run again (PD-TOOLS-04).
- **The final round runs only the handoff.** Any other tool's result could not be read by a later round, so the only thing it could produce is an effect nothing reasoned about — a knowledge search charged to nobody, a CRM write or a customer message planned on unverified grounds (TOOL-19).
- **A grant is re-read at the instant of the call.** The turn-start snapshot decides what the model is *offered*; it is not authorization. An administrator revoking a capability stops the calls of the turn already running (TOOL-05).
- **Human ownership outranks a stale AI decision.** A colleague who took the conversation over during the inference is the owner: the lead tool refuses, the follow-up tool refuses, and the handoff tool is a no-op that leaves the colleague's own reason and writes no agent handoff row (TOOL-06, TOOL-07). A handoff that *does* succeed stops the rest of its own response, so `[handoff, schedule_follow_up]` cannot plant a nudge on a conversation just handed over (PD-TOOLS-02).

### What a tool call leaves behind

Every call the provider asks for writes one row in `tool_executions` — the ones that ran, the ones refused before they ran, and the ones suppressed as duplicates (TOOL-12). The row names the turn, the trigger message, the agent and the conversation, carries the provider's call id, the round and the position within the response, a closed `state` and a closed `reason_code`, and the four timestamps of a lifecycle. It carries the argument *names* a call supplied and never their values, for the same reason `audit_logs.meta` does not.

"No execution row" therefore means "the provider never asked", rather than "something happened and nothing recorded it" — which is what it used to mean.

`agent_tools.config` is **reserved and not applied**. It is the right shape for the first tool that needs a per-grant knob, and until one reads it a non-empty value is refused at the API rather than stored and ignored: an administrator must not be able to believe a tool is constrained when it is not (TOOL-14).

Granting or revoking a tool writes `AGENT_TOOL_GRANTED` / `AGENT_TOOL_REVOKED` against the administrator who did it (TOOL-13), so "who gave this agent the ability to hand conversations over, to write to the CRM, to arrange a message" is answerable after the fact.

`additionalProperties: false` is declared on every tool schema, and the real provider was observed to honour it — a developer-channel instruction demanding two extra identifier fields produced a call carrying only declared ones. That is recorded as an observation rather than relied upon: `strict` is not set, so adherence is provider behaviour rather than a contract, and the boundary that actually holds is `validate_arguments` on the way in. `strict` is not adopted because it requires every property to be required, which would force the deliberately optional lead fields to be sent as nulls and lose the distinction between "not mentioned" and "empty".

Every tool that mutates writes an audit row after — never before — the mutation succeeds, with `AuditActorKind.AGENT` as the actor and the tenant and conversation taken from `ToolContext`. The model cannot influence who the row says acted; a refused or failed tool leaves no row, and neither does a lead capture that changed nothing, so a model calling the tool on every turn does not bury its own trail (TOOL-18). `meta` carries shapes only — which lead fields were filled, how long a follow-up delay was — never the customer's words, so the trail does not become a second copy of the conversation with a different retention story.

Rules the registry enforces now: every argument is validated against a declared schema before a handler runs; a handler receives a `ToolContext` carrying the tenant id, the conversation id, the session and — where one is configured — an embeddings client, so a tool cannot reach outside the workspace it was called in; an agent is offered only the tools it has been granted; and a name the registry does not know is never dispatched, so model output cannot name its way into arbitrary execution.

A tool that cannot work says so in its own output rather than failing the turn. `search_knowledge` without a configured provider returns a sentence telling the agent not to guess and to offer a handoff, which is a usable instruction; an exception would only end the turn silently. The same holds when a search *fails* — an embedding outage, an invalid query vector, a database error inside the search: it becomes `KnowledgeSearchUnavailableError`, a failed tool call the model answers around, and its database work is rolled back to a savepoint so the turn's transaction stays usable (RAG-03). Retrieved passages reach the model as one JSON object inside `function_call_output`, never as instructions or message items, and the result count, relevance threshold and context size are the server's whatever the arguments say ([RAG.md](RAG.md)).

Retrieval details in [RAG.md](RAG.md); escalation in [CRM.md](CRM.md).

## Queue and worker

Jobs move through three Redis lists — `agent:jobs:pending`, `agent:jobs:inflight`, `agent:jobs:failed` — reserved with a blocking `BLMOVE` so a job survives the death of the worker holding it (ADR-015). A job carries the tenant id, the conversation id, and optionally a specific agent.

The webhook enqueues one job per conversation that received a message, however many arrived in the delivery, and swallows a queue failure after logging `agent.enqueue_failed`: the messages are already stored, and a Redis outage must not make Meta retry the whole delivery.

The worker owns the transaction. It opens one session per job, runs the orchestrator inside it, sends any reply through `MessagingService`, and commits once — so the outbound message row and the conversation timestamps land together or not at all.

What the worker does not have is a process to run in. `AgentWorker.run_forever()` exists and nothing calls it; the entrypoint and its container service arrive with the Phase 8 worker. Nothing reaps the in-flight list yet either, so a job abandoned by a killed worker stays visible but stalled.
