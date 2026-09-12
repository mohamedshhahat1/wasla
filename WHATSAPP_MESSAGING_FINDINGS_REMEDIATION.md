# Wasla — WhatsApp / Messaging Core Findings Remediation

**Remediation of `WHATSAPP_MESSAGING_CORE_AUDIT.md`.**
Date: 2026-09-11 · Scope: MSG-01 … MSG-25 · Method: revalidate → fix → prove → mutate → document.

---

## 1. Executive Summary

The audit's central finding was structural rather than incidental: **23 of 23 mutations were killed**, so every guarantee the messaging core claimed was already pinned by a test that fails when the guarantee is removed. It followed that the defects were not weak guards but *absent* ones — the class mutation testing cannot discover by construction.

That shaped this work. Each finding needed a guard written from nothing, and then a test written from nothing to hold it down, and then a deliberate mutation to prove the test was not decoration. Twelve mutations, twelve killed.

**The three production blockers are closed.**

**MSG-01** — a phone number changing hands leaked the previous workspace's inbound traffic to the new one for as long as Meta retries, which is seven days. `whatsapp_accounts` now carries a tenure interval and routing asks which interval contains the event. The property being protected turned out not to be chronology but cross-workspace disclosure, and that distinction decides the case a pure interval test gets wrong: a provider timestamp can legitimately precede the claim it belongs to, so an event outside every tenure is still delivered when one workspace has always held the number — and dropped the moment a second appears in its history. Proven end to end against a real handover with a gap in it.

**MSG-02** — a message could be acknowledged, stored, and never processed, with no signal, no query and no remedy, while the runbook promised one. `whatsapp_events.state` now means what its name says: `PROCESSED` is "projected *and* every handoff it needed was accepted", not "a row exists". `InboundRecoveryWorker` claims what is still owing and re-derives what is missing rather than replaying the webhook. Proven against a real Redis outage: stored, owing, recovered once, and still once after a second sweep.

**MSG-05 and MSG-06** — a follow-up fired into a conversation a colleague had taken over, and at a customer who had said STOP. Both are now re-read at dispatch, with handover additionally cancelling pending nudges at the moment it happens. The opt-out question was a genuine product decision and it is now written down rather than implied.

Beyond the blockers, the change that matters most to a customer is **MSG-07**: a `2xx` from Meta carrying no message id was recorded as a definite failure, which is the one classification that licenses a new send — so a campaign or follow-up acting on it put the message on somebody's phone twice. It is now an unknown, which is terminal.

**Three things are worth stating plainly about what this report does not claim.**

A re-verification pass after the final commits found that two of them had broken eighty-three tests, including two of this report's own cited proofs. Making `origin` a required argument was right; running `mypy app` when CI runs `mypy app tests` was not, and it is why the break went unseen by every gate. It is fixed in `aa71e80`, test-side only, and §22 has the numbers rather than quietly absorbing them.


No verification against a real Meta account was possible; the provider contract was re-checked against Meta's own documentation today, and §26 lists what remains external.

And **the suite is not green at the audited HEAD.** A clean worktree of `a486321` fails 165 tests, all of them in eight billing files, from a test-isolation defect in which several billing modules share a platform-wide plan catalogue and overwrite each other's limits. The tip fails the same 165 and nothing else. They are unrelated to messaging, they are not touched by this work, and they are reported here rather than quietly absorbed. §22 has the numbers and §28 the recommendation.

**Verdict: MESSAGING FINDINGS CLOSED WITH EXTERNAL PROVIDER / DEPLOYMENT VERIFICATION.**

---

## 2. Repository State

| Property | Value |
| --- | --- |
| Branch | `worktree-billing-google-auth` (unchanged) |
| Initial HEAD | `a486321d92309f14bd9736601b0d606ae92c18f9` — **identical to the audited revision** |
| Final HEAD | see §23 commit list; `aa71e80` after the re-verification pass, plus this report |
| Initial tree | clean except three untracked audit reports |
| Final tree | the same three, plus this report |
| Stashes | none at start; none at end |
| Migration head | `0053` → **`0056`** |
| Python | 3.12.7 |
| PostgreSQL | 16.15 (`wasla-postgres-1`, healthy) |
| Redis | 7.4.11 (`wasla-redis-1`, healthy) |
| Docker | both containers up |

**Stale-install check**, because a previous audit was misled by one:

```
>>> import app; print(app.__file__)
E:\wasla\app\__init__.py
```

The imported package is the checkout being remediated. `tests/conftest.py` asserts this at collection time and still does.

HEAD had not advanced since the audit, so no intervening commits needed revalidating. Every finding was nonetheless re-read against the current code before being acted on, and two were found to have been described slightly more narrowly than the code warranted — see MSG-09 and MSG-19 in §3.

**Infrastructure used.** Seven isolated databases, each built by `alembic upgrade head` rather than `create_all`, so every runtime result is against the schema a deployment actually gets: `wasla_msgfix`, `wasla_conc`, `wasla_mut`, `wasla_bf`, `wasla_mig55`/`wasla_mig56`, `wasla_probe`, `wasla_final`. Redis logical databases 11–14, flushed between probes. Real `uvicorn` on real sockets for every webhook probe, and real HTTP servers standing in for `graph.facebook.com` that count connections — so "one provider call" means one socket accepted.

**A note on discipline, learned from the audit's own appendix.** The audit was misled once by running a probe against the database a pytest session was using. That happened again here: an intermediate full-suite run showed widespread failures because an end-to-end probe was driving uvicorn and Redis on the same machine at the same time. It was discarded rather than reported, the run was repeated with nothing else running, and the baseline comparison in §22 was taken from a **separate git worktree** at `a486321` against its own database for exactly this reason.

---

## 3. Findings Revalidation

