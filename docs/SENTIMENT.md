# Sentiment and escalation

**Status: Implemented** — reading, storage, priority, automatic handoff, the inbox filter and the manual priority reset, exercised against real PostgreSQL.

Every message a customer sends is classified before an agent is allowed to answer it. The reading decides two things: how urgently the conversation wants a person's attention, and whether the agent should stop talking altogether.

## Where it runs, and why there

Inside the agent turn, after the agent is resolved and before the first word is composed.

That order is the whole feature. An escalation that arrives after the reply means the AI already answered an angry customer — and a reply written and then discarded has still cost the tokens, and worse, anything its tools did during the turn has already happened. Placing the check late would produce a system that escalates correctly and helps nobody.

It sits in the orchestrator rather than in the worker, beside the `HUMAN`-mode guard it resembles, so the decision travels with the turn rather than with one caller.

```
agent job claimed
    |
conversation loaded ---- HUMAN mode ----> nothing to do
    |
agent resolved --------- not active ----> nothing to do
    |
newest inbound message
    |
already read? -- yes --> reuse the stored reading, pay nothing
    |
    no
    |
classify (one small call, schema-constrained, temperature 0)
    |
store the reading; update the conversation
    |
escalate? -- yes --> mode = HUMAN, reason recorded, agent stays silent
    |
    no
    |
the agent composes its reply as usual
```

## What is stored

Two places, deliberately:

| Where | What | For |
| --- | --- | --- |
| `conversations` | `sentiment`, `sentiment_score`, `priority`, `intent`, `intent_confidence` | The state an inbox sorts and filters on |
| `message_sentiments` | one row per analysed message | History, the audit trail, and the time series analytics will count |

The split is the one media already makes. `messages` is the table every conversation read touches, so it stays narrow; and a single current value cannot answer "why was this escalated" after the fact.

`message_sentiments.message_id` is unique. That constraint is the idempotency key: a retried agent job must not pay for a second inference on a message already read, and the database enforces it rather than a check in a service.

## What is classified

The customer's own words. That means the message body, and a voice note's transcript, joined.

It does **not** mean a photograph's description. A transcript is what the customer said; a description of an image is this system's own prose, and reading a mood off it would be treating our inference back as evidence. A photograph with no caption is therefore not classified at all.

The prompt names the two failure modes that would produce wrong escalations:

- A customer describing an angry neighbour, a difficult week or a broken product **is not an angry customer** unless they are directing it at the business.
- Directness, short messages and absent pleasantries are ordinary in Egyptian Arabic and in many other languages. The register is explicitly not evidence of anger.

The answer comes back in a schema the provider enforces (`text.format`, `strict: true`), not as JSON requested in a prompt — that approach fails on exactly the traffic that matters here, the unusual message. Temperature is zero: this is classification, and a rule that fires intermittently is worse than one that does not fire at all.

## What a reading does

**Priority goes up, never down.**

| Reading | Priority becomes |
| --- | --- |
| `angry` | `urgent` |
| `negative` | `high` |
| `neutral`, `positive` | unchanged |

A customer who was furious five minutes ago and is now merely terse has not stopped being a problem, and a conversation quietly demoted out of somebody's queue is one nobody looks at again. Giving the priority back is a person's decision, made through `POST /api/v1/conversations/{id}/priority`.

**Handoff is configured per agent.** `Agent.escalation_sentiment` is the threshold: a reading at least that severe hands the conversation to a person and stops the agent replying. It defaults to `angry`, including for agents that existed before this phase — the migration adds the column with a server default rather than a null, so nobody is silently opted out of a feature they were never asked about. Setting it to null switches automatic handoff off while still taking the reading and still raising the flag.

The handoff reason says the decision was automatic. A colleague picking up a conversation opens with entirely different words depending on whether a customer asked for them or a classifier decided.

## Three things a reading deliberately does not do

**Below the confidence floor (0.6) it never silences an agent.** It still raises the flag — a wrong flag on an inbox costs nothing — but a wrong silence leaves a customer waiting on a colleague nobody told. The model's self-reported confidence is weakly calibrated, so it is used as a floor and never as evidence that a reading is right.

**A provider failure costs nothing but the reading.** Every `ExternalServiceError` and `RateLimitedError` is contained and logged; the turn continues and the customer still gets an answer. A reading is an enhancement, the reply is the product.

**A message already judged is not judged again.** Beyond saving the call, this is what stops a conversation a colleague deliberately handed back to the AI from re-escalating on words that were already read, before the customer has said anything new.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `OPENAI_SENTIMENT_MODEL` | `gpt-4.1-mini` | One small call per customer message, so this is the model whose cost tracks traffic |

A deployment with no OpenAI key takes no readings and answers customers exactly as it did before.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/v1/conversations?priority=urgent` | Narrows the inbox to one level. Filters, does not reorder |
| `POST /api/v1/conversations/{id}/priority` | Sets priority by hand. The only way it comes down |
| `PATCH /api/v1/agents/{id}` | `escalation_sentiment` — the threshold, or null to switch handoff off |

`ConversationRead` carries `sentiment`, `sentiment_score`, `priority`, `intent` and `intent_confidence`, so a client can render why a conversation is flagged rather than only that it is.

## Known limits

- **Nothing acknowledges the customer on escalation.** The agent goes quiet and a person is flagged. Phase 11 removed the obstacle — there is now a registry of approved templates one could be sent from — but the remaining question is not a technical one: what a business says to a customer it has just decided is angry is the sentence most likely to make things worse, and it has to be that workspace's own words rather than a default. It is agent configuration, and it belongs with escalation rather than with campaigns; still open.
- **Intent is a free-form label, not an enum.** Common ones are suggested in the prompt so reports group cleanly; a genuinely new intent is recorded in the model's own words rather than forced into "other".
- **Only the newest inbound message is read.** Messages Meta delivers in one webhook share a `created_at` — it is the transaction's start time — so which of them is judged falls to the id tie-break. They arrived in the same moment and carry the same mood; the alternative is a monotonic column on the busiest table in the schema.
- **Nothing re-reads a conversation as a whole.** A customer whose mood curdles over ten polite messages is judged one message at a time.
