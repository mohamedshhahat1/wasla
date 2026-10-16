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

### Keeping the registry current

Sync is an administrative action and nothing performs it on a schedule, so a template Meta pauses stays `APPROVED` locally until somebody clicks it — and every campaign and follow-up using it is refused by Meta one at a time in the meantime.

A refusal is now written back. When Meta declines a send with one of its template error codes, the local row is marked `PAUSED` with the code recorded, so the next send is refused locally without asking. Three deliberate narrownesses:

- Only codes that unambiguously mean *this template may not be sent* are acted on. Marking a template invalid on an ambiguous error takes a working template away from a workspace, and getting it back needs a manual sync.
- It is recorded as `PAUSED` whatever the code, because pausing is the reversible state and a sync is what establishes which of the three Meta actually means.
- A template the registry has never heard of does not get a row invented for it. That would turn one refusal into a permanent local block on a name the workspace may never have synced.

Periodic background sync is **not** implemented. The write-back covers the case that costs a workspace its number — sending a template Meta has withdrawn, repeatedly — and a sweep across every account's templates is a third-party call budget nobody has needed yet.

## Who a campaign can reach

**Only people who already have a conversation with this business on the sending number.** There is no route that uploads phone numbers, imports a list, or otherwise creates a recipient who is not already a contact of the workspace ([ADR-025](../DECISIONS.md)).

That absence is the compliance boundary, not a missing feature. An upload turns a customer engagement platform into a spam tool without any further decision by anybody, and it moves the entire consent story into a promise nothing here can check — a promise WhatsApp will hold the *number* responsible for rather than the claim.

Within that population, filters narrow. None of them widens:

| Filter | Narrows to |
| --- | --- |
| `last_inbound_within_days` | People who wrote recently |
| `lead_statuses` | People with a lead at one of these stages |
| `contact_ids` | Named contacts of this workspace |

**Saved segments** ([ADR-149](../DECISIONS.md), PG-06) are named sets of these
rules - recency and lead statuses, never contact ids - at most 100 per
workspace (`/api/v1/campaign-segments`). Writing an audience from a segment
copies its rules onto the campaign (`audience`, with `audience_segment_id`);
filters sent with it can only narrow them, and editing or deleting the segment
later changes no campaign that used it.

**The contact picker** (`GET /api/v1/contacts`, PG-07) searches the workspace's
contacts by name or number and, on a chosen number, says who a campaign could
reach (`eligible_for_campaign`, `ineligible_reason: opted_out`). Picking
somebody still goes through the audience, filtered by the base population.

**Opt-out is part of the base population, not a filter.** It lives inside `AudienceRepository` so no future endpoint can build an audience that omits it, and it is re-checked at send time — a campaign may run for hours, and somebody who says stop in the middle of one must not receive the rest of it.

### The audience is written down, not computed

`POST /campaigns/{id}/audience` materialises one row per recipient, and does so only while the campaign is a draft.

A list computed as the campaign ran would change under its feet: a contact who writes in halfway through drops out of a filter defined by silence, and afterwards nobody could answer "who was this sent to". Rebuilding the list of a campaign that has already sent to part of it would either duplicate those people or silently drop them, so it is refused.

`UNIQUE(campaign_id, contact_id)` is the idempotency key. A campaign of ten thousand people is ten thousand chances for a worker to die halfway, and the only way a restart can know what it already sent is a state per person — enforced by the database, because a check in a service would have to hold across every replica at once.

## How a campaign sends

