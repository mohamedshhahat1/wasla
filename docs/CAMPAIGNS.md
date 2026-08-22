# Campaigns and templates

**Status: Implemented** — the template registry, campaign composition and targeting, the rate-limited worker, delivery statistics and marketing opt-out, exercised against real PostgreSQL.

A campaign is the one thing Wasla does that writes to thousands of people who did not just say something. Everything in this document exists because of that sentence.

## The template registry

Templates are drafted and approved in the WhatsApp Business Manager. Wasla stores a copy of what Meta says about them, refreshed by an explicit sync.

The copy earns its place because two things need the answer in a transaction rather than behind a network call: a follow-up leaving the 24-hour service window, and a campaign about to write to ten thousand people.

| | |
| --- | --- |
| Identity | `(account, name, language)` — Meta's own |
| Sendable | `approved`, and nothing else |
| Gone from Meta | marked `disabled`, never deleted — a campaign may reference it, and the reason it stopped working is worth keeping |
| An unrecognised status | `unknown`, which is not sendable |

That last row is the point of having an `unknown` member at all. Meta's vocabulary grows without warning, and a status this code has never seen must land somewhere that fails closed rather than being guessed into one that sends.

**The registry can be stale, and says so.** Meta pauses a template that draws complaints without telling anyone, so a row reading `approved` means "approved when we last looked". Sends are still attempted against Meta's own answer and a rejection is recorded as one; the registry stops the sends that are obviously wrong, not every one.

### What the registry is allowed to refuse

There is an asymmetry here that is easy to lose in a later edit.

- A template the registry has **never heard of** is allowed through. A workspace that has not synced cannot be told apart from one whose template is genuinely unknown, and refusing there would break every template-bearing follow-up it has.
- A template the registry **does** know and Meta has not approved is refused, because there the answer is real.

Campaigns apply a stricter rule of their own: the template must exist in the registry and be approved. A campaign is a new thing a workspace sets up deliberately, so requiring a sync first costs it one click rather than a regression.

## Who a campaign can reach

**Only people who already have a conversation with this business on the sending number.** There is no route that uploads phone numbers, imports a list, or otherwise creates a recipient who is not already a contact of the workspace ([ADR-025](../DECISIONS.md)).

That absence is the compliance boundary, not a missing feature. An upload turns a customer engagement platform into a spam tool without any further decision by anybody, and it moves the entire consent story into a promise nothing here can check — a promise WhatsApp will hold the *number* responsible for rather than the claim.

Within that population, filters narrow. None of them widens:

| Filter | Narrows to |
| --- | --- |
| `last_inbound_within_days` | People who wrote recently |
| `lead_statuses` | People with a lead at one of these stages |
| `contact_ids` | Named contacts of this workspace |

**Opt-out is part of the base population, not a filter.** It lives inside `AudienceRepository` so no future endpoint can build an audience that omits it, and it is re-checked at send time — a campaign may run for hours, and somebody who says stop in the middle of one must not receive the rest of it.

### The audience is written down, not computed

`POST /campaigns/{id}/audience` materialises one row per recipient, and does so only while the campaign is a draft.

A list computed as the campaign ran would change under its feet: a contact who writes in halfway through drops out of a filter defined by silence, and afterwards nobody could answer "who was this sent to". Rebuilding the list of a campaign that has already sent to part of it would either duplicate those people or silently drop them, so it is refused.

`UNIQUE(campaign_id, contact_id)` is the idempotency key. A campaign of ten thousand people is ten thousand chances for a worker to die halfway, and the only way a restart can know what it already sent is a state per person — enforced by the database, because a check in a service would have to hold across every replica at once.

## How a campaign sends

```
scheduled, and its moment has come
    |
claimed with FOR UPDATE SKIP LOCKED
    |
number still active? template still approved? -- no --> FAILED, reason recorded
    |
claim the next batch of pending recipients (also SKIP LOCKED)
    |
for each: opted out? -- yes --> SKIPPED, never retried
    |                            (a policy outcome, not an error)
    |
    send the approved template; record the message row
    |
write next_send_at = now + batch / messages_per_minute
    |
nobody left? -- yes --> COMPLETED
```

**The rate limit is a timestamp on the row, never a sleep** ([ADR-026](../DECISIONS.md)). A sleep would hold the campaign's lock while doing nothing, would not survive a restart — the replacement would start by sending immediately, exactly during a rolling deployment — and would not compose across replicas, because two workers each pacing correctly produce twice the rate.

The default is 60 messages a minute and the ceiling is 600. Both are far below what Meta will accept. This is a quality limit, not a throughput one: a number that suddenly writes to ten thousand people is the pattern that collects blocks, and a blocked number takes the whole business down rather than one campaign.

The worker holds one HTTP connection pool per sweep, so a batch of fifty is fifty requests over one connection rather than fifty TLS handshakes to the same host.

## Stopping

`PAUSED` and `CANCELLED` are deliberately different states.

- **Paused** is stopped and resumable. The remaining recipients keep waiting; scheduling again resumes.
- **Cancelled** is finished. What was sent stays sent, and the rest never goes.