| ID | Still applicable? | Priority | Fix | Proof | Final status |
| --- | --- | --- | --- | --- | --- |
| MSG-01 | Yes | P0 | Ownership tenure on the account; `owner_at` resolves by event timestamp; never falls back to the current owner | `test_whatsapp_number_handover.py` (6), probe §4, M-A | **CLOSED** |
| MSG-02 | Yes | P0 | `state` made real; `InboundRecoveryWorker`; gauge; `queues unprocessed-inbound` | `test_whatsapp_inbound_recovery.py` (12), probe §5, M-B, M-C | **CLOSED** |
| MSG-03 | Yes | P2 | NUL stripped and counted before storage; `DataError` answered 200 and counted | `test_whatsapp_lifecycle_and_backoff.py` (12), probe | **CLOSED** |
| MSG-04 | Yes | P1 | Status resolved by provider id across a number's holders | `test_whatsapp_number_handover.py`, probe | **CLOSED** |
| MSG-05 | Yes | P0 | Cancel on handover **and** re-check at dispatch | `test_follow_up_revalidation.py` (6), M-D | **CLOSED** |
| MSG-06 | Yes | P1 | Opt-out re-read at dispatch; decision documented | `test_follow_up_revalidation.py`, M-E | **CLOSED** |
| MSG-07 | Yes | P1 | `2xx` with no usable id → `UncertainDeliveryError` | `test_whatsapp_provider_outcomes.py` (14), M-F | **CLOSED** |
| MSG-08 | Yes | P1 | `httpx.TransportError` → `UncertainDeliveryError` | `test_whatsapp_provider_outcomes.py`, M-G | **CLOSED** |
| MSG-09 | Yes, **and wider than described** | P1 | `ON CONFLICT` on events; savepoint-and-re-read on contact and conversation | `test_whatsapp_inbound_concurrency.py` (6), probe, M-H | **CLOSED** |
| MSG-10 | Yes | P1 | Registry guard moved into `MessagingService.send_template` | `test_whatsapp_send_idempotency.py`, M-I | **CLOSED** |
| MSG-11 | Yes | P1 | Two gauges, an alert, `queues unresolved-sends`, runbook procedure | promtool, CLI, §12 | **CLOSED** |
| MSG-12 | Yes | P0 | Eight messaging alert rules, each tested from both sides | promtool + threshold mutation | **CLOSED** |
| MSG-13 | Yes | P2 | Subsumed by MSG-02 | as MSG-02 | **CLOSED** |
| MSG-14 | Yes | P2 | `failed` ranked between `sent` and `delivered` | `test_conversation_projection.py`, sweep | **CLOSED** |
| MSG-15 | Yes | P1 | Durable `Idempotency-Key`, workspace-scoped | `test_whatsapp_send_idempotency.py` (14), M-J | **CLOSED** |
| MSG-16 | Yes | P2 → done | `messages.origin`, backfilled exactly | backfill probe, sweep, tests | **CLOSED** |
| MSG-17 | Yes | P2 | `Retry-After` honoured and clamped; additive jitter | `test_whatsapp_lifecycle_and_backoff.py` | **CLOSED** |
| MSG-18 | Yes | P1 | `ProviderAuthError`; campaign stops once; follow-up sweep skips the workspace | `test_whatsapp_provider_outcomes.py` | **CLOSED** |
| MSG-19 | Yes, **narrower than described** | P2 | No agent turn for `UNSUPPORTED` | `test_whatsapp_inbound_recovery.py`, M-L | **CLOSED** |
| MSG-20 | Yes | P2 | Documentation corrected; capability not built | `test_documentation_truth.py` | **CLOSED (documented)** |
| MSG-21 | Yes | P2 | Sunset table + start-up warning at 90 days | `test_whatsapp_lifecycle_and_backoff.py` | **CLOSED** |
| MSG-22 | Yes | P2 | Status re-read per recipient inside a claimed batch | code + docs | **CLOSED** |
| MSG-23 | Yes | P1 | Real concurrency probes promoted into CI | 6 tests in `tests/integration` | **CLOSED** |
| MSG-24 | Yes | P2 | Provider rejection written back to the registry | `test_whatsapp_send_idempotency.py` (3) | **CLOSED (partially deferred)** |
| MSG-25 | Yes | P1 | Length check at the choke point; generation guidance | `test_whatsapp_provider_outcomes.py`, M-K | **CLOSED** |

**Two revalidation corrections.**

**MSG-09 was wider than the audit described.** The audit named `WhatsAppEventRepository.record` and `ConversationRepository.get_or_create`. Fixing the first and writing the concurrency test immediately exposed a third: `ContactRepository.upsert` had the same read-then-insert shape, and eight concurrent deliveries of one customer's first message still produced `IntegrityError`. The test found it before a deployment did, which is the argument for MSG-23 in one sentence.

**MSG-19 is narrower than "reactions and other unsupported types trigger a billed AI turn".** They trigger an agent *job*; whether that job becomes a billed inference depends on the orchestrator, which may hand off or find nothing to answer. The cost is real and the fix is the same, but the finding overstates the guaranteed spend.

---

## 4. MSG-01 — Historical Ownership

### What the resolver now answers

Not "who holds this number" but "who held it when this event happened".

`whatsapp_accounts.ownership_started_at` is set at claim time and, with the existing `released_at`, gives a half-open tenure `[ownership_started_at, released_at)`. Half-open because a release and the next claim can share a timestamp to the microsecond when a number moves quickly, and a closed interval would put that instant in two workspaces at once — the one answer this must never give.

A column of its own rather than `created_at`. The two coincide today because `connect` is this table's only writer and sets both in one statement — but that is a fact about one call site, and attributing a customer's message to a business should not rest on a generic audit timestamp continuing to mean something specific. Migration 0056 backfills it from `created_at`, which is accurate for every row the schema can contain.

### The rule, and the case it gets right that a naive one does not

| Event timestamp | Resolved to |
| --- | --- |
| Inside a claim's tenure | That workspace |
| Outside every tenure, one workspace has ever held the number | That workspace |
| Outside every tenure, more than one has | **Nobody**; dropped and counted `unowned` |
| Inside two tenures at once | **Nobody**; logged as a data-integrity error |

The second row was not in the original plan and was forced by the evidence. The first implementation compared timestamps strictly, and the existing suite went red: fixtures carry provider timestamps weeks older than the account rows they run against. That is not a test artefact — clocks drift, and Meta can hold a message sent moments before a claim committed. Refusing those would drop real customer messages on numbers that have never been handed over, which is most of them.

The correct framing is that **the property being protected is cross-workspace disclosure, not chronology.** Where one workspace has always held a number there is no other workspace the message could belong to; the moment a second appears in its history, that reasoning is gone and the event is dropped rather than guessed at.

### Runtime results

Real handover: A claims at −30d, releases at −10d, B claims at −5d, leaving a five-day gap.

```
late inbound (during A's tenure, arriving now)   http=200  owner=A
gap inbound  (after A released, before B)        http=200  owner=None   (dropped, counted)
current inbound (during B's tenure)              http=200  owner=B
messages visible to B from either of A's         0
```

Plus, from `tests/integration/test_whatsapp_number_handover.py`: an event before any claim is dropped, and a slightly-early message on a number only one workspace has ever held is still delivered — the negative control that stops "never route to the new owner" from being satisfied by dropping everything.

**Mutation M-A** replaces the resolver with a current-owner lookup. **KILLED.**

---

## 5. MSG-02 — Durable Inbound Recovery

### What `PROCESSED` means now

| State | Meaning |
| --- | --- |
| `received` | Stored, and something it needed has not happened. |
| `processed` | Projected, **and** every handoff it needed was accepted by a queue. |
| `failed` | Permanently unprocessable. An operator's problem, not a sweeper's. |

The second row is the whole fix. A state that could not distinguish a message nobody was asked to answer from one that was answered would be decoration, and that is exactly what the column was: written once at insert, never advanced, `PROCESSED` and `FAILED` unreachable from any code path.

### Recovery re-derives; it does not replay

Replaying a stored webhook would re-project the message, re-cancel its follow-ups and re-meter the delivery. Each claimed event is instead asked what it is still missing — is the message there, does its file still need reading, does its conversation still need a turn — and only that is supplied. Which is why running it twice produces one turn.

**A redelivery is deliberately not a recovery path.** Meta's retry of an already-stored event stops at the duplicate check whatever state that event is in. The tempting alternative — let a redelivery re-enqueue what the first delivery could not — would be two mechanisms racing to queue one turn, and the loser is whichever the sweeper also picks up.

### Runtime results

Redis pointed at a closed port, then restored:

```
redis down:  http=200  messages=1  state=received  error=agent_enqueue_failed  queue=0
redelivery:  http=200  queue=0                         (a duplicate, not a recovery)
sweep 1:     agent_jobs=1  queue=1  state=processed
sweep 2:     claimed=0     queue=1                     (still exactly one)
```

Two sweepers against one event: `claimed` is `[0, 1]` and one agent job between them. Separately, and more strongly, a claim held open across a second sweeper's read returns nothing to the second — which is the lock itself rather than the loop's timing.

**Mutations.** M-B marks the event processed despite a refused enqueue — **KILLED**. M-C removes `FOR UPDATE SKIP LOCKED` — **KILLED**.

M-C is worth recording as a methodological result. It *survived* the first time: the two-sweeper test drove two `run_once` calls through `asyncio.gather`, and those can finish one after the other without ever overlapping, so the test passed with the lock removed. A test of a lock that never contends is not a test of a lock. The explicit held-open-transaction test was written to fix that, and it kills the mutation.

### Operator surface