```
scheduled or running, due, and its lease (next_send_at) has run out
    |
claimed with FOR UPDATE SKIP LOCKED; next_send_at := lease, lease_token := new  -> COMMIT
    |
re-taken by id only while the row still carries this sweep's lease_token (ADR-134)
    |
workspace served? subscription not suspended? -- no --> PAUSED, status_reason (ADR-135)
number active? template approved, MARKETING, parameters fit? -- no --> FAILED, status_reason
deadline (send_until) passed? -- yes --> pending SKIPPED send_window_closed, COMPLETED
inside the workspace's quiet hours? -- yes --> WAIT quiet_hours until the window ends
number's messaging limit never read? -- yes --> WAIT limit_unknown, asked again in 5 min
    |
claim the next batch of pending recipients not deferred (also SKIP LOCKED)
    |
image / video / document header? Meta's id fresh (< 25 days, this number)? -- yes --> use it
    -- no --> hold, read the stored file (SHA-256 checked), upload it to Meta,
              write the id only while the lease is still this worker's (ADR-144)
    |
for each recipient:
    hold: campaign still scheduled/running and still this worker's? renew the lease
          -- no --> stop here (a pause, a cancel, or a lost lease)
    workspace still served? -- no --> PAUSED, stop here
    quiet hours began? deadline passed? -- yes --> stop here (the wait or the deadline)
    take: still pending and nobody else's? (one SELECT ... FOR UPDATE SKIP LOCKED,
          the row re-read) -- no --> skip this person, no send, no attempt
    opted out on this channel? -- yes --> SKIPPED opted_out, never retried
    linked to a message that may have left? -- yes --> FAILED delivery_unconfirmed
    values written for another contact? -- yes --> SKIPPED identity_changed (CR-15)
    Meta's limit: under the portfolio's lock, past 80 %? -- yes --> WAIT daily_limit
    marketing in the last 24 h to this contact? -- yes --> deferred a day, no attempt
    send the template with this person's own values; record the message row
    |
write next_send_at = batch end + batch / messages_per_minute; clear lease_token
    |
nobody left (deferred people still count)? -- yes --> COMPLETED
only capped people left? -- yes --> WAIT contact_cap
```

**A wait is not a pause** (ADR-145..ADR-147). Quiet hours, Meta's daily limit,
the per-customer cap and an unread limit leave the status `scheduled` or
`running`, spend no attempt, re-arm nobody and keep no lease: the campaign
records `waiting_reason` and `resumes_at` and the next sweep after that moment
claims it again. Each transition into a wait is audited once
(`campaign_waiting`, system) and counted once
(`wasla_campaign_waits_total{reason}`); a campaign that sends again clears it.

**One owner per recipient, whatever the number of workers** ([ADR-134](../DECISIONS.md), CMP-01). A batch's row locks end at its first send commit ([ADR-093](../DECISIONS.md)), so ownership is re-established before every person in two single statements: `hold` (the campaign is live and still carries this worker's `lease_token`, and the lease is renewed) and `take` (the recipient is still pending and not locked by another worker, re-read from the database). The lease therefore has to outlast one send - worst case 3 x 10 s plus 2 x 45 s of throttle waits, 120 s - not a batch; `CLAIM_LEASE_SECONDS` is 300. A lease that does run out is re-taken with a new token; the stalled worker stops at its next recipient, and the one send it had in flight was linked to its recipient before Meta was asked, so the new owner abandons rather than resends it. Campaign replicas are safe to run from this release on.

**A header image, video or document is uploaded once per campaign** ([ADR-144](../DECISIONS.md)). The workspace uploads the file to Wasla (`POST /api/v1/campaigns/header-media`), never a link; it is stored like any attachment and counts against the workspace's storage. The worker holding the campaign's lease reads it back, checks its SHA-256, uploads it to Meta through the number's own credential and records Meta's id - conditioned on its lease token, in its own transaction - before the first recipient is taken. Every recipient's header carries that one id. Two or four workers upload it once; a worker whose lease was taken over during the upload writes nothing and sends nothing (its file at Meta goes unused and expires). Meta's ids expire after 30 days, so an id older than `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS` (25 days) - after a long pause, say - is uploaded again before the batch.

| Header file problem | What happens |
| --- | --- |
| stored file gone, or not what was stored | `PAUSED header_media_unavailable`; nothing sent to Meta |
| Meta refuses the upload (a 4xx with a code) | `FAILED header_media_refused`, Meta's code in `last_error`; the template stays approved |
| Meta not reachable for the upload | waits, no attempts spent; `PAUSED provider_unreachable` after the bound |
| throttled, 5xx, read timeout, 2xx without an id | no id recorded; the next batch uploads again |
| a send answered 131053 | uploaded again once and that person resent; a second 131053 fails the campaign `header_media_refused` after two sends |
| the credential refused or unreadable | `FAILED credential_refused` |

No recipient spends an attempt for a header problem, and a header problem never fails recipients one by one.

**What counts as sent.** The recipient row moves to `SENT` in the transaction that meters the send; the per-recipient claim is what makes that pair safe from double counting across workers. The invariant `cr02_campaign_meters_differ_from_recipients_sent` checks it.

**Meta's answers** ([ADR-139](../DECISIONS.md)):

