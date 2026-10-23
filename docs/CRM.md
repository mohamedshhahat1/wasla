# CRM, Leads, and Conversations

Scope: contacts, conversations, human handoff, leads, and follow-ups.

Conversations, contacts and handoff are implemented (Phase 4). Leads are implemented (Phase 7). Follow-ups are implemented (Phase 8). Sentiment and automatic escalation are implemented (Phase 10) and documented in full in [SENTIMENT.md](SENTIMENT.md).

## Conversations

Fields: `tenant_id`, `contact_id`, `account_id`, `assigned_to_id`, `mode`, `status`, `handoff_reason`, `last_message_at`, `last_inbound_at`. Messages: `tenant_id`, `conversation_id`, `wa_message_id`, `direction`, `kind`, `body`, template metadata, `status`, timestamps. All messages are stored.

A conversation is scoped by connected WhatsApp number as well as by contact: a business with a sales number and a support number is holding two genuinely separate conversations with the same person.

**Contact identity is Meta's canonical `wa_id`** (CRM-20). A contact is unique per `(tenant_id, wa_id)`, and only the webhook creates contacts, from the digits-only `wa_id` Meta sends, so `+20…` and `20…` never meet today. Any future path that creates a contact from a typed or imported phone number must normalise to that same form before it looks one up, or it will create a second identity for the same person.

`ConversationStatus.PENDING` is **reserved and unreachable**: nothing writes it, and a conversation is `OPEN` or `CLOSED`. It stays in the enum because removing a PostgreSQL enum label is a table rewrite, and nothing is gained by it.

## Ownership: one rule for every transition

Every change to who owns a conversation — taking it over, releasing it, assigning it, closing or reopening it — is judged against the row **as committed at the write**: the row is locked (`FOR NO KEY UPDATE`), re-read, and only then decided (CRM-02/03/04). The loser of a race is judged against the winner's result, never overwrites it, and writes no event and no audit row. No lock is ever held across OpenAI, Meta or Redis: every provider call goes through `released`, which commits first.

Every winning transition writes one append-only `audit_logs` row (CRM-05): `conversation_taken_over`, `conversation_released_to_ai`, `conversation_assigned`, `conversation_reassigned`, `conversation_unassigned`, `conversation_closed`, `conversation_reopened`. `meta` carries the conversation, the previous and new mode, the previous and new assignee, the `source` (`user`, `agent`, `sentiment`, `system`) and whether a reason was supplied — never the reason's text, a message, or anything the customer said; the reason stays on the conversation row. A no-op writes nothing. `actor_kind` is `user` for a colleague, `agent` for the handoff tool, and `system` for sentiment and platform fallbacks (told apart by `meta.source`).

## Human handoff

Every conversation is in `AI` or `HUMAN` mode. In `HUMAN` mode automatic AI replies stop and team members reply through the system.

The rules, all locked product decisions (CRM audit crm-c4h7):

- **A colleague taking over becomes the owner** (PD-CRM-3). `POST /conversations/{id}/mode {"mode": "human"}` on an AI conversation sets `mode = HUMAN`, the handoff reason and `assigned_to_id = the caller` in one transition.
- **Taking over a conversation that is already human changes nothing** (PD-CRM-3, PD-CRM-5): not the owner, not the reason — an empty reason cannot clear one. Moving it between colleagues is the assignment endpoint, which says whom it expects to replace.
- **Automatic handoffs never invent an owner** (PD-CRM-3). The agent's `request_human_handoff`, a sentiment escalation, an empty model response and an exhausted AI allowance set `mode = HUMAN` only if the conversation is still the AI's at the write, keep an existing owner, and otherwise leave it unassigned. An "awaiting human" or "unassigned" inbox view is future work (CRM-18).
- **The handoff reason belongs to the transition that made the conversation human** (PD-CRM-5). Nothing rewrites it while it stays human: not stale automation, not another takeover. Editing it is not a feature; history is in the audit trail.
- **Releasing to AI clears the reason and keeps the owner.** It does **not** answer what the customer said while a person owned the conversation (PD-CRM-6, CRM-19): the AI answers the next new message. A reply to a backlog a colleague may already have handled would be the machine talking over them.
- A takeover cancels the **agent's** pending follow-ups and keeps a colleague's own (PD-CRM-1).