```
python -m app.workers.queues unprocessed-inbound
```

Age, kind, workspace, reason, provider event id. No message bodies: the workspace and conversation are enough to find the conversation in the product, and customer text does not belong in a shell history. The command builds no Redis client, so the backlog can be inspected while Redis is the thing that is down.

`wasla_unprocessed_inbound_events` and `wasla_unprocessed_inbound_oldest_age_seconds` share the sweeper's own cutoff, imported rather than restated, so an operator alerting on a backlog and a sweeper draining one are looking at the same set.

---

## 6. Event State Machine

```
                 stored
                   │
      ┌────────────┴────────────┐
      │                         │
 handoff accepted        handoff refused
      │                         │
  PROCESSED                 RECEIVED ──────► claimed by sweeper
  processed_at set          error = token         │
                                       ┌──────────┴──────────┐
                                  work supplied        no pass will help
                                       │                     │
                                  PROCESSED               FAILED
```

Events that owe nothing are `PROCESSED` on the spot — a delivery status (including one naming a message this deployment never sent, which is ordinary traffic), a message type Wasla cannot read, and a message on a number the workspace has released. An event the sweeper picks up on every pass is a backlog gauge that never reaches zero.

Reasons are bounded machine-readable tokens (`agent_enqueue_failed`, `media_enqueue_failed`, `projection_missing`), never a payload fragment or a provider message: the column is printed by an operator command and shipped in logs.

Media is `PROCESSED` once the `MediaJob` is accepted, not when the file is eventually read. The media worker owns what happens after that, and holding the event open across a separate workflow would make the gauge report the media pipeline's depth instead of the webhook's debt.

---

## 7. Follow-Up Revalidation

| Condition | Provider calls | Outcome |
| --- | --- | --- |
| Ordinary due follow-up | 1 | `SENT` |
| Conversation in `HUMAN` mode | **0** | `SKIPPED` |
| Contact opted out | **0** | `SKIPPED` |
| Handover before the sweep | **0** | `CANCELLED` at handover |
| Takeover after the row became due, driven through the real worker | **0** | `SKIPPED` |

The assertion that matters is `provider_calls == 0`, not the recorded status: a guard that set the status correctly *after* asking Meta to deliver would satisfy the second and fail the customer.

**Two layers, because one cannot cover the race.** `InboxService.set_mode` cancels pending nudges at handover, so they stop existing rather than waiting to be declined and the colleague can see they will not arrive. The claim commits, though, so no lock survives it — a handover landing between the sweep claiming a row and the send leaving is caught only by the dispatch-time re-read.

Both are skips, not failures. Neither a human-owned conversation nor a withdrawn consent resolves itself, so retrying would queue a message that can never legally go out.

Handing a conversation *back* to the AI cancels nothing, which is pinned — `set_mode` cancelling unconditionally would quietly delete a nudge every time somebody resumed the AI.

**Mutations M-D and M-E** remove each check. Both **KILLED**.

---

## 8. Provider Outcome Taxonomy

Every row measured against a real socket instructed to misbehave.

| Provider behaviour | Calls | Raises | Row | Safe to send again? |
| --- | --- | --- | --- | --- |
| `200` + message id | 1 | — | `sent` / `sent` | n/a |
| `200`, no `messages` array | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | **No** |
| `200`, empty `messages` | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | **No** |
| `200`, message with no id | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | **No** |
| `200`, unreadable body | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | **No** |
| `5xx` | 1 | `UncertainDeliveryError` | `pending` / `requested` | **No** |
| Connection reset mid-response | 1 | `UncertainDeliveryError` | `pending` / `requested` | **No** |
| Read timeout | 1 | `UncertainDeliveryError` | `pending` / `requested` | **No** |
| `400` (outside window) | 1 | `SendNotAttemptedError` | `failed` / `undelivered` | Yes |
| `401` | 1 | `ProviderAuthError` | `failed` / `undelivered`, **raised** | Yes, but the credential is dead |
| `403` + code 190 | 1 | `ProviderAuthError` | as above | as above |
| `400` + code 190 | 1 | `ProviderAuthError` | as above | as above |
| `403`, no code | 1 | `SendNotAttemptedError` | `failed` / `undelivered` | Yes |
| Meta template codes | 1 | `TemplateWithdrawnError` | `failed` / `undelivered`, registry marked | Yes |
| `429` | 3 | `RateLimitedError` | `failed` / `undelivered` | Yes |

**MSG-07.** The four `200` rows used to be `failed` / `undelivered`. A `2xx` is Meta saying it accepted the message; that it then failed to name it is a fact about the response, not about the delivery. The failure reason even read *"accepted"* while the state read *undelivered* — the code already knew. Campaigns and follow-ups now abandon these rather than retry, so the one path in the outbound design that could duplicate a customer-visible message is closed.

**MSG-08.** A reset connection escaped as a raw `httpx.RemoteProtocolError` past `_attempt`, which catches only this package's own types. `httpx.TransportError` is caught **after** `ConnectError`, which provably never reached Meta and stays the retryable case.

**MSG-18.** `ProviderAuthError` subclasses `SendNotAttemptedError`, because the truth about *this message* is that nothing was delivered — and is its own type because the truth about the *workspace* is that every recipient will fail identically. Neither the status nor the code is trusted alone: a `401` is unambiguous, a bare `403` is not (Meta uses it for number-level permission problems), and `code 190` is authoritative and arrives on a `400` too. The bare-`403` case is pinned so a stricter reading cannot creep in and stop a campaign that should have failed one recipient.

**Mutations M-F** (malformed `2xx` → undelivered) and **M-G** (transport failure escapes). Both **KILLED**.

---

## 9. Concurrent Webhook Behaviour

Real uvicorn, one connection per delivery, `asyncio.Barrier`.

| Scenario | HTTP | Rows | Jobs |
| --- | --- | --- | --- |
| Same message ×2 | 2 × 200 | 1 event, 1 message, 1 contact, 1 conversation, 0 orphans | 1 |
| Same message ×4 | 4 × 200 | same | 1 |
| Same message ×8 | 8 × 200 | same | 1 |
| Distinct first messages ×2 | 2 × 200 | 1 contact, 1 conversation, 2 messages, 0 orphans | 2 |
| Distinct first messages ×4 | 4 × 200 | 1 contact, 1 conversation, 4 messages, 0 orphans | 4 |
| Same status ×8 | 8 × 200 | 1 event, 0 messages, 0 conversations | 0 |

The audit measured `{500: 3, 200: 5}` at eight-way. It is now `200` eight times. The data invariants were never in doubt; the protocol was, and a `500` rate proportional to burst concurrency on an endpoint whose failure rate Meta watches is not a cosmetic problem — it also lengthens the window in which a late redelivery can arrive.

**One job per delivery, not one per conversation, is the contract** and it is pinned deliberately in both directions. Job deduplication is *within* a delivery: two messages in one webhook produce one turn, because the worker reads the conversation fresh. Two separate deliveries are two separate things the customer said, and collapsing them across requests would need a lock held across the whole ingestion path. A change making the distinct-message count `1` would be suppressing real messages; a change making the duplicate count anything but `1` would be answering one message twice.

**Mutation M-H** reverts the event insert to raising on the unique race. **KILLED** — the eight-way test's all-200 assertion fails.

---

## 10. Manual Send Idempotency

`Idempotency-Key` on `POST /conversations/{id}/messages`, `…/messages/template` and `…/messages/media`.

| Case | Provider calls | Messages | Result |
| --- | --- | --- | --- |
| Same key, sequential | 1 | 1 | Original returned, including its `wa_message_id` |
| Same key, **concurrent, two connections** | 1 | 1 | Both requests get the same message |
| No key, identical body ×2 | 2 | 2 | Two messages — intentional |
| Different keys, identical body | 2 | 2 | Two messages |
| Same key, different body | 1 | 1 | `409` |
| Same key, two workspaces | 2 | 1 each | No collision |