| Answer | What happens |
| --- | --- |
| 2xx with a message id | `SENT` |
| 2xx without one, 5xx, read timeout, reset | uncertain: `FAILED delivery_unconfirmed`, never resent |
| 132000 / 132012 / 131008 (parameters) | campaign `FAILED template_parameters_refused` after one request; the template is not paused |
| 132001 / 132015 / 132016 (template) | registry `PAUSED`; campaign `FAILED template_withdrawn`; nobody else asked |
| 131049 (per-user marketing limit) | that person `FAILED marketing_limit_per_user` at once - Meta says wait 24 h |
| 131026 (undeliverable) | that person `FAILED undeliverable` at once |
| 131050 (stopped marketing) | that person `SKIPPED`, opt-out recorded on the channel |
| 131056 (pair rate limit) | that person deferred a minute (`not_before`), attempts kept; the rest go on |
| 429 / 130429 / 4 / 80007 | the client retries, then the campaign waits; no attempts spent |
| 401 / 190 / 368 and connection codes | campaign `FAILED credential_refused` / `connection_refused` after one request |
| a bare 403 | per message (the classifier, OMNI-035): retried until the attempts run out |
| no connection at all (connect error, connect or pool timeout) | the campaign waits a minute, no attempts spent; `PAUSED provider_unreachable` after `CAMPAIGN_PROVIDER_UNREACHABLE_PAUSE_HOURS` |

**The rate limit is a timestamp on the row, never a sleep** ([ADR-026](../DECISIONS.md)). A sleep would hold the campaign's lock while doing nothing, would not survive a restart — the replacement would start by sending immediately, exactly during a rolling deployment — and would not compose across replicas, because two workers each pacing correctly produce twice the rate.

The default is 60 messages a minute and the ceiling is 600. Both are far below what Meta will accept. This is a quality limit, not a throughput one: a number that suddenly writes to ten thousand people is the pattern that collects blocks, and a blocked number takes the whole business down rather than one campaign.

The worker holds one HTTP connection pool per sweep, so a batch of fifty is fifty requests over one connection rather than fifty TLS handshakes to the same host.

## Quiet hours

Every workspace has a time zone, chosen at onboarding ([ADR-145](../DECISIONS.md)),
and quiet hours in it - 22:00 to 09:00 by default, on for everybody
(`GET/PUT /api/v1/workspace/campaign-settings`). No campaign sends inside them:
the worker asks before a batch and before every recipient, and a campaign that
reaches the window's start stops at the next person and waits for its end,
computed as a local wall time with that date's rules (Cairo's DST is real). A
schedule inside the window is accepted; its plan starts at the window's end
with the notice `starts_after_quiet_hours`. Changing the zone never moves a
`scheduled_at` (an instant with its offset) - it moves where the window falls
from the next batch on; changing the window, or turning it off, releases waiting
campaigns at their next sweep. Quiet hours protect the customer from
broadcasts: they do not bind AI or human replies, follow-ups, or an admin's test
to themself.

## Meta's messaging limit

Meta limits how many **unique** people a **business portfolio** may message
outside a customer service window in a moving 24 hours - 250 for a new
portfolio, then 2,000, 10,000, 100,000 and unlimited - shared by every number in
it ([Messaging limits](https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits)).
Meta documents no error for a send past it, so Wasla never relies on Meta
refusing ([ADR-146](../DECISIONS.md)).

- **Read** with `whatsapp_business_manager_messaging_limit` (Graph API v24.0 for
  that one call) at connect, credential rotation, enable, at schedule when older
  than six hours, and from `business_capability_update` webhooks. A value Wasla
  does not recognise is treated as 250 and alerted (`MessagingLimitUnknown`). A
  number whose limit was never read cannot schedule (422
  `messaging_limit_unknown`) and its running campaigns wait `limit_unknown`.
- **Counted per portfolio** in one ledger of business-initiated sends - every
  template to somebody whose window (23 hours, an hour of margin) is closed,
  from campaigns, manual templates, follow-ups and tests, reserved under the
  portfolio's advisory lock before Meta is called and deleted only when Meta
  provably did not take the message. Wasla keys the portfolio on the WABA: Meta
  names no field for a WABA's portfolio (EXTERNAL VERIFICATION).
- **Campaigns stop at 80 %** of the limit (`CAMPAIGN_PORTFOLIO_LIMIT_SHARE`),
  every source counted. The campaign waits `daily_limit` until the first person
  counted leaves the moving window (plus five minutes) and carries on by itself,
  day after day, until everybody is reached or `send_until` passes. Somebody whose
  window is open does not count. Follow-ups and manual templates are recorded and
  not refused; `PortfolioLimitReached` warns when the whole limit is used.
