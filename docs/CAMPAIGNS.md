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
    take: still pending and nobody else's? (one SELECT ... FOR UPDATE SKIP LOCKED,
          the row re-read) -- no --> skip this person, no send, no attempt
    opted out on this channel? -- yes --> SKIPPED opted_out, never retried
    linked to a message that may have left? -- yes --> FAILED delivery_unconfirmed
    send the template with every component it needs; record the message row
    |
write next_send_at = batch end + batch / messages_per_minute; clear lease_token
    |
nobody left (deferred people still count)? -- yes --> COMPLETED
```

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

## Stopping

`PAUSED` and `CANCELLED` are deliberately different states. Every pause or failure records `status_reason`, from a closed vocabulary (`CampaignStopReason`): `workspace_suspended`, `workspace_deleted`, `subscription_suspended`, `admin_paused`, `template_removed`, `template_not_approved`, `template_withdrawn`, `template_category`, `template_parameters_refused`, `connection_disabled`, `credential_refused`, `connection_refused`, `provider_unreachable`, `repeated_batch_failure`, `header_media_unavailable`, `header_media_refused`. It is the one field that says why a campaign is not sending; `last_error` keeps its sentence for a person.

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

A message Meta accepted is `sent`; whether it arrived is Meta's news to bring. A read message was also delivered, so `delivered` includes it — counting the two statuses separately would undercount delivery by everyone who read it.

Every recipient that did not end `sent` carries `reason_code` (CMP-13): `opted_out`, `contact_gone`, `other_connection`, `identity_changed`, `channel_not_in_plan`, `recipient_opted_out_at_provider`, `marketing_limit_per_user`, `undeliverable`, `delivery_unconfirmed`, `attempts_exhausted`, `template_parameters_refused`. `last_error` keeps the sentence. Rows recorded before reason codes have none.

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
| `POST /api/v1/contacts/{id}/opt-out` | member | Record a refusal on one channel (`channel` required) |
| `DELETE /api/v1/contacts/{id}/opt-out?channel=…` | admin | Undo one recorded in error, on that channel |

Reading is ordinary inbox work. Composing, targeting and starting a campaign take an administrator, because a campaign writes to thousands of customers at once and is the least reversible thing this platform does. Recording an opt-out is any member's to do — the person handling the conversation is the one a customer says "stop" to — while clearing one takes an administrator, because undoing somebody's own refusal should be deliberate.

Lifecycle changes are named transitions rather than a `PATCH` on `status`. Status is the only field with an operational meaning, and naming the transition keeps both the audit trail and the authorization legible.

## Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `WORKER_KINDS` | all | Include `campaign` to run the sending loop. Several replicas are safe (ADR-134) |
| `CONNECTION_SENDS_PER_MINUTE` | unset | One number's sending allowance, shared by every sender on it ([ADR-123](../DECISIONS.md)); `channel_connections.sends_per_minute` overrides it per number (0090). Unset, a campaign is paced by its own rate alone |
| `CAMPAIGN_PROVIDER_UNREACHABLE_PAUSE_HOURS` | 6 | How long Meta may be unreachable before a waiting campaign is paused |
| `CAMPAIGN_BATCH_FAILURE_PAUSE_AFTER` | 5 | Batches in a row that may raise before a campaign is paused |
| `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS` | 2160000 (25 days) | How old Meta's id for a header file may be before it is uploaded again; Meta's expire after 30 days |

There is no campaign-specific credential. Sends use the platform Meta token, as every other outbound path does ([ADR-009](../DECISIONS.md)).

## Known limits

- **No per-recipient personalisation.** A campaign's template variables are one list for the whole send. Filling `{{1}}` with each customer's name needs a source of per-recipient facts that nothing here has yet.
- **The rate is per campaign unless a number has an allowance.** Two campaigns running at once on one number can exceed either one's rate while `CONNECTION_SENDS_PER_MINUTE` is unset; with it set (or a per-number override), they share the number's allowance and wait for it without spending attempts.
- **Meta's portfolio messaging limit is not read or enforced** (CMP-17, EXT-CMP-01). Meta counts unique users messaged outside a customer-service window in a moving 24 hours, shared by every number of the business portfolio, with tiers of 250 (new), 2,000, 10,000, 100,000 and unlimited. A 50,000-person campaign on a 250-tier portfolio will be refused beyond the tier, with an error Meta does not document; keep first campaigns under the tier. Reading the limit and warning at scheduling is CMQ-06.
- **One header file per campaign.** IMAGE, VIDEO and DOCUMENT headers are sent with the campaign's header file, the same for everybody; a per-recipient picture is not supported. **LOCATION and GIF headers are refused** at composition (`template_unsupported`). Text headers, body (positional or named) and URL and copy-code buttons are filled.
- **That Meta honours one uploaded id across many messages for 30 days** is what its pages imply (they recommend sending by uploaded id and state the lifetime) and is EXTERNAL VERIFICATION until a real campaign shows it.
- **Workspace time zone.** A `scheduled_at` must carry its UTC offset (CMP-15); there are no quiet hours (CMQ-04).
- **No import, deliberately.** See [ADR-025](../DECISIONS.md). Adding one means answering the consent question, not writing a CSV parser.
- **No campaign analytics events.** Counts come from the recipient and message rows; the analytics event table arrives in Phase 12, and writing a second shape now would mean migrating two later.
- **Nothing sweeps completed campaigns.** Recipient rows accumulate for the life of the workspace. They are the record of who was written to and when, so a retention policy is a product decision rather than a cleanup job.
- **Opt-out is all-or-nothing.** A contact who opts out is out of every campaign, including a utility template they might have wanted. Per-category consent needs a vocabulary a business can actually explain to a customer.