**Explicit, never inferred.** Sending the same words twice is something people legitimately do — "are you there?" twice is two messages — so a window suppressing a duplicate body would silently swallow real intent, and a swallowed message is worse than a visible duplicate because nobody can see it happen. `test_two_sends_of_the_same_words_without_a_key_are_two_messages` is where that decision is written down, and it is as important as the deduplication tests.

Scoped `UNIQUE(tenant_id, idempotency_key)`, mirroring `payments`: keys are client-generated, so a global constraint would let one workspace's chosen key suppress another's message.

The concurrent case uses **two transactions on two connections**, because a sequential test passes with no constraint at all. The losing insert is wrapped in a savepoint — without it the failure poisons the surrounding transaction and the request cannot produce a response at all.

An attachment send short-circuits before doing any work, because it has work either side of the dispatch: the file is re-read and the storage allowance re-charged on the way in, and the attachment recorded on the way out.

**Mutation M-J** removes the atomic claim. **KILLED.**

---

## 11. Template Guard

`MessagingService.send_template` is now the choke point, so every caller inherits the registry check.

| Registry status | Manual send | Provider calls |
| --- | --- | --- |
| `PAUSED` | `422` | 0 |
| `REJECTED` | `422` | 0 |
| `DISABLED` | `422` | 0 |
| `APPROVED` | sent | 1 |
| Not in the registry | sent | 1 |

The last row is the asymmetry `refusal_reason_for` argues for, pinned so it is not "tidied up" into a stricter rule: a workspace that has not synced cannot be told apart from one whose template does not exist, and refusing both would lose every template-bearing message the first workspace has. Campaigns keep their stricter rule on top.

**MSG-24, the freshness half.** A provider rejection is written back: Meta's template error codes mark the local row `PAUSED` with the code recorded, so the next send is refused locally without asking. Three narrownesses, each tested — only codes that unambiguously mean *this template may not be sent* are acted on; it is recorded as `PAUSED` whatever the code, because pausing is the reversible state; and a template the registry has never heard of does not get a row invented for it.

Periodic background sync is **not** implemented, and that is a deliberate deferral (§28).

**Mutation M-I** removes the guard. **KILLED.**

---

## 12. Unresolved Outbound Operations

`docs/WHATSAPP.md` said a `requested` row *"is shown to a person rather than resolved by a sweep"* and named `ix_messages_unresolved_delivery` as the query. The index existed and was correctly partial. Nothing read it.

Now:

- `wasla_unresolved_outbound_messages` — how many.
- `wasla_oldest_unresolved_outbound_age_seconds` — whether the oldest is a send in flight or one that broke an hour ago.
- `UnresolvedOutboundSends` — alerts on the **age**, because a handful of rows seconds old is every send currently in flight.
- `python -m app.workers.queues unresolved-sends` — id, workspace, conversation, age, provider id.
- `docs/RUNBOOK.md` — *A send WhatsApp never confirmed*, with the investigation procedure and an explicit refusal to resend.

**None of it resends anything, and none of it lets an operator clear a row by hand.** That is the point: `requested` means Meta may already have delivered the message, there is no lookup to ask with, and a retry is the one action that cannot be taken back. The runbook says to read the conversation, check Meta's own reporting, ask the workspace — and then, if it genuinely did not arrive, send a *new* message through the product.

The command builds no Redis client and the queue commands build no database pool, so either half can be inspected while the other is down.

---

## 13. Messaging Alerts

`deploy/monitoring/alerts.yml` held twelve rules covering lifecycle, auth, billing and the *email* webhook, and not one concerned WhatsApp — although every metric needed already existed and was already scraped.

| Alert | Fires when | Severity |
| --- | --- | --- |
| `WhatsAppInboundStopped` | No inbound for 30m, on a deployment that had traffic today | critical |
| `WhatsAppWebhookSignatureFailures` | Sustained signature refusals | critical |
| `WhatsAppSendFailureRate` | >20% of sends failing for 15m | warning |
| `WhatsAppRateLimited` | Sustained 429s | warning |
| `UnprocessedInboundBacklog` | Stored inbound owing work for 15m | critical |
| `UnresolvedOutboundSends` | A send unconfirmed for over an hour | warning |
| `QueueJobsStuck` | Oldest unclaimed job over 15m | warning |
| `DeadLetterGrowth` | Jobs being dead-lettered | warning |

`WhatsAppInboundStopped` is the one with no other symptom: if Meta disables the subscription everything looks healthy from inside — no errors, no queue depth, no failed jobs, just an inbox that stops filling. It is guarded on the deployment having had traffic in the last 24 hours, which is the honest version of "business hours" for a product that does not know its customers' hours.

WhatsApp signature failures are now **counted** as well as logged, on the same counter and with the same shape as the email webhook's.

**Every rule is tested twice — once under its threshold and once over it.** A rule only ever shown firing has not been shown to discriminate. `promtool test rules` → `SUCCESS`; `promtool check rules` → 20 rules found; `amtool check-config` → valid.

**Non-vacuity proved by mutation.** Raising `UnprocessedInboundBacklog` to `> 100000` and `UnresolvedOutboundSends` to `> 999999` made exactly the two firing cases fail and left the two quiet cases passing. Both thresholds restored byte-exactly; `git diff` shows insertions only.

---

## 14. Agent Text Length

| Reply length | Provider calls | Result |
| --- | --- | --- |
| 4,096 (exactly the limit) | 1 | `sent` |
| 4,097 | **0** | `ValidationError`, nothing staged |

The check lives in `MessagingService.send_text`, which every producer goes through — the API schema was never the guarantee, because the agent path does not go through a request schema. `MAX_TEXT_LENGTH` in the schema now imports the service's constant rather than restating it, so the two cannot drift.

**Refused, not truncated or split**, per the locked product decision. Truncating puts words in a business's mouth and cuts them off mid-sentence; splitting reintroduces chunk ordering, partial failure and duplicate chunks that this system does not currently have to reason about.

Refused *before* anything is staged, so it costs no row and no provider call, and the failure is a recorded, alertable job failure rather than a silent message the customer never receives.

**Generation guidance**, not a generation guarantee. Agents are told the limit in their instructions, appended to whatever the workspace wrote — a workspace cannot be expected to know Meta's limit, and one that deleted the sentence would get the failure back. It reduces how often the refusal is reached. It cannot be more than that: tokens are not characters, and no token budget bounds a character count across languages, which is why the service check is authoritative.

The boundary is pinned from **both** sides. Meta's limit is inclusive, so refusing at exactly 4,096 would be inventing a stricter rule than the provider's and silently losing the longest legitimate replies.

**Mutation M-K** removes the guard. **KILLED** — the over-limit test observes a provider call.

---

## 15. Unsupported / Interactive Types

**MSG-19.** `reaction`, `order`, `system` and `contacts` are stored as `UNSUPPORTED` messages, acknowledged, and **queue no agent job**. The event is `PROCESSED` immediately, so the sweeper does not pick it up on every pass and eventually queue the turn anyway.

Still stored, and deliberately: the raw event is kept whole so a type Meta ships tomorrow can be understood later, and the message row keeps the conversation's history honest. It is the *turn* that is refused, not the record. An ordinary text message still gets its turn — the negative control, without which "never enqueue" would satisfy the whole set and break the product.

**Mutation M-L** gives unsupported messages an agent job. **KILLED** — and it is worth noting that it *survived* the first run, because no test covered MSG-19 until one was written. A finding with no test is a finding no mutation can reach.