- **The plan** (`GET /campaigns/{id}/delivery-plan`, and with every schedule)
  replays these rules: the days, how many people each, the completion, and
  notices (`starts_after_quiet_hours`, `spreads_over_days`,
  `limit_shared_with_other_numbers`, `limit_unknown`, `contact_cap_defers`,
  `plan_exceeds_ttl` - a marketing template's messages live 30 days by default,
  `deadline_leaves_recipients`). Spreading is the decided behaviour: nothing is
  refused because of the plan.
- **What the page may show**: `GET /api/v1/whatsapp/accounts/{id}/messaging-limit`
  - tier, limit, the campaigns' budget, people used in 24 hours, what is
  available now and when more will be. Aggregates only, even when another
  workspace shares the portfolio.
- **Pacing**: a send Meta answers `held_for_quality_assessment` is sent and
  counted `held`; a held message Meta later drops (135000) pauses its campaign
  `meta_quality_review`, because the portfolio cannot send templates during the
  review.

## One marketing message per customer per day

A customer receives at most one marketing template from a workspace in a moving
24 hours, from any campaign or sent by hand ([ADR-147](../DECISIONS.md)), claimed
by one conditional update of the contact in the send's own transaction - two
campaigns on two workers cannot both win. A capped campaign recipient is
deferred a day and keeps their attempts; a manual marketing template inside the
cap is 409 `contact_frequency_cap` with the moment it becomes possible. Meta's
own per-user marketing limit (131049) has no published number; Wasla's cap is
stricter, and a 131049 also pushes the customer's slot a day out.

## Each person's own values

A body or text-header placeholder can take a constant, or a field of the
customer - their display name, or their active lead's name, interest, status or
budget, or one of the workspace's lead custom fields - with a fallback for
people without it ([ADR-148](../DECISIONS.md)). Never the phone number, never a
button's value. Values are made one line (newlines, tabs and repeated spaces
collapsed, control characters removed), fall back when empty or longer than
`CAMPAIGN_PARAMETER_MAX_CHARS` (60), and fall back together when they would make
the body longer than Meta's 1,024 characters. They are **written down with the
audience**: each recipient keeps the values resolved for them, with the contact
they were resolved for, and the send uses exactly those - renaming a contact
afterwards changes nothing this campaign sends. `POST
/campaigns/{id}/preview-messages` shows up to five chosen people's messages.

## The campaigns page

- **Search** by name (`?q=`), **duplicate** (a new draft with the composition and
  audience rules), **edit** a draft from the version read (`PATCH`, `If-Match`;
  changing the number, template, values or header file clears the written list),
  **delete** a draft nobody was sent, and an **idempotent create**
  (`Idempotency-Key`).
- **Activity**: `GET /campaigns/{id}/activity`, from the audit trail - people's
  actions and the system's (waits, completion, failure) - with field names,
  counts and reasons, never a contact.
- **Send a test to myself**: `POST /campaigns/{id}/test-send` to a contact of the
  admin's who already wrote to the number; the same message a recipient would
  get, never a recipient, ten a day per workspace.