Somebody watching a broadcast go out and having second thoughts needs an action that is not destructive, which is what pause is for. `FAILED` is different again: it means the campaign could not run at all — its template was withdrawn, its number was disabled — as distinct from one that ran and had some recipients fail, which completes.

## Marketing opt-out

Recorded on the contact as a timestamp and a source, not a boolean. "Since when" is the question a dispute about a marketing message actually turns on, and a colleague's note is not the same fact as a customer's own refusal.

### A customer saying stop

Honoured on the inbound path, in the same transaction that stores the message. That closes a real window: between the message arriving and a worker reading it, a campaign sweep could write to somebody who has already said no, and nothing takes that message back.

The matcher is as crude as it can be, and the crudeness is the design. It fires only when the **entire** message is one of a short list of words that mean nothing else — `stop`, `unsubscribe`, `الغاء`, `ايقاف` and a few more. "stop" is a refusal; "stop by the shop tomorrow" is not, and a matcher that looked for the word anywhere would confuse the two constantly.

The asymmetry sets the boundary. A false positive stops marketing to somebody who did not quite mean it, which a colleague undoes in a moment and the customer can always write again. A false negative keeps promoting to somebody who has asked twice, which is how a business loses its number.

Arabic is folded for keyboard variation — the hamza-bearing alefs, alef maksura, teh marbuta and the diacritics — so which spelling a customer's phone produces does not decide whether they are left alone.

**It does not silence the agent.** Someone refusing marketing is not refusing an answer, and deciding otherwise from one word would leave people talking to nobody.

**It does not read sentences.** "please take me off your list" is not recognised. That is an accepted limit rather than an oversight: the alternative is a model call on every inbound message to decide something a person can record in one click.

## Statistics

| Count | Comes from |
| --- | --- |
| `pending`, `sent`, `failed`, `skipped` | The recipient rows |
| `delivered`, `read` | The message rows a delivery webhook advances |

A message Meta accepted is `sent`; whether it arrived is Meta's news to bring. A read message was also delivered, so `delivered` includes it — counting the two statuses separately would undercount delivery by everyone who read it.

`skipped` carries its reason on the recipient row, which is what tells a workspace somebody was left out because they had opted out rather than because something broke.

## API

| Route | Who | Purpose |
| --- | --- | --- |
| `GET /api/v1/templates` | member | What may be sent |
| `POST /api/v1/templates/sync` | admin | Refresh the registry from Meta |
| `GET /api/v1/campaigns` | member | Newest first, keyset paged |
| `POST /api/v1/campaigns` | admin | Compose a draft |
| `POST /api/v1/campaigns/audience/preview` | admin | Count a filter without writing anything |
| `POST /api/v1/campaigns/{id}/audience` | admin | Materialise the recipient list (drafts only) |
| `POST /api/v1/campaigns/{id}/schedule` | admin | Send, now or at a time |
| `POST /api/v1/campaigns/{id}/pause` | admin | Stop, reversibly |
| `POST /api/v1/campaigns/{id}/cancel` | admin | Stop, finally |
| `GET /api/v1/campaigns/{id}/statistics` | member | Outcomes and delivery |
| `GET /api/v1/campaigns/{id}/recipients` | member | Who it reached, and who it did not |
| `POST /api/v1/contacts/{id}/opt-out` | member | Record a refusal |
| `DELETE /api/v1/contacts/{id}/opt-out` | admin | Undo one recorded in error |

Reading is ordinary inbox work. Composing, targeting and starting a campaign take an administrator, because a campaign writes to thousands of customers at once and is the least reversible thing this platform does. Recording an opt-out is any member's to do — the person handling the conversation is the one a customer says "stop" to — while clearing one takes an administrator, because undoing somebody's own refusal should be deliberate.

Lifecycle changes are named transitions rather than a `PATCH` on `status`. Status is the only field with an operational meaning, and naming the transition keeps both the audit trail and the authorization legible.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `WORKER_KINDS` | all | Include `campaign` to run the sending loop; a workspace mid-broadcast is the case that most wants a replica of its own |

There is no campaign-specific credential. Sends use the platform Meta token, as every other outbound path does ([ADR-009](../DECISIONS.md)).

## Known limits

- **No per-recipient personalisation.** A campaign's template variables are one list for the whole send. Filling `{{1}}` with each customer's name needs a source of per-recipient facts that nothing here has yet.
- **The rate is per campaign, not per number.** Two campaigns running at once on one number can exceed either one's rate. A per-number budget needs a shared counter, and a workspace that starts two simultaneous broadcasts has made a decision this system can surface but should not silently override.
- **No import, deliberately.** See [ADR-025](../DECISIONS.md). Adding one means answering the consent question, not writing a CSV parser.
- **No campaign analytics events.** Counts come from the recipient and message rows; the analytics event table arrives in Phase 12, and writing a second shape now would mean migrating two later.
- **Nothing sweeps completed campaigns.** Recipient rows accumulate for the life of the workspace. They are the record of who was written to and when, so a retention policy is a product decision rather than a cleanup job.
- **Opt-out is all-or-nothing.** A contact who opts out is out of every campaign, including a utility template they might have wanted. Per-category consent needs a vocabulary a business can actually explain to a customer.