**MSG-20 is documented rather than built.** `send_buttons`, `send_list` and `send_location` exist on the client and are called by nothing, so interactive messaging is not a product capability. `docs/WHATSAPP.md` now separates client support from product support and carries a *Not supported* section; `test_documentation_truth.py` holds down the sentence that invited the wrong inference. Inbound interactive replies still keep their reply id only in the raw event — building the feature means carrying `interactive.*_reply.id`, never the title, into a column of its own.

---

## 16. Message Origin

Implemented rather than deferred. The audit's own reasoning is the argument: attribution is wrong *today*, for auditability and analytics, independently of coexistence.

`messages.origin` ∈ `customer`, `human`, `agent`, `campaign`, `follow_up`, `system`. Total over every row, so one column answers the question for any message rather than only where a caller remembered to set it.

**No default, set explicitly at every creation site.** A default is what an unlabelled send would silently inherit, and inheriting the wrong attribution is the defect being removed. Making it a required keyword meant mypy enumerated all seven call sites rather than leaving them to be found by hand.

**The backfill is exact, and was verified on real rows** — schema built at 0055, one message per producer inserted as the application leaves them, upgraded, every label checked:

```
ok  expected=human      actual=human
ok  expected=agent      actual=agent
ok  expected=campaign   actual=campaign
ok  expected=follow_up  actual=follow_up
ok  expected=customer   actual=customer
ok  rows left null: 0
```

The two joins run **first**, precisely because they are the two cases the naive inference got wrong, so anything they claim is claimed on evidence rather than on the absence of it. `sent_by_id` then separates the two that remain, which it was always able to do once campaigns and follow-ups were out of the way.

The enum is open at the end on purpose: a `business_app` member slots in beside these when coexistence happens. Having the column at all is the part worth doing now — adding a label later is an `ALTER TYPE`, and adding a column to a large `messages` table is not.

---

## 17. Phone Number Handover Matrix

A owns `PN-MOVES` from −30d to −10d; B claims it at −5d.

| Event | Timestamp | Routed to | Visible to B? |
| --- | --- | --- | --- |
| Inbound | before A claimed | nobody (dropped, counted) | no |
| Inbound | during A's tenure | **A** | **no** |
| Inbound | in the gap | nobody (dropped, counted) | no |
| Inbound | during B's tenure | **B** | yes |
| Status for A's outbound | during B's tenure | **A's message**, by provider id | no |
| Inbound, slightly early, number never moved | before the only claim | that workspace | n/a |

Outbound remains bound to a live claim: `_dispatch` loads the account through the tenant-scoped repository and refuses one that is not active, so queued work for a released account cannot send through the number's new owner. That guard was already correct and was not weakened.

A late inbound routed to A is **recorded, not answered** — the workspace cannot send through a claim it no longer holds, so an agent turn could only end in a refusal after paying for an inference.

---

## 18. Crash / Recovery Matrix

| Crash point | Provider retries | Recovered by | Duplicates | Converges |
| --- | --- | --- | --- | --- |
| After parse, before DB | yes (`500`) | Meta's retry | no | yes |
| After DB, before enqueue | no (`200`) | **`InboundRecoveryWorker`** | no | **yes** (was: never) |
| After enqueue, before commit | yes | `FIRST_ATTEMPT_TRANSIENT` | no | yes |
| Outbound intent, before provider | n/a | job requeued from `reserved` | no | yes |
| Provider engaged, before local update | n/a | quarantined; a person reads the conversation | **no** | partially |
| Local update, before job ack | n/a | quarantined | no | yes |
| Status webhook, before ack | yes | event dedup | no | yes |
| PostgreSQL unavailable | yes (`500`) | Meta's retry | no | yes |
| Redis unavailable | no (`200`) | **`InboundRecoveryWorker`** | no | **yes** (was: never) |
| Payload PostgreSQL rejects | no (`200`) | **sanitised, or acknowledged and counted** | no | **yes** (was: 7-day loop) |

The three rows the audit marked as having no recovery path now have one. Row 5 remains as designed and is not a defect: `REQUESTED` is terminal by construction because Meta publishes no way to ask what became of an unanswered request.

**One residual window, stated rather than hidden.** The sweeper publishes its job before the transaction commits, so a commit that then fails leaves a job whose event is still owing and a later sweep publishes a second. The alternative — commit first, publish after — turns a crash into permanent silence, which is the failure the worker exists to remove. ADR-089 made that trade on the webhook path and this follows it rather than inventing a second answer.

---

## 19. Concurrency Results

| Scenario | Fan-out | Result |
| --- | --- | --- |
| Same inbound | 2, 4, 8 | 1 event, 1 message, 1 contact, 1 conversation, 1 job, **all 200** |
| Distinct first messages | 2, 4 | 1 contact, 1 conversation, 0 orphans, **all 200** |
| Same status | 8 | 1 event, no placeholder, **all 200** |
| Two sweepers, one owing event | 2 | 1 claim, 1 job |
| Sweeper claim held open across a second read | 2 | second sees nothing |
| Same idempotency key, two connections | 2 | 1 message, 1 provider call |
| Two workspaces, same key | 2 | 1 message each |

Every one uses real PostgreSQL and independent connections. The idempotency and event races additionally cross transactions, because a same-session test exercises SQLAlchemy's refusal rather than PostgreSQL's constraint.

---

## 20. Mutation Results

| Mutation | Expected detector | Killed? |
| --- | --- | --- |
| M-A — historical ownership replaced with current-owner lookup | handover tests | **KILLED** |
| M-B — event marked processed despite a refused enqueue | recovery tests | **KILLED** |
| M-C — sweeper claim made non-atomic | recovery tests | **KILLED** |
| M-D — follow-up human-mode check removed | follow-up revalidation | **KILLED** |
| M-E — follow-up opt-out check removed | follow-up revalidation | **KILLED** |
| M-F — malformed `2xx` classified undelivered | provider outcomes | **KILLED** |
| M-G — transport failure allowed to escape raw | provider outcomes | **KILLED** |
| M-H — event insert reverts to raising on the unique race | inbound concurrency | **KILLED** |
| M-I — manual template registry guard removed | send idempotency | **KILLED** |
| M-J — idempotency claim made non-atomic | send idempotency | **KILLED** |
| M-K — agent text length guard removed | provider outcomes | **KILLED** |
| M-L — unsupported inbound gets an agent job | recovery tests | **KILLED** |

**12 applied, 12 killed, 0 survived.**

Two survived their first run and both were informative rather than embarrassing, which is the argument for running mutations at all. M-C survived because the two-sweeper test could complete sequentially, so the lock was never contended — fixed by a test that holds one claim's transaction open across another's read. M-L survived because nothing covered MSG-19 until a test was written for it.

**Restoration discipline.** Each mutation was applied from an in-memory `bytes` copy and restored with `write_bytes`, never `write_text` — the audit's own runner corrupted four files by round-tripping through text on Windows, rewriting every line ending and leaving `git diff` showing four modified files with no content hunks. The anchors are LF and some working-tree files are CRLF, so the runner normalises both sides for matching and restores the file's own convention.

After the full matrix, `git status` and `git diff --stat` showed exactly the uncommitted work in progress and nothing else. No mutation remains.

Two additional mutations were applied to the **alert thresholds** (§13) rather than to code, with the same discipline and the same result.

---

## 21. Database Invariant Sweep

Run against `wasla_probe` after the end-to-end probe, whose data includes a real handover, a Redis outage and its recovery, eight-way duplicate deliveries, a gap event and a NUL payload.