Two independent guards keep a stale AI reply from reaching a customer after a takeover: the orchestrator re-reads the mode after inference (and after every tool round), and the worker re-reads it immediately before the send. Each is pinned by its own test. The residual window between that last read and Meta's socket is the accepted Messaging boundary (CRM-17), unchanged here.

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

A move is judged against the status **as committed**, under the lead's row lock (CRM-07): two moves from one state cannot both succeed. Send `expected_status` (the status you are moving from) and a move somebody beat you to answers **409 `stale_lead_status`**; without it, a move the graph forbids from the new status is 422 as usual. Reopening a `LOST` lead while the customer has a newer open lead answers **409 `open_lead_exists`** and changes nothing (CRM-13).

The activity timeline is ordered by when each row was written (`clock_timestamp()`), not when its transaction began, with the id as a deterministic tie-break (CRM-16). Lead mutations are serialised on the lead row, so the timeline's order is the order the changes were applied in, and its last status move agrees with the lead.

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

**Human verification permanently outranks AI, under concurrency too** (PD-CRM-7, CRM-06). The model composes its values with no lock held; they are *applied* under the lead's row lock against the lead as committed, so a correction a colleague committed a moment earlier is seen and skipped. A person's edit takes the same lock, so two colleagues editing different fields end with the **union** of their verified fields, never one of them.

Example, from "My name is Ahmed, I want to finish a 150m apartment and my budget is 500000":
`name=Ahmed`, `interest=150m apartment finishing`, `budget_amount=500000`.

### Notes and activity

`lead_notes` holds internal text written by a person or an agent; notes are never sent to the customer. `lead_activities` is an append-only log of what changed, who changed it (`user`, `agent` or `system`), and the previous value. There is no route that edits or removes an entry: an audit trail the application can rewrite does not answer the question it exists to answer.

### Assignment

Assignment goes through the existing membership system — the assignee must hold an **active** membership in the workspace, verified rather than assumed, because the id arrives in a request body.

Conversation and lead assignment both carry a **required** `expected_assigned_to_id` (null is a value): the owner the caller believes it is replacing — `assigned_to_id` from the read they are looking at. If that is no longer the owner the answer is **409 `stale_assignment`** and nothing changes; the caller reads again (PD-CRM-4). Last-writer-wins let two colleagues both be told they owned a customer, and let a stale screen undo a manager's reassignment. Any active member may reassign a conversation (PD-CRM-10); lead assignment stays administrative.

An assignment share-locks the assignee's membership, after key-share-locking the workspace row — the order a removal takes them in — so it cannot race a removal: whichever reaches the member second waits for the other.

### When a colleague leaves

Removing a member — or the member leaving, or closing their account — does the following **in the same transaction** (PD-CRM-2, PD-CRM-8):

- unassigns every conversation and lead they own (audited as `conversation_unassigned`, or a lead `unassigned` activity, with `cause = member_revoked`), rather than handing them to whoever removed them;
- cancels their pending follow-ups with `cancelled_reason = member_revoked`; dispatch re-checks the author, for one a retry carried past the removal.

Notes, activity, audit rows and follow-ups already sent stay attributed to them. Deleting a whole workspace revokes everyone and serves nobody; it does not unassign, and the invariant queries skip deleted workspaces.

### Linked records agree

A lead's contact and conversation, a follow-up's conversation and lead, and a note's or activity's lead all belong to the same workspace — enforced by composite foreign keys (ADR-100, CRM-01). When a lead names both a contact and a conversation, the conversation is with that contact (a three-column key on `leads`; CRM-14). A follow-up's `lead_id` must be this workspace's lead **of the conversation's customer**: another workspace's or a nonexistent id is the same 404, another customer's is 422.

Every human CRM text field refuses NUL and unencodable text with a 422 (CRM-12); Arabic, emoji and every other character a person can type are kept exactly.

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

A follow-up is a promise to say something later unless the customer speaks first. There are two kinds: **one-off** follow-ups, which a person schedules over the API (or the agent, through `schedule_follow_up`, where sequences do not cover the conversation), and **sequence steps**, the owner's automatic reminders (ADR-151).

### Sequences: the owner's reminders (ADR-151)