- **Results**: `replied` (an answer on the campaign's number within 72 hours of
  their copy, credited to the customer's latest copy only), `opted_out_after`, a
  `reasons` breakdown, the **KPI summary** (`GET /campaigns/summary`), **live
  progress** for up to twenty campaigns in one request (`GET
  /campaigns/progress`, with an ETag), the **ETA** on a campaign, and a **CSV
  export** of the recipients (UTF-8 with a BOM, local times, formula cells
  neutralised, five an hour).

`docs/CAMPAIGNS_PAGE_CONTRACT.md` maps every element of the page to its route.

## What a campaign costs

Wasla does not charge for WhatsApp messages: Meta bills the business's WhatsApp
Business Account directly, per delivered template, by category and by the
recipient's country ([Pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing)).
`GET /campaigns/{id}/cost-estimate` prices each recipient at the active rate
card's marketing rate for their calling-code market, counting everybody as
delivered - an upper bound, labelled an estimate ([ADR-150](../DECISIONS.md)).
Platform staff upload Meta's rate card (`/platform/whatsapp-rate-cards`); without
an active card the estimate has no total. Free entry point windows are not
modelled.

## Stopping

`PAUSED` and `CANCELLED` are deliberately different states. Every pause or failure records `status_reason`, from a closed vocabulary (`CampaignStopReason`): `workspace_suspended`, `workspace_deleted`, `subscription_suspended`, `admin_paused`, `template_removed`, `template_not_approved`, `template_withdrawn`, `template_category`, `template_parameters_refused`, `connection_disabled`, `credential_refused`, `connection_refused`, `provider_unreachable`, `repeated_batch_failure`, `header_media_unavailable`, `header_media_refused`, `meta_quality_review`. It is the one field that says why a campaign is not sending; `last_error` keeps its sentence for a person.

**A workspace that is not served does not broadcast** ([ADR-135](../DECISIONS.md), CMP-02, CMQ-11). Suspension by the platform, deletion by the owner, and a subscription suspended for an unpaid invoice pause the workspace's scheduled and running campaigns in the same transaction, one audit entry each. The worker asks the same "served" predicate the follow-up gate asks (`app/services/workspace_serving.py`) before every batch and every recipient, so a transition it could not see still stops it at the next person. `past_due` is the grace and keeps sending. Restoring a workspace or settling the invoice resumes nothing: an admin reschedules, and the reservation is taken again then.

- **Paused** is stopped and resumable. The remaining recipients keep waiting; scheduling again resumes.
- **Cancelled** is finished. What was sent stays sent, and the rest never goes.

Somebody watching a broadcast go out and having second thoughts needs an action that is not destructive, which is what pause is for - and it stops at the next recipient, not after the claimed batch (CMP-08). `FAILED` is different again: it means the campaign could not run at all — its template was withdrawn, its number was disabled, no WhatsApp credential is configured — as distinct from one that ran and had some recipients fail, which completes.

**A failed campaign can be scheduled again**, because every condition that fails one is something a workspace fixes and then wants to carry on from. Its remaining recipients are still pending and still have not been written to, so resuming sends to them and to nobody twice. What `FAILED` keeps that `PAUSED` does not is `last_error` — the reason it stopped.

It can also be **cancelled**, which a completed or already-cancelled campaign cannot. That follows from the same fact: because failure is recoverable, a workspace that has decided against a broken campaign needs a way to close it for good rather than leaving something a later hand could reschedule.

A missing platform credential fails the campaign rather than each recipient in turn. The client refuses to be built without a token, so no attempt is made and nothing increments; treating it per-recipient would retry forever without exhausting anyone's attempts, staging a message row per recipient per sweep on a deployment that cannot send at all.

**A credential Meta *refuses* is handled the same way**, and used not to be. A `401` or Meta's own `code 190` means the number's token is expired or revoked, which is true of every recipient in the audience - so the campaign stops once with a clear reason rather than burning one attempt budget per person discovering the same dead token. A bare `403` with no code is not one of them: Meta uses it for refusals that are sometimes about one message, so it is per-message (CMP-16; `app/integrations/meta/errors.py`). The person in flight at the refusal provably received nothing, so on resume they are sent, not abandoned (CMP-11, [ADR-140](../DECISIONS.md)).

**A batch that keeps raising** is counted (`consecutive_batch_failures`) and paused `repeated_batch_failure` after `CAMPAIGN_BATCH_FAILURE_PAUSE_AFTER` in a row (CMP-20), instead of being retried every lease for ever.

**Cancelling and pausing stop the claimed batch too**, at the next recipient. The campaign is re-read from the database once per recipient (`hold`) rather than off the campaign object, because `cancel()` and `pause()` run in other transactions. What was already sent stays sent - the only honest answer once a message has reached somebody's phone. The lock `hold` takes lasts only until the next send intent commits, never across a Graph API call ([ADR-093](../DECISIONS.md)).

## Marketing opt-out

Recorded **per contact and channel** (`contact_channel_consents`, ADR-131 / ENT-19) as a timestamp and a source, not a boolean. "Since when" is the question a dispute about a marketing message actually turns on, and a colleague's note is not the same fact as a customer's own refusal.

**An opt-out belongs to the channel it was said on.** A STOP on WhatsApp stops WhatsApp marketing from every WhatsApp number of the workspace, and nothing else: the same person on Instagram or Messenger is still reachable there until they say so there. A resume is per channel too, and newer resume evidence still wins over an older stop being replayed (OMNI-030). Every writer records the channel - a stop word or tap arriving on a connection (that connection's channel), WhatsApp's `user_preferences` stop and the `131050` refusal, the `recover-button-opt-outs` replay (WhatsApp), a colleague (the channel they name) - and every reader asks about the recipient's channel: the audience builder excludes a contact opted out on the sending connection's channel, and the campaign send guard and the follow-up guard re-read it there at delivery. Migration 0097 moved every earlier, person-level opt-out to the contact's WhatsApp row, the only channel one could have been said on.

### A customer saying stop

Honoured on the inbound path, in the same transaction that stores the message. That closes a real window: between the message arriving and a worker reading it, a campaign sweep could write to somebody who has already said no, and nothing takes that message back.

The matcher is as crude as it can be, and the crudeness is the design. It fires only when the **entire** message is one of a short list of words that mean nothing else - `stop`, `unsubscribe`, `opt-out`, `unsub`, `stop please`, `please stop`, `remove me`, `الغاء`, `ايقاف`, `اوقف`, `ستوب`, `لا ترسل` and a few more (CMP-12, CMQ-12) - or one of them said more than once ("STOP STOP"), compared word for word. "stop" is a refusal; "stop by the shop tomorrow" is not, and a matcher that looked for the word anywhere would confuse the two constantly. `كفاية`, `وقف`, `بطل رسايل` and "unsubscribe me" are deliberately left out.

**Meta's later word counts too** (CMP-10, [ADR-142](../DECISIONS.md)). A marketing or campaign message Meta accepted and later reported `failed` with 131050 records the opt-out on the conversation's channel, `via=provider_status`.

The asymmetry sets the boundary. A false positive stops marketing to somebody who did not quite mean it, which a colleague undoes in a moment and the customer can always write again. A false negative keeps promoting to somebody who has asked twice, which is how a business loses its number.

Arabic is folded for keyboard variation — the hamza-bearing alefs, alef maksura, teh marbuta and the diacritics — so which spelling a customer's phone produces does not decide whether they are left alone.

### What opt-out covers, and what it does not

The rule is applied unevenly on purpose, and the line is whether the customer asked for the message:

| Path | Honours opt-out | Why |
| --- | --- | --- |
| Campaign | **Yes**, re-checked at delivery | Unsolicited, automated, and to a list |
| Follow-up | **Yes**, re-checked at dispatch | Unsolicited and automated; nobody asked for it, and it arrives *because* the conversation went quiet |
| AI reply | No | Answering a question the customer just asked |
| Manual human reply | No | A colleague replying in a live conversation |
| Manual **marketing** template | **Yes**, and admin-only | Sent by hand, it was a broadcast that ignored opt-out (CMQ-02, [ADR-141](../DECISIONS.md)); utility and authentication templates are unchanged |

**It does not silence the agent.** Someone refusing marketing is not refusing an answer, and deciding otherwise from one word would leave people talking to nobody.

The follow-up row is the one that was decided rather than inherited. A follow-up sits between a campaign and a reply — conversational in tone, but unsolicited in fact — and it is filed with the campaign, because a nudge is not a reply to anything the customer said. It is re-read at dispatch rather than trusted from scheduling, exactly as the campaign sweep re-reads it: hours pass between a nudge being scheduled and being due, and somebody who says STOP in that gap must not receive it.

A follow-up refused this way is `SKIPPED`, not failed. Consent does not come back on its own, so retrying would queue a message that can never legally go out.

**It does not read sentences.** "please take me off your list" is not recognised. That is an accepted limit rather than an oversight: the alternative is a model call on every inbound message to decide something a person can record in one click.

## Statistics

| Count | Comes from |
| --- | --- |
| `pending`, `sent`, `failed`, `skipped` | The recipient rows |
| `delivered`, `read` | The message rows a delivery webhook advances |
| `failed_after_send` | Recipients `sent` whose message Meta later reported `failed` (CMP-10) |
| `held` | Recipients `sent` whose message Meta held for quality assessment (LIM-08) |
| `replied` | Recipients who answered on the campaign's number within 72 hours, this being their latest copy (PG-10) |
| `opted_out_after` | Recipients who opted out themselves within 72 hours after their copy |
| `reasons` | Every recipient once, by status, reason code (`unrecorded` for none) and Meta's error code |

A message Meta accepted is `sent`; whether it arrived is Meta's news to bring. A read message was also delivered, so `delivered` includes it — counting the two statuses separately would undercount delivery by everyone who read it.

Every recipient that did not end `sent` carries `reason_code` (CMP-13): `opted_out`, `contact_gone`, `other_connection`, `identity_changed`, `channel_not_in_plan`, `recipient_opted_out_at_provider`, `marketing_limit_per_user`, `undeliverable`, `delivery_unconfirmed`, `attempts_exhausted`, `template_parameters_refused`, `send_window_closed`, `contact_frequency_cap`. `last_error` keeps the sentence. Rows recorded before reason codes have none.

## API

| Route | Who | Purpose |
| --- | --- | --- |
| `GET /api/v1/templates` | member | What may be sent |
| `POST /api/v1/templates/sync` | admin | Refresh the registry from Meta |
| `GET /api/v1/campaigns` | member | Newest first, keyset paged |
| `POST /api/v1/campaigns` | admin | Compose a draft (`header_media_id` for a media-header template) |
| `POST /api/v1/campaigns/header-media` | admin | Upload a template header file (image, video, PDF) |
| `GET /api/v1/campaigns/header-media/{id}` | member | A header file's metadata |
| `GET /api/v1/campaigns/header-media/{id}/file` | member | Its bytes, as an attachment |
| `POST /api/v1/campaigns/audience/preview` | admin | Count a filter without writing anything |
| `POST /api/v1/campaigns/{id}/audience` | admin | Materialise the recipient list (drafts only) |
| `POST /api/v1/campaigns/{id}/schedule` | admin | Send, now or at a time |
| `POST /api/v1/campaigns/{id}/pause` | admin | Stop, reversibly |
| `POST /api/v1/campaigns/{id}/cancel` | admin | Stop, finally |
| `GET /api/v1/campaigns/{id}/statistics` | member | Outcomes and delivery |
| `GET /api/v1/campaigns/{id}/recipients` | member | Who it reached, and who it did not - by `cursor`, filtered by `status` and `reason` |
| `GET /api/v1/campaigns?q=` | member | Find campaigns by name |
| `PATCH /api/v1/campaigns/{id}` | admin | Edit a draft (`If-Match`) |
| `DELETE /api/v1/campaigns/{id}` | admin | Delete a draft nobody was sent |
| `POST /api/v1/campaigns/{id}/duplicate` | admin | A new draft like this one |
| `GET /api/v1/campaigns/{id}/activity` | member | What happened to it |
| `POST /api/v1/campaigns/{id}/preview-messages` | admin | Up to five people's own messages |
| `GET /api/v1/campaigns/{id}/delivery-plan` | admin | When it will reach whom |
| `GET /api/v1/campaigns/{id}/cost-estimate` | admin | What it may cost at Meta's prices |
| `POST /api/v1/campaigns/{id}/test-send` | admin | Send it to my own contact |
| `GET /api/v1/campaigns/{id}/recipients/export.csv` | admin | The recipients as CSV |
| `GET /api/v1/campaigns/summary` | member | KPI cards over a window |
| `GET /api/v1/campaigns/progress?ids=` | member | Live state of up to 20 campaigns |
| `GET/POST/PATCH/DELETE /api/v1/campaign-segments` | member reads, admin writes | Saved audience rules |
| `GET /api/v1/contacts` | member | The contact picker |
| `GET/PUT /api/v1/workspace/campaign-settings` | member reads, admin writes | Quiet hours |
| `GET /api/v1/whatsapp/accounts/{id}/messaging-limit` | member | Meta's limit and how much of it is used |
| `GET /api/v1/timezones` | public | The zones a workspace may choose |
| `POST /api/v1/contacts/{id}/opt-out` | member | Record a refusal on one channel (`channel` required) |
| `DELETE /api/v1/contacts/{id}/opt-out?channel=…` | admin | Undo one recorded in error, on that channel |

Reading is ordinary inbox work. Composing, targeting and starting a campaign take an administrator, because a campaign writes to thousands of customers at once and is the least reversible thing this platform does. Recording an opt-out is any member's to do — the person handling the conversation is the one a customer says "stop" to — while clearing one takes an administrator, because undoing somebody's own refusal should be deliberate.

Lifecycle changes are named transitions rather than a `PATCH` on `status`. Status is the only field with an operational meaning, and naming the transition keeps both the audit trail and the authorization legible. `PATCH` edits a draft's composition and nothing else; a campaign's fields cannot change once it leaves `draft`.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `WORKER_KINDS` | all | Include `campaign` to run the sending loop. Several replicas are safe (ADR-134) |
| `CONNECTION_SENDS_PER_MINUTE` | unset | One number's sending allowance, shared by every sender on it ([ADR-123](../DECISIONS.md)); `channel_connections.sends_per_minute` overrides it per number (0090). Unset, a campaign is paced by its own rate alone |
| `CAMPAIGN_PROVIDER_UNREACHABLE_PAUSE_HOURS` | 6 | How long Meta may be unreachable before a waiting campaign is paused |
| `CAMPAIGN_BATCH_FAILURE_PAUSE_AFTER` | 5 | Batches in a row that may raise before a campaign is paused |
| `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS` | 2160000 (25 days) | How old Meta's id for a header file may be before it is uploaded again; Meta's expire after 30 days |
| `CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS` | 21600 (6 hours) | How old a number's messaging limit may be before scheduling reads it again |
| `META_MESSAGING_LIMIT_API_VERSION` | v24.0 | The Graph API version of the limit read alone |
| `CAMPAIGN_PORTFOLIO_LIMIT_SHARE` | 0.80 | The share of Meta's limit campaigns may use; a platform setting |
| `CAMPAIGN_CONTACT_MARKETING_INTERVAL_SECONDS` | 86400 | One marketing message per customer per this long |
| `CAMPAIGN_PARAMETER_MAX_CHARS` | 60 | The longest a person's own value may be before the fallback |
| `CAMPAIGN_TEST_SENDS_PER_DAY` | 10 | Tests per workspace in a moving day |
| `CAMPAIGN_EXPORTS_PER_HOUR` | 5 | CSV exports per workspace in a moving hour |
| `CAMPAIGN_REPLY_ATTRIBUTION_SECONDS` | 259200 (72 hours) | How long after a copy an answer counts as its reply |

There is no campaign-specific credential. Sends use the platform Meta token, as every other outbound path does ([ADR-009](../DECISIONS.md)).

## Known limits

- **Values come from the contact and their lead only.** A URL button's suffix and a copy code are the same for everybody, and a header file is one per campaign.
- **The rate is per campaign unless a number has an allowance.** Two campaigns running at once on one number can exceed either one's rate while `CONNECTION_SENDS_PER_MINUTE` is unset; with it set (or a per-number override), they share the number's allowance and wait for it without spending attempts.
- **The portfolio is keyed on the WABA.** Meta names no field for the portfolio a WABA belongs to; two WABAs of one portfolio are counted apart and could together exceed its limit (EXTERNAL VERIFICATION).
- **Follow-ups and manual templates are not refused at the limit.** They are counted and alerted at 100 %; what a follow-up should do then is an open product decision.
- **Without the `business_capability_update` webhook subscribed**, a changed limit is noticed by the six-hourly read only.
- **The cost estimate is an upper bound** that counts everybody as delivered and ignores free entry point windows; +7 numbers are priced as Russia (Meta does not document the split with Kazakhstan).
- **One header file per campaign.** IMAGE, VIDEO and DOCUMENT headers are sent with the campaign's header file, the same for everybody; a per-recipient picture is not supported. **LOCATION and GIF headers are refused** at composition (`template_unsupported`). Text headers, body (positional or named) and URL and copy-code buttons are filled.
- **That Meta honours one uploaded id across many messages for 30 days** is what its pages imply (they recommend sending by uploaded id and state the lifetime) and is EXTERNAL VERIFICATION until a real campaign shows it.
- **A `scheduled_at` still needs its UTC offset** (CMP-15): it is an instant, not a local time; quiet hours are in the workspace's zone.
- **No import, deliberately.** See [ADR-025](../DECISIONS.md). Adding one means answering the consent question, not writing a CSV parser.
- **No campaign analytics events.** Counts come from the recipient and message rows; the analytics event table arrives in Phase 12, and writing a second shape now would mean migrating two later.
- **Nothing sweeps completed campaigns.** Recipient rows accumulate for the life of the workspace - each now with the values sent to that person. They are the record of who was written to and when, so a retention policy is a product decision rather than a cleanup job (CMQ-08).
- **Live progress is polled**, not pushed: no WebSocket or Server-Sent Events.
- **Opt-out is all-or-nothing.** A contact who opts out is out of every campaign, including a utility template they might have wanted. Per-category consent needs a vocabulary a business can actually explain to a customer.