```
== invariants (every one must be 0) ==
    duplicate_inbound_wamid_within_tenant        0
    duplicate_provider_event_id                  0
    same_wamid_in_two_tenants                    0
    duplicate_conversation_identity              0
    cross_tenant_conversation_contact            0
    cross_tenant_conversation_account            0
    cross_tenant_message_conversation            0
    message_without_conversation                 0
    sent_outbound_without_provider_id            0
    inbound_with_delivery_state                  0
    failed_but_delivered                         0
    failed_but_read                              0
    read_without_delivered                       0
    two_live_accounts_same_number                0
    account_without_ownership_start              0
    account_released_before_claimed              0
    overlapping_tenures_same_number              0
    inbound_outside_its_accounts_tenure          0
    event_tenant_disagrees_with_account          0
    event_without_account                        0
    processed_message_event_without_projection   0
    processed_event_without_timestamp            0
    message_without_origin                       0
    inbound_not_from_customer                    0
    outbound_labelled_customer                   0
    duplicate_idempotency_key                    0
    follow_up_sent_into_human_mode               0
    follow_up_sent_to_opted_out_contact          0
    orphan_media                                 0

== levels (reported, not asserted) ==
    unresolved_requested_sends                   0
    events_still_owing_work                      0
    events_processed                            22
    events_failed                                0
    messages_total                              22

VIOLATIONS: 0
```

Nine of these are new and exist because of this work: the tenure invariants, `inbound_outside_its_accounts_tenure` (the disclosure itself, as a query), the two `PROCESSED`-consistency checks, the three origin checks, the idempotency-key uniqueness, and the two MSG-14 contradictions.

`events_never_advanced_past_received`, which the audit reported as 48 — every event ever stored — is now `events_still_owing_work = 0` alongside `events_processed = 22`. The column is no longer dead.

---

## 22. Quality Gates

| Gate | Result |
| --- | --- |
| `ruff check app tests` | All checks passed |
| `black --check app tests` | 496 files unchanged |
| `mypy app tests` | 9 errors, all pre-existing — identical to the `a486321` baseline (see below) |
| `alembic check` | No new upgrade operations detected |
| `alembic heads` | `0056` |
| `promtool check rules` | SUCCESS, 20 rules |
| `promtool test rules` | SUCCESS |
| `amtool check-config` | valid — 1 route, 1 inhibit rule, 2 receivers |
| Migration round trip | fresh → head, check, downgrade to 0053, re-upgrade, check, head `0056` — clean |
| Backfill on populated data | 5/5 producers labelled, 0 nulls |

### A verification gap found after the report was first written, and closed

**The evidence in this section was gathered before the last two commits, and two of those commits broke it.** A re-verification pass caught it. The account below is what was wrong, why the gates as run did not say so, and what it cost.

`49ec608` made `origin` a required keyword-only argument on `MessagingService.send_text`, `send_template`, `send_media` and `MessageRepository.stage_outbound`. Its commit message says mypy "enumerated every call site — there are seven, and all seven now say what they are." That is true of `app/`, and only of `app/`. The gate run here was `mypy app`. **CI runs `mypy app tests`.** The seven production call sites were found and fixed; sixty-five in the test suite were not, because the gate that would have named them was never pointed at them.

| | Baseline `a486321` | Tip `ee33249` as committed | After this pass (`aa71e80`) |
| --- | --- | --- | --- |
| `mypy app tests` | 9 errors | 29 errors | **9 errors** — same six files, same codes |
| Full suite | 165 failures, all billing | 182 failures | **165 failures, all billing** |
| Of those, messaging-family | 0 | 17 | **0** |
| Messaging surface (11 files) | n/a | 63 failures | **126 passed, 0 failed** |

**One cause, four shapes** — and the last three are the ones a search for `.send_*(` call sites does not find:

| Shape | Count | Where it showed up |
| --- | --- | --- |
| Direct call sites | 57 | The messaging surface |
| Monkeypatched `send_text` stubs whose signature no longer matched | 2 | `test_queue_commit_visibility`, `test_media_release_ordering` |
| Stub classes standing in for `MessagingService` | 2 | `test_campaigns`, `test_follow_ups` |
| `Message` rows built in memory and never inserted | 4 | `test_conversation_endpoints`, `test_media_endpoints`, and the raw `INSERT INTO messages` in the table-driven foreign-key test |

The in-memory rows are the interesting ones. A `NOT NULL` column protects anything that reaches the database, so the only rows that can carry a null `origin` are the ones that never do — and two of those are serialised straight back out through `MessageRead`, where a null is a `500` from a response model rather than an integrity error.

**Two of the broken tests were this report's own cited proofs.** `test_a_late_status_never_moves_a_message_backwards` is the evidence offered for MSG-14 and the whole of `test_whatsapp_send_idempotency.py` is the evidence for MSG-15; both were failing with a `TypeError` before reaching an assertion, which is a test that proves nothing while looking like it proves something.

**The fixes are test-side only.** `git diff ee33249..aa71e80 -- app alembic docs deploy` is empty: no guard, no service, no migration, no alert. The mutation results in §20 stand because they were taken against the same guards, and the stub that now takes `origin` and writes what it was given — rather than hardcoding a value — is strictly a stronger check than the one it replaced.

**The lesson is about the gate, not the change.** Making `origin` required was right; a default is what an unlabelled send would silently inherit. Three things were wrong. Trusting a local gate narrower than CI's to enumerate the consequences. Gathering test evidence before the last commit rather than after it. And re-verifying against *the selection that matches the work* — the eleven messaging files were green after the first fix while seventeen failures sat in four files that selection does not contain. The full suite is what found them.

### The suite, and an honest account of it

**The baseline is not green, and it was not green before this work.**

A clean `git worktree` at `a486321` — the audited revision, with none of this work present — running the full suite against its own migration-built database fails **165 tests**, in eight files and nothing but those eight:

```
32  test_paymob_checkout.py       20  test_invoicing.py
29  test_billing_worker.py        16  test_billing_endpoints.py
28  test_dunning_lifecycle.py     12  test_entitlements.py
23  test_paymob_refunds.py         5  test_refund_entitlements.py
```

The count moves with execution order, which is the signature of the defect rather than an inconsistency in the measurement: an earlier partial run reported 30, another subset 20, and those three of the files run *alone* produce 60 between them. What does not move is the family — every failure, in every slicing tried, is billing.

The cause is a test-isolation defect, not a product one. `plans.code` is unique platform-wide, several billing test modules seed a plan with code `"pro"` using different limits, and `test_refund_entitlements.py::_catalogue` deliberately reuses an existing row rather than creating one — so whichever module ran first wins and the assertions fail with `assert 5 == 20`.

It is unrelated to messaging, it is out of scope for this remediation, and it has not been touched. It is flagged for a separate change (§28).

**Against that baseline, this work adds no failures — now measured on the whole suite rather than on a selection.** The full suite at `aa71e80` fails 165 tests — the same 165, in the same eight files, with the same per-file counts as the audited revision above. The intermediate state is on the record too: at `ee33249` the full suite failed 182, and the 17-test difference was this work's own breakage in four files the messaging selection does not contain (see the subsection above).

| Full suite | Failures | Where |
| --- | --- | --- |
| `a486321` (audited, none of this work) | 165 | Billing family |
| `ee33249` (as originally committed) | 182 | Billing family + 17 from this work |
| `aa71e80` (after re-verification) | **165** | Billing family only |

The messaging surface — every file this remediation created or changed — is green:

| Selection | Result |
| --- | --- |
| `tests/unit` (full) | 0 failed |
| The eleven-file messaging surface (126 tests) | 0 failed |
| The six new messaging integration files (49 tests) | 0 failed |
| `test_whatsapp_webhook.py`, `test_conversation_projection.py`, `test_campaigns.py`, `test_campaign_worker.py`, `test_follow_ups.py`, `test_follow_up_worker.py`, `test_outbound_delivery_protocol.py`, `test_media_worker.py` | 0 failed |
| `test_documentation_truth.py` | 0 failed |
| `test_schema_parity.py` | 0 failed |