The workspace's follow-up settings (`GET`/`PUT /api/v1/workspace/follow-up-settings`) hold `enabled`, the platforms (`channel_types`, WhatsApp only while it is the one working adapter) and up to five reminders of 5 minutes to 23 hours each, with optional guidance for the AI. Every workspace starts **on, WhatsApp, one reminder after 5 minutes**.

- **Start.** When the AI or a person sends a free-form reply on an AI-owned, open conversation of a covered platform, and the customer has written since the last run began, a run starts: step 1 is due that reply plus the first reminder. Each later step is due the previous step's **send** plus its reminder. Another reply restarts the run from step 1 (`sequence_restarted`); a business message to a customer who has not written since never starts one. Only the next step is ever waiting.
- **The AI writes each step when it is due**, from the conversation itself, and may decide not to send (`concluded`, `purchased`, `declined`, `waiting_on_business`, `inappropriate`, `other`): a skip ends the run. A text the guardrails refuse (over the channel's length, a credential shape, a phone number) is skipped `guardrail`. A model that fails twice ends the run `ai_unavailable`. A step that asked the model costs one AI turn; a step cancelled before costs nothing.
- **Never outside the window.** A step is sent only while at least `FOLLOW_UP_WINDOW_MARGIN_SECONDS` (30 minutes) of the customer's 24-hour window remain: a later step is never scheduled (`window_closing`), and one that came due too late is skipped (`window_closed`). A step is always free text - never a template, never a message tag.
- **Stops.** The run ends, in the transaction that causes it, when the customer writes (`customer_replied`; `opted_out` for a STOP), a person takes over or is assigned (`human_took_over`), the conversation is closed (`conversation_closed`), the customer opts out on that channel (`opted_out`), the lead is won or lost (`lead_closed`), or the settings stop covering it (`settings_changed`: turned off, platform removed, or the waiting reminder deleted). At dispatch also: the workspace not served (`not_served`), the allowance exhausted (`allowance_exhausted`). Wasla records no purchases; a customer who says they will buy is the AI's `purchased`.
- **Any hour.** Steps are not bound by quiet hours: the customer wrote inside the window.
- **Who owns the slot.** While sequences cover a conversation the agent is not offered `schedule_follow_up` (a call is refused with readable text); a person's one-off replaces the waiting step and ends the run (`replaced_by_person`); a colleague's waiting one-off keeps any run from starting.
- **What a person sees.** `GET /conversations/{id}` carries `follow_up` (state, step of total, due time with its local twin, how it ended); `GET /conversations/{id}/follow-ups` is the history (the text of sent steps only); `DELETE /conversations/{id}/follow-up` stops the run (`stopped_by_person`, audited).

### Model

`follow_ups` carries `tenant_id`, `conversation_id`, `lead_id`, `scheduled_at`, `requested_at`, `deferred_reason`, `deferrals`, `status`, `body`, `template_name`, `template_language`, `template_components`, `header_media_id`, `reason`, `created_by_id`, `created_by_kind`, `attempts`, `last_error`, `sent_at`, `cancelled_at`, `cancelled_reason`, `message_id` and, for a step, `origin = sequence`, `sequence_run_id`, `step_index`, `anchor_at` and `skip_reason`. `follow_up_sequence_runs` holds a run's anchor, state, `end_reason`, `ended_at` and `steps_sent`.

The row carries both what to say inside the service window (`body`) and which approved template to use outside it, because which one applies is not known until the moment it comes due.

### One pending nudge per conversation

Enforced by a partial unique index. Scheduling again while one waits **reschedules** it rather than adding a second — an agent that decides to follow up on every turn would otherwise stack notifications on one customer's phone. A finished follow-up releases the slot, so a conversation can be followed up again later.

### Cancellation on reply

A customer's reply cancels the waiting nudge, and this happens on the **inbound webhook path**, in the same transaction that stores the message. Leaving it for the worker would allow a sweep between the reply landing and the cancellation being visible. A delivery status is not a reply and cancels nothing.

Cancelling something already sent returns it untouched with its real status — `sent` says the nudge went out.

### Who a follow-up belongs to

A colleague's own follow-up (`created_by_kind = user`) is a person's reminder, not the AI's (PD-CRM-1): it is accepted on a conversation a person owns, **survives a takeover, and is sent**, subject to every ordinary guard (workspace served, conversation open, customer not opted out, window and template rules). An agent's follow-up is the AI: it is refused on a human-owned conversation at scheduling, cancelled by a takeover, and skipped at dispatch if the conversation became human.

### Claims, rescheduling and cancelling (CRM-09, CRM-10)

The sweep claims a due row by stamping it with a `claim_token` and `claimed_until`; `scheduled_at` is only ever what somebody asked for. A worker may send only while the row still carries its exact token. Rescheduling or cancelling a claimed row clears the token, so a worker holding it sends nothing and the new time and text are what go out. Once the send intent has committed the customer may have the message, so a reschedule or cancel answers **409 `dispatch_in_progress`** and the row ends `SENT` (or `FAILED`) — never `CANCELLED` naming a sent message.

`scheduled_at` must carry a UTC offset (`Z`, `+02:00`, …); a naive time is **422** (PD-CRM-9). It is stored normalised to UTC, and kept as `requested_at`: a wait for Meta's limit (`deferred_reason: portfolio_limit`, FLIM-01) or for the workspace's quiet hours (`quiet_hours`, a template follow-up only - FQH-01) moves `scheduled_at` and never `requested_at`, and spends no attempt; editing the quiet hours or the zone re-evaluates a waiting follow-up at the next sweep (FQH-03). The workspace has a time zone (`tenants.timezone`), but the API does not guess an offset for a wall time; `GET /api/v1/workspace/local-time` resolves one (TZ-08).

### Window and template compliance

This table is a one-off follow-up's; a sequence step is free text inside the window or nothing (above).

| Situation | What happens |
| --- | --- |
| Inside the 24-hour window, has a body | Free text is sent, at any hour |
| Outside the window, has an approved template | The template is sent - outside the workspace's quiet hours (it waits for them, `quiet_hours`) and under Meta's whole limit (it waits for the window to free, `portfolio_limit`) (ADR-152, ADR-153). A media header uses a file of the workspace's library (`header_media_id`, ADR-154) |
| Outside the window, no template | **`SKIPPED`** — not sent, recorded, never retried |
| Conversation closed before it came due | `SKIPPED` |
| Send attempted and rejected | Retried with widening backoff, then `FAILED` |

`SKIPPED` and `FAILED` are deliberately different states. `FAILED` means an attempt broke and may work later. `SKIPPED` means Wasla decided not to send because sending would breach WhatsApp's rules — a policy outcome that retrying can never fix, since the window does not reopen on its own. The reason is written to `last_error` either way, so a workspace can see why its nudge never went out.

**Closed in Phase 11.** The template registry now answers whether Meta has approved a template, and a follow-up asks it twice: when the nudge is scheduled, where a person is present to fix the problem, and again before the send, because Meta pauses a template without warning and hours pass between. A refusal at dispatch is `SKIPPED` like any other policy outcome. A template the registry has never heard of is still allowed through — a workspace that has not synced cannot be told apart from one whose template is genuinely unknown, and refusing there would break every follow-up it has. See [CAMPAIGNS.md](CAMPAIGNS.md).

### Delivery

A polling worker sweeps every 30 seconds for rows whose time has come, claiming them with `SELECT ... FOR UPDATE SKIP LOCKED` so two replicas cannot send the same message twice ([ADR-022](../DECISIONS.md)). A follow-up therefore fires within one poll interval of its due time rather than exactly at it.

## Sentiment and priority

**Status: Implemented.** Full detail in [SENTIMENT.md](SENTIMENT.md); the short version follows.

Every customer message is classified — `positive`, `neutral`, `negative` or `angry`, with a score, an intent and a confidence — before an agent is allowed to answer it. The current reading sits on the conversation, which is what the inbox filters on; every reading is kept on `message_sentiments`, which is the audit trail and the time series [ANALYTICS.md](ANALYTICS.md) will count.

A bad reading raises priority (`negative` → `high`, `angry` → `urgent`) and never lowers it; a person gives it back through `POST /conversations/{id}/priority`. Above the agent's configured threshold and above a confidence floor it also hands the conversation to a human and stops the agent replying, with a reason that says the handoff was automatic.

Escalation analytics events are not written yet: there is no analytics event table until Phase 12, and `message_sentiments` already carries the timestamped rows those counts will read.