Four existing tests needed updating, and each was a contract this work deliberately changed rather than a break: the status ordering (MSG-14), the account index list (MSG-01), the worker-kind list (MSG-02), and the agent instruction assertion (MSG-25). Two client backoff tests now inject the jitter fraction, which is why it was made injectable.

---

## 23. Documentation Changes

| File | Change |
| --- | --- |
| `docs/WHATSAPP.md` | Historical ownership and the resolution table; event state and what `200` guarantees; status reconciliation by provider id and the new monotonicity; manual send idempotency; message origin; a **Not supported** section; the Graph API version policy; the corrected retry table |
| `docs/RUNBOOK.md` | Three new procedures — *Inbound stored but never answered*, *A send WhatsApp never confirmed*, *WhatsApp is refusing this workspace's credential*; the Redis-outage entry corrected; eleven new log lines in the alerting table; the stale `whatsapp.signature_invalid` event name fixed |
| `docs/OBSERVABILITY.md` | The messaging alert group; four new state gauges; that every rule is tested from both sides |
| `docs/CAMPAIGNS.md` | The opt-out matrix with the follow-up row marked as decided; registry write-back; refused credentials; cancellation stopping the claimed batch |
| `ARCHITECTURE.md` | Webhook steps 3 and 6; the two deliberate properties restated with their missing halves; the outbound section corrected on interactive support and on who supplies idempotency; `inbound_recovery` described |
| `README.md` | Migration range `0001`–`0056` |
| `DECISIONS.md` | **ADR-101**, **ADR-102**, **ADR-103** |

**Two false promises removed and held down.** `test_documentation_truth.py` now pins the runbook's *"wait for a person until somebody requeues them"* and the WhatsApp doc's unresolved-send claim, so neither can return without the mechanism. A third entry holds down the sentence that listed unreachable client methods as things the client "covers".

The doc-truth test caught two real drifts introduced by this work — the migration range and the undescribed worker kind — which is the test doing exactly its job.

### Commits

```
076ba8e  fix(messaging): attribute inbound to who held the number at the time
6a0c0d7  feat(messaging): recover inbound whose handoff never reached the queue
5bab3e6  feat(observability): alert on messaging, and show the backlogs to a person
bf795ba  fix(followups): stop a nudge that a colleague or the customer has ruled out
1805a9a  fix(whatsapp): tell apart the failures that mean different things
4e87273  feat(messaging): let a caller say a send is the same send as before
06e3ec7  test(messaging): pin the races and failure boundaries in CI
2907ccf  fix(whatsapp): bound the poison payload, the backoff and the version horizon
49ec608  feat(messaging): record what produced each message
ee33249  docs(messaging): say what the system does, and record the three decisions
cf63e1e  test(messaging): say what produced the message at every call site
aa71e80  test(messaging): let the stubs and the in-memory rows carry an origin too
```

The last two are the re-verification pass described in §22. Both are test-side only: `git diff ee33249..aa71e80 -- app alembic docs deploy` is empty.

One boundary differs from the suggested structure and the reason is worth recording: MSG-01 and MSG-02 were rewritten as one unit of the ingest loop and are genuinely interleaved in `whatsapp_service.py`, so splitting them would have produced two commits neither of which was coherent alone. The first commit says so.

---

## 24. Product Decisions Applied

The locked decisions, applied as given:

1. **Automated follow-ups respect marketing opt-out.** Manual replies and direct AI answers remain exempt. Written into `docs/CAMPAIGNS.md` as a matrix, with the reasoning for the follow-up row.
2. **No outbound chunking.** One logical reply is one provider message. An over-limit reply is refused, not truncated and not split, and the limit lives at the service choke point.
3. **Idempotency is an explicit `Idempotency-Key` only.** No content-based deduplication of any kind; scope is `tenant + key`; a replay returns the original result with no second provider call; a key reused for different content is a `409`.
4. **A late inbound is attributed to the historical owner where one can be established, and never to the current owner otherwise.** Where none can be, it is dropped and counted rather than quarantined — the simplest safe option in this architecture, and the audit's own recommendation.
5. **`MessageKind.UNSUPPORTED` does not enqueue an agent turn.** Reactions and interactive messaging are not implemented.
6. **Campaign cancellation keeps "already sent stays sent"**, improved to stop at the next recipient rather than after the whole claimed batch.

One decision was taken beyond the brief and is flagged as such: **MSG-16 was implemented rather than deferred.** The brief permits deferral if coexistence is not near-term. It was built because the attribution is wrong today — a campaign reads as its creator's reply and a follow-up as an AI reply — the backfill turned out to be exact rather than a guess, and adding a column to `messages` gets harder as the table grows.

---

## 25. Remaining Product Decisions

Genuinely open, and not decided here:

1. **Should the inbox order by provider timestamp rather than arrival?** Both columns exist; `created_at` orders and `sent_at` carries Meta's own. Unchanged by this work.
2. **Should interactive messaging be built?** The client half exists and is unreachable. Either build it — carrying reply *ids*, never titles — or trim the dead methods. Documented as unsupported in the meantime.
3. **Should reactions be first-class?** They are currently stored as `unsupported` and cost nothing. Treating them as metadata on the referenced message is the natural shape.
4. **How long should a late inbound remain attributable?** There is no lookback horizon: an event from a year ago still resolves to whoever held the number then. Safe, but a retention policy may eventually want a bound.

---

## 26. Remaining External Verification

**No verification against a real Meta account was performed.** No test WABA, no test credentials and no designated test recipient exist in this environment, and sending unsolicited traffic to a real number is out of the question.

**EXTERNAL VERIFICATION REQUIRED** for:

| Item | What would establish it |
| --- | --- |
| Real send contract | One send from a test WABA to a designated recipient; a real `wamid`; a real status webhook reconciling |
| Real 4xx envelope | Live error codes checked against the `SendNotAttempted` / `ProviderAuth` / `TemplateWithdrawn` / `UncertainDelivery` split — the template code table especially, which is taken from Meta's documentation rather than observed |
| Whether a malformed `2xx` occurs at all | MSG-07's classification is now right either way; its frequency is unknown |
| NUL-byte reachability | Whether Meta permits `\u0000` in any field. The sanitiser makes this moot for storage; the question decides whether it was ever customer-triggerable |
| Real `Retry-After` values | Observed against a genuinely throttled account, to confirm the 30s clamp is generous rather than restrictive |
| Real retry cadence | Observed against a deliberately failing endpoint, to confirm the 7-day window |
| Production webhook delivery | Meta reaching the deployment through its real reverse proxy and TLS |
| Alert delivery | That Alertmanager routes to a receiver a human reads. `amtool check-config` proves the configuration parses, not that anybody is paged |
| Production queue topology | Replica counts, visibility timeout and worker concurrency as deployed — `inbound_recovery` in particular must be running *somewhere* |

---

## 27. Coexistence Readiness

Not implemented, and not attempted. What is now in place and what is not:

| Prerequisite | Status |
| --- | --- |
| An origin column that can distinguish a provider-originated message | **Done.** `MessageOrigin` is an enum with room for `business_app`; adding a label is an `ALTER TYPE` |
| Statuses for sends Wasla did not make | **Partly.** Resolution is by provider id across a number's holders, so an unknown id is already handled gracefully — it acknowledges and creates no placeholder. Recording an app-originated message so its statuses land somewhere is not built |
| App-originated outbound in the service-window calculation | **Not done.** `conversations.last_inbound_at` is still the whole window state |
| Not auto-answering an inbound a human already handled in the app | **Not done.** The human-mode check covers Wasla's own handoff, not the Business App's |
| Contact identity | **Unaffected.** `wa_id` per tenant survives coexistence unchanged |

---

## 28. Remaining Things That Can Be Fixed Later

**Recommended next, and not part of this remediation:**

1. **Make the local quality gate match CI's.** The documented local gate is `mypy app`; CI runs `mypy app tests`. That difference is what let a required-argument change break eighty-three tests without any gate saying so (§22). A one-word change to the documented command, or a `make check` target that runs exactly what CI runs, removes the class.
2. **The billing test-isolation defect (§22).** 165 tests fail at the audited HEAD with none of this work present — the full suite, measured in this pass against its own migration-built database — because billing modules share a platform-wide plan catalogue and overwrite each other's limits. Unrelated to messaging, untouched by it, and the single reason "the suite is green" is not a sentence anybody can say about this repository.
3. **Periodic template sync.** The write-back covers the case that costs a workspace its number; a scheduled refresh per live account would keep the registry current for the rest.
4. **Inbox ordering by provider timestamp.** Both columns exist; only the decision is missing.
5. **Interactive messaging**, or removing the dead client methods.
6. **Reactions as metadata** on the referenced message.
7. **A quarantine table for permanently unstorable events.** The NUL case is fixed at source and the general case is bounded — acknowledged, counted, alertable — but the event itself is not retained, because the row is exactly what cannot be written. A table holding the event id and reason without the payload would make such an event inspectable.
8. **Moving `429` retry ownership to the worker-level policy**, which already has exponential backoff, jitter and a bounded attempt count. The inline retry is now jittered and honours `Retry-After`, which was the reachable improvement; consolidating the two is a larger change to synchronous human-send behaviour.
9. **The Graph API version move**, before 2027-01-21. The warning now fires from 2026-10-23.

---

## 29. Updated Messaging Score

Scored on what was proven, against the audit's own dimensions.

| Dimension | Was | Now | Basis |
| --- | --- | --- | --- |
| Inbound durability | 6 | **9** | Commit-before-response unchanged; processing is now recoverable, observable and operable |
| Webhook trust boundary | 10 | **10** | Unchanged, and signature failures are now counted and alerted |
| Deduplication | 9 | **10** | Side effects still covered; the losers no longer answer 500 |
| Conversation integrity | 9 | **10** | Identity races converge without 500s; composite FKs unchanged |
| Message ordering | 7 | **7** | Unchanged — still arrival-ordered, still a documented decision |
| Status reconciliation | 6 | **9** | Provider-id-first resolution survives handover; monotonicity contradictions removed |
| Outbound idempotency | 8 | **10** | ADR-093 intact; explicit key closes the manual path; malformed `2xx` no longer licenses a resend |
| Retry correctness | 8 | **9** | `Retry-After` honoured, jitter added, narrowness preserved |
| Provider error handling | 7 | **10** | Transport, auth and template failures each have their own answer |
| Worker recovery | 10 | **10** | Unchanged, and extended to work that never reached a reservation |
| Human / AI coordination | 6 | **9** | Two-layer recheck on both paths; residual millisecond race documented |
| Phone-number lifecycle | 4 | **9** | Tenure is a column; disclosure is a swept invariant |
| Template / window correctness | 8 | **9** | Guard at the choke point; registry learns from rejections |
| Media messaging seam | 9 | **9** | Unchanged; media handoffs now participate in event state |
| Campaign / follow-up seams | 7 | **9** | Follow-up revalidates mode and consent; both stop on a dead credential |
| Tenant isolation | 9 | **10** | The one routing defect that looked like an isolation failure is closed |
| Database integrity | 10 | **10** | 29 invariants, all clean |
| Observability | 4 | **9** | Eight tested messaging alerts; four state gauges; two operator commands |
| Testing | 8 | **9** | 49 new messaging tests on real sockets and connections; 12/12 mutations killed |
| Operations | 4 | **9** | Recovery, unresolved sends, credential failure — each with a metric, an alert, a command and a procedure |
| Coexistence readiness | 2 | **4** | The origin column exists; the rest is future work |

**Weighted overall: 7.2 → 9.1 / 10.**

The two dimensions still below 9 are deliberate. Message ordering is an open product decision rather than a defect, and coexistence is future work whose one worthwhile piece of groundwork is now in place.

---

## 30. Final Verdict

# MESSAGING FINDINGS CLOSED WITH EXTERNAL PROVIDER / DEPLOYMENT VERIFICATION

**Why "closed".** All twenty-five findings are addressed: twenty-two by a code change with a regression test, one by subsumption (MSG-13 into MSG-02), and two by documentation where the honest fix was to stop claiming a capability (MSG-20) and to state a bounded semantic (MSG-22, also improved). The three production blockers are closed and each is proven at runtime against real PostgreSQL, real Redis, real sockets and a real call-counting provider rather than against mocks. Twelve deliberate mutations were applied and twelve were killed, including two that survived their first run and were only killed after the tests were made stronger — which is the evidence that the matrix is doing work rather than confirming what was already believed.

**What was preserved, deliberately and verifiably.** ADR-093's delivery semantics are intact: the `REQUESTED` commit still happens before Meta can act, `REQUESTED` rows are still never auto-retried, and uncertain outcomes are still not converted into `FAILED` for convenience — three of the twelve mutations exist specifically to prove those did not regress. The engagement barrier is untouched. Tenant isolation is untouched and now has a swept invariant of its own for the routing case that looked like an isolation failure. Every change moved classification *towards* uncertainty rather than away from it, which is the direction that cannot duplicate a customer's message.

**What re-verification changed, and what it did not.** A pass after the final commits found that eighty-three tests had been broken by the last two of them, two of which were this report's own cited proofs (§22). It was closed test-side in `cf63e1e` and `aa71e80`; no guard, service, migration or alert moved, so the mutation matrix and the runtime results above stand on the code they were taken against. What it changed is this report's confidence in its own gates, which is why §28 now opens with making the local gate match CI's rather than with a product item.

**Why the qualification.** Two things stand outside what this environment can establish.

**No real Meta verification was possible.** The provider contract was re-checked against Meta's own documentation today — the Graph API changelog confirms v21.0 is supported until 2027-01-21, as the audit reported — but no message has been sent to a real WhatsApp account through this code. §26 lists what a test WABA would settle, and the template error-code table is the item most worth confirming, because it is the one place this work acts on codes it has not observed.

**And the suite is not green at HEAD.** 165 billing tests fail on a clean worktree of the audited revision, from a test-isolation defect that has nothing to do with messaging and has not been touched. Every messaging test passes and this work adds no failures, but "the full suite is green" is not a sentence anybody can currently say about this repository, and saying it would be the kind of claim the audit was written to prevent.

**Would I trust Wasla today to carry real customer WhatsApp traffic without silent loss or duplicate customer-visible sends?**

For **duplicates — yes**, and more confidently than before. The one path that could produce one (MSG-07) is closed, transport failures no longer escape into unclassified error handling, the manual API can no longer be double-submitted into two messages, and the delivery-state model that made all of this safe was preserved rather than worked around.

For **silent loss — yes, now.** Not because messages never go unprocessed — a Redis outage still leaves work owing — but because owing work is now a state the system records, a number an operator can see, an alert that fires, a command that lists it, and a worker that finishes it. The audit's answer was "not yet, because a message can be stored and never answered with nothing to say so". There is now something to say so.

The remaining conditions are deployment facts rather than code facts: a worker must actually run `inbound_recovery`, Alertmanager must route to somebody who reads it, and one message must be sent through a real WABA before anyone believes the payloads are right.
