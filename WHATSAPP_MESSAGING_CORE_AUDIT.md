# Wasla — WhatsApp / Messaging Core Security & Reliability Audit

**Independent, adversarial, runtime-first.**
Audit date: 2026-09-11 · Auditor: automated engineering audit · Scope: inbound webhook → normalization → dedup → routing → persistence → processing → outbound send → provider reconciliation.

This audit begins after *Authentication & Account Security* and *Authorization & Multi-Tenancy* were closed. Generic JWT / password / RBAC / tenant-IDOR guarantees were not re-tested except where they bear directly on the messaging pipeline (tenant resolution from `phone_number_id`, outbound recipient and account binding, phone-number lifecycle).

---

## 1. Executive Summary

Wasla's messaging core is **substantially better engineered than the median WhatsApp integration**, and the parts that are usually wrong are right here. The outbound delivery protocol (ADR-093) is the strongest thing in the codebase: the send intent is genuinely committed to PostgreSQL *before* the Graph API can deliver anything — proven at runtime by reading `delivery_state = requested` from an independent connection while the provider call was parked mid-flight — and a send whose outcome is unknown is left unknown rather than being guessed at in the direction that duplicates a customer's message. The queue's engagement barrier (ADR-074) survives process death in Redis, so a worker killed after it called OpenAI is quarantined rather than retried. Twenty-three deliberate mutations of these guarantees were all **KILLED** by the existing suite.

The defects that remain are not in the guards that exist. They are in **three places where no guard exists at all**, which is precisely the class mutation testing cannot find:

1. **A phone number that changes hands leaks the previous workspace's inbound traffic to the new one.** Tenant is resolved purely from *current* ownership of `phone_number_id`, with no comparison against the event's own timestamp. Meta retries undelivered webhooks **for up to 7 days** (confirmed against Meta's own documentation today), so a customer message sent to Workspace A before it released a number can be delivered, projected and displayed inside Workspace B's inbox — message body, customer phone number and all. Reproduced end-to-end.

2. **An inbound message can be acknowledged with `200`, stored, and then never processed by anything, ever.** When Redis is unavailable the webhook swallows the enqueue failure by design (correctly — a 5xx would make Meta disable the subscription). But there is no sweeper, no `whatsapp_events.state` transition, and no operator query to find those messages afterwards — and because deduplication is keyed only on the event id, a later redelivery of the same message is discarded *without* re-enqueuing. The `RUNBOOK` says these conversations "wait for a person until somebody requeues them"; there is no mechanism by which anybody can requeue them. Reproduced end-to-end.

3. **Automated follow-ups revalidate less than they must.** A follow-up fires into a conversation a colleague has taken over (`mode = human`), and fires at a contact who has opted out. The AI reply path rechecks both conditions correctly and the campaign path checks opt-out correctly; the follow-up path checks neither. Reproduced end-to-end against the real `FollowUpWorker`.

Alongside these: a single message carrying a NUL byte permanently fails its webhook delivery with `500` and Meta retries it for seven days with no dead-letter and no bound; a `200` from Meta whose body lacks a message id is recorded as *failed*, which is the one direction that licenses a duplicate send; an AI reply longer than WhatsApp's 4096-character body limit is sent whole, refused, and the customer receives nothing; and there is **not one alert rule in the deployment that concerns messaging** — not for signature failures, not for provider error rate, not for queue depth, not for `agent.enqueue_failed`, although the email webhook does have one.

No cross-tenant *misrouting of outbound sends* was found. No duplicate customer-visible send was produced by any provider-failure path, any worker crash, or any concurrent retry. Every database invariant swept clean. The isolation work from the previous audits holds under messaging load.

**Verdict: MESSAGING READY WITH REQUIRED CONFIGURATION / EXTERNAL VERIFICATION** — conditional on closing MSG-01, MSG-02 and MSG-05, which are the three production blockers.

---

## 2. Repository Verification

Recorded **before** any audit action, per the brief.

| Property | Value |
| --- | --- |
| Repository | `mohamedshhahat1/wasla` (`origin` → `https://github.com/mohamedshhahat1/wasla.git`) |
| Branch | `worktree-billing-google-auth` |
| HEAD | `a486321d92309f14bd9736601b0d606ae92c18f9` |
| Working tree | clean except two untracked files |
| Untracked | `AUTHORIZATION_MULTI_TENANCY_AUDIT.md`, `FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md` |
| Stashes | none |
| Alembic head | `0053` (`alembic heads`), `alembic check` → *No new upgrade operations detected* |
| Python | 3.12.7 (`C:\Users\LAPTOP\AppData\Local\Programs\Python\Python312\python.exe`) |
| PostgreSQL | 16.15 (`pgvector/pgvector:pg16`, container `wasla-postgres-1`, healthy) |
| Redis | 7.4.11 (`redis:7-alpine`, container `wasla-redis-1`, healthy) |
| Docker | two containers up, 4 h uptime |

**Stale-install check (mandatory, because a previous audit was misled by one):**

```
>>> import app; print(app.__file__)
E:\wasla\app\__init__.py
```

The imported package is the checkout under audit. All results below are about this tree. (`tests/conftest.py` additionally asserts this at collection time — a guard added after the earlier incident, and it is still present and still correct.)

**Audit infrastructure** — isolated, and none of it touches the developer's own database:

* `wasla_msgaudit` — a **migration-built** database (`alembic upgrade head`, 38 tables, version `0053`), not `create_all`. Every runtime result below is against the schema a deployment actually gets.
* Redis logical DB `9`, flushed between probes.
* A real `uvicorn` server on a real socket for every webhook probe, so the `CommittingRoute` commit-before-response boundary is genuinely exercised (an in-process ASGI transport cannot observe it).
* A real HTTP server standing in for `graph.facebook.com`, counting every call, so retry counts and duplicate sends are measured rather than asserted.

**Post-audit tree state** (see §38 for the mutation discipline):

```
$ git status --porcelain
?? AUTHORIZATION_MULTI_TENANCY_AUDIT.md
?? FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md
$ git diff --stat          # (empty)
$ git rev-parse HEAD
a486321d92309f14bd9736601b0d606ae92c18f9
```

Identical to the starting state plus this report. No mutation remains. No unrelated work was touched.

---

## 3. Methodology

Discover → reproduce → prove → isolate root cause → classify → recommend. **No production code was remediated.** Every deliberate mutation was applied in a `try`, reverted in a `finally` from a byte-exact in-memory copy, and the tree was re-verified against git after each batch.

Evidence hierarchy used throughout:

* **CONFIRMED BY RUNTIME** — reproduced against real PostgreSQL, real Redis, a real socket, and a real (fake-endpoint) provider that counts calls.
* **CONFIRMED BY DATABASE** — a query over the post-probe database.
* **CONFIRMED BY PROVIDER** — checked against Meta's own current documentation, source and access date recorded.
* **CONFIRMED BY CODE** — read and traced, where a runtime reproduction would need a live model or a live Meta account.
* **CONFIRMED BY TEST** — an existing test pins it, and a mutation proves the test is not vacuous.

Ten probe programs were written (`p01`–`p10`), plus two mutation runners. All live in the session scratchpad; none was committed.

---

## 4. File Inventory

**53 human-authored messaging-relevant modules, ~11,500 lines.** Every one was read in full or programmatically analysed. **Unreviewed relevant messaging surface: 0.**

| Layer | Files |
| --- | --- |
| Trust boundary | `app/api/v1/webhooks.py`, `app/integrations/whatsapp/signature.py`, `app/api/route.py` |
| Provider client | `app/integrations/whatsapp/{client,payload,ownership}.py`, `app/core/net.py` |
| Ingestion & projection | `app/services/{whatsapp_service,conversation_service}.py` |
| Outbound | `app/services/messaging_service.py`, `app/services/credential_service.py` |
| Repositories | `app/repositories/{conversation,whatsapp,campaign,follow_up,template}_repository.py` |
| Models | `app/db/models/{conversation,whatsapp,whatsapp_template,campaign,follow_up,media}.py` |
| Queues & workers | `app/workers/{queue,queues,ai_worker,media_worker,campaign_worker,follow_up_worker,recovery,retry,dispatch,runner,media_queue}.py` |
| Agent | `app/agents/{orchestrator,memory}.py` |
| API | `app/api/v1/{whatsapp,conversations,contacts,campaigns,follow_ups,templates}.py` |
| Seams | `app/services/{campaign_service,follow_up_service,template_service,inbox_service,opt_out,whatsapp_account_service}.py` |
| Infrastructure | `app/db/session.py`, `app/core/{rate_limit,limits}.py` |
| Tests | 1,426 messaging-relevant tests across `tests/unit`, `tests/integration` |
| Migrations | `alembic/versions/*` through `0053` |
| Docs | `docs/{WHATSAPP,MEDIA,CAMPAIGNS,RUNBOOK,OBSERVABILITY,API}.md`, `ARCHITECTURE.md` |

**Notable absence:** `app/services/chunking.py` exists but is used **only** by knowledge-base document ingestion. There is no outbound message chunking anywhere (see §30).

---

## 5. Messaging Architecture (reconstructed from code, not documentation)

### Inbound — actual order of operations

```
Meta POST /api/v1/webhooks/whatsapp
  → raw body read                                    (webhooks.py:receive_events)
  → HMAC-SHA256 over the exact bytes                 (_require_signature → verify_signature)
        · no secret + developer env  → skipped, logged
        · no secret + any other env  → 503 (retryable)
        · bad/absent signature       → 403, nothing persisted
  → json.loads; non-dict or unparseable → 200 {"status":"ignored"}, nothing persisted
  → parse_webhook()                                  (payload.py — raises nothing, ever)
        entry[] → changes[] → value.metadata.phone_number_id
        value.messages[] → InboundMessage(event_id=<wamid>, …)
        value.statuses[] → DeliveryStatus(event_id="<wamid>:<status>", …)
        anything unreadable → `ignored` counter
  → FOR EACH message and status, in one loop:
        account = directory.get_by_phone_number_id(pn)   ← UNSCOPED, released_at IS NULL
            None      → `unknown_accounts`, continue
            inactive  → `inactive_accounts`, continue
        tenant_id = account.tenant_id
        WhatsAppEventRepository(tenant).record(event_id)  ← read, then INSERT
            already present → `duplicates`, CONTINUE (no projection, no enqueue)
        project:
            message → contact.upsert(wa_id) → flush
                    → conversation.get_or_create(contact, account) → flush
                    → touch_inbound (reopens a CLOSED conversation)
                    → message.record_inbound(wamid)
                    → media row if attached
                    → cancel pending follow-ups on this conversation
                    → opt-out if the whole text is a stop word
            status  → message.apply_status(wamid) (tenant-scoped; None if unknown)
  → session.flush()
  → enqueue AgentJob per conversation (media messages excluded — the media worker
    enqueues theirs after reading the file);  RedisError swallowed + logged
  → enqueue MediaJob per file;                RedisError swallowed + logged
  → record_provider_call(inbound_webhook, SUCCESS)
  → return 200 {"status":"accepted"}
  → CommittingRoute commits the session  ← BEFORE the response reaches the client
```

Two things here differ from what a reader would assume and both are deliberate and documented:

* **Jobs are enqueued before the transaction commits.** The stated trade: a job naming a rolled-back conversation dead-letters noisily; the alternative loses a message silently. `FIRST_ATTEMPT_TRANSIENT` (ADR-089) gives exactly one extra retry for `NOT_FOUND` on attempt 1 to cover the commit window, and the agent worker deliberately reads the conversation *before* marking the turn engaged so that retry is still legal.
* **The commit happens inside the handler chain, not in dependency teardown.** `CommittingRoute` exists because a `yield`-dependency teardown runs *after* the response is on the wire — which would mean answering `200` before the write was durable. This is right, and it is why a commit failure becomes a `5xx` rather than a lying `200`.

### Outbound — actual order of operations (ADR-093)

```
caller (human reply / AI worker / campaign / follow-up)
  → MessagingService._dispatch
  → conversation = require_by_id(id)            ← tenant-scoped
  → if require_window and not window_open → ValidationError, ZERO provider calls
  → account  = accounts.require_by_id(conversation.account_id)   ← tenant-scoped
       not active → ValidationError
  → contact  = contacts.require_by_id(conversation.contact_id)   ← tenant-scoped
  → stage_outbound(status=PENDING, delivery_state=CLAIMED); flush
  → link(message)      ← caller ties its own row (follow-up / campaign recipient) in
  → client built from CredentialService.resolve(account)  ← per-send, per-account token
  --- media only: released(session) → COMMIT ; upload file ; failure = UNDELIVERED
  → delivery_state = REQUESTED ; flush
  → released(session) → COMMIT                 ← the row is durable before Meta can act
  → POST {GRAPH}/{version}/{phone_number_id}/messages  ← no DB connection held
  → classify the outcome, then record it:
        SentMessage            → SENT   / delivery_state SENT + wamid + usage event
        UncertainDeliveryError → left PENDING / REQUESTED. Terminal. No second send.
        SendNotAttempted/429/  → FAILED / UNDELIVERED + failure_reason
        other ExternalService
```

`released()` **commits** — that is the mechanism, not a side effect, and it is what makes "the row says Meta may have this, before Meta can" literally true. Verified at runtime in §17.

---

## 6. Provider / Channel Model

| Question | Answer |
| --- | --- |
| Tenant resolution key | `whatsapp_accounts.phone_number_id`, **live claims only** (`released_at IS NULL`) |
| Uniqueness | Partial unique index `uq_whatsapp_accounts_live_phone_number_id` — one live claim per number platform-wide |
| Released rows | Kept, so conversation history survives; excluded from resolution; cannot be re-enabled |
| Ownership proof | `ownership_verified_at` — a claim is backed by proof to Meta (ADR-037) |
| Credential | `access_token_encrypted` per account, decrypted per send, falling back to the platform token |
| Customer identity | `contacts.wa_id`, `UNIQUE(tenant_id, wa_id)` — tenant-scoped |
| Conversation identity | `UNIQUE(tenant_id, contact_id, account_id)` — one per customer **per number** |
| Inbound never inferred from | the customer's phone number — correct |
| Graph API version | `v21.0` (configurable via `META_API_VERSION`) |
| Numbers per tenant | unlimited by schema; plan-enforced elsewhere |

Runtime confirmation of the model (probe 7):

```
multi_number_same_tenant:    contacts=1  conversations=2     ← one person, two numbers, two threads
cross_tenant_same_customer:  contacts_per_tenant=[1, 1]      ← same wa_id, two businesses, isolated
```

---

## 7. Message State Machine

**Discovered enums** (`app/db/models/conversation.py`) — nothing assumed:

* `MessageDirection`: `inbound`, `outbound`
* `MessageStatus`: `received`, `pending`, `sent`, `delivered`, `read`, `failed`
* `MessageDeliveryState` (outbound only, nullable): `claimed`, `requested`, `sent`, `undelivered`
* `ConversationStatus`: `open`, `pending`, `closed` · `ConversationMode`: `ai`, `human`

The two-axis design is the right one and is unusual: `MessageStatus` answers *what happened to it*, `MessageDeliveryState` answers *may this be sent again, and might Meta already have it*. A single column cannot carry both, because the honest answer to the second is sometimes "nobody knows" while the first still reads `pending`.

### Who may cause each transition

| Transition | Caused by | Guarded |
| --- | --- | --- |
| `→ pending`/`claimed` | `stage_outbound`, API or worker | tenant-scoped |
| `claimed → requested` | `_dispatch`, immediately pre-provider | committed first |
| `requested → sent` | Meta acknowledgement | needs a `wamid` |
| `requested → undelivered` | `SendNotAttempted` / 429 / rejection | only where nothing was delivered |
| `requested → (stays)` | timeout / 5xx | **terminal by construction** |
| `sent → delivered → read` | status webhook | monotonic, tenant-scoped |
| `→ failed` | status webhook `failed` | unconditional, wins over everything |
| `→ received` | inbound projection | never advances |

### Runtime transition matrix (probe 7, real webhooks, real signatures)

```
in_order              sent→delivered→read      ⇒ read      sent✓ delivered✓ read✓
reverse               read→delivered→sent      ⇒ read      sent✓ delivered✓ read✓   ← no downgrade
delivered_before_sent delivered→sent           ⇒ delivered sent✓ delivered✓
duplicate_delivered   delivered ×3             ⇒ delivered delivered_at written once
unknown_status_word   "warp"→delivered         ⇒ delivered ignored then applied
deleted_status        "deleted"                ⇒ sent      safely ignored
read_then_failed      read→failed              ⇒ FAILED    read_at✓  ← see MSG-14
failed_then_delivered failed→delivered         ⇒ failed    delivered_at✓ ← contradictory pair
```

Out-of-order and duplicate statuses are handled correctly. Two edges are not: `read → failed` downgrades a message the customer demonstrably read, and `failed` + a non-null `delivered_at` is a reachable contradiction. Both are MSG-14 (Low).

**Illegal transitions tested and rejected:** `read → sent` (no change), `delivered → pending` (not expressible), `inbound → delivered` (inbound messages never carry a `wamid` that a status can match; sweep confirms 0 inbound rows with a `delivery_state`).

---

## 8. Inbound Webhook Trust Boundary

Real HTTP, real signatures, database counted before and after (probe 1).

```
verify.correct_token        (200, 'CHAL-123')
verify.wrong_token          (403)  ← challenge never echoed
verify.bad_mode             (403)
verify.long_challenge       (200, 4000 bytes echoed)

signature.missing_signature (403)   signature.wrong_secret   (403)
signature.empty_signature   (403)   signature.modified_body  (403)
signature.malformed_header  (403)   signature.no_prefix      (403)

signature.persistence_before_after  ((0,0,0,0), (0,0,0,0))
                                     events, messages, contacts, conversations
```

**Nothing is persisted and nothing is queued before the signature is accepted.** The HMAC is computed over the exact received bytes, never over a re-serialisation. The comparison is `hmac.compare_digest`. The verification challenge is never echoed to a failed attempt and the token comparison is constant-time.

The fail-open branch is correctly gated on `is_developer_environment` rather than on `is_production` — the fix for the earlier finding is present and `staging` is on the safe side. A public deployment without a secret answers `503` (retryable) rather than `403` (dropped), which is the right refusal for a provider that retries.

**Mutation M18** (signature check bypassed) — **KILLED**.

---

## 9. Webhook Acknowledgement Semantics

**The contract, stated exactly:**

| Situation | Response | Persisted | Meta retries |
| --- | --- | --- | --- |
| Bad/absent signature | `403` | nothing | yes (harmless) |
| Unconfigured secret, non-dev | `503` | nothing | yes |
| Unparseable / non-object body | `200 {"status":"ignored"}` | nothing | no |
| Unknown `phone_number_id` | `200 {"status":"accepted"}` | nothing | no |
| Disabled account | `200` | nothing | no |
| Duplicate event | `200` | nothing new | no |
| Success | `200` | committed **before** the response is written | no |
| PostgreSQL unavailable | `500` | nothing | yes |
| Redis unavailable | `200` | **message committed, job never queued** | **no** — see MSG-02 |
| Concurrent duplicate (loser) | `500` | nothing | yes → retry finds the row |
| Payload PostgreSQL rejects | `500` | nothing | yes, forever — see MSG-03 |

The critical property — *does Wasla ever answer 200 without durable acceptance?* — holds for the message row itself. `CommittingRoute` commits inside the handler chain, so a commit failure becomes a `500`; there is no window in which a `200` describes a write that did not land. Verified by the dedicated `tests/integration/test_commit_boundary.py`, which runs a real socket precisely because an in-process transport cannot observe the ordering.

Where the contract breaks is one layer out: a `200` guarantees the message is **stored**, and does *not* guarantee it will ever be **processed**. That gap is MSG-02.

Crash-point behaviour, measured:

| Crash point | Result |
| --- | --- |
| before DB write | nothing stored, non-2xx, Meta retries |
| during DB write | transaction rolls back, `500`, Meta retries |
| after DB write, before enqueue | **message stored, job lost, 200 returned** (MSG-02) |
| after enqueue, before commit | job dead-letters or retries once via `FIRST_ATTEMPT_TRANSIENT` |
| after commit, before response | commit already durable; Meta retries; dedup absorbs it |

---

## 10. Tenant Resolution

```python
# app/repositories/whatsapp_repository.py — the one deliberately unscoped lookup
async def get_by_phone_number_id(self, phone_number_id: str) -> WhatsAppAccount | None:
    return await self._first(self._select().where(
        WhatsAppAccount.phone_number_id == phone_number_id,
        WhatsAppAccount.released_at.is_(None),
    ))
```

Isolated in a two-method class so the exception to scoping stays visible in review — good practice, and it is the only unscoped read in the module.

**A `tenant_id` in the webhook payload is meaningless.** Nothing reads one. The tenant comes from the resolved account and from nowhere else, and every repository constructed downstream is bound to `account.tenant_id`. Confirmed by code and by the invariant sweep (`event_tenant_disagrees_with_account = 0`).

**One number cannot resolve to two live tenants** — the partial unique index makes it a database fact, and the sweep confirms `two_live_accounts_same_number = 0`. **Mutation M03** (drop the `released_at IS NULL` filter) — **KILLED**.

**What is *not* considered:** the event's own timestamp. Resolution answers "who holds this number *now*", never "who held it when this message was sent". That is MSG-01.

---

## 11. Deduplication

**Key:** `whatsapp_events.UNIQUE(tenant_id, event_id)`.
**Event id:** for a message, Meta's `wamid`. For a status, the composed `"{wamid}:{status}"` — necessary, because Meta reports `sent`, `delivered` and `read` for one message under one id, and keying on the id alone would file the first and discard the rest.

**Scope: tenant, not global.** Correct for this architecture, and for a reason worth stating: a global unique constraint would let one workspace's traffic suppress another's, and because one number resolves to exactly one live tenant, a `wamid` maps deterministically to one tenant anyway. The sweep confirms the property holds in practice (`same_wamid_in_two_tenants = 0`).

**Provider contract** (verified today against Meta's own documentation — see §41): Meta guarantees **at-least-once** delivery and states plainly that *"These retries can result in duplicate webhook notifications."* Deduplication is therefore mandatory, and Wasla's is correctly positioned.

### Sequential dedup — and whether it covers side effects

```
valid.first         (200)  events=1 messages=1 contacts=1 conversations=1
replay.second       (200)  events=1 messages=1 contacts=1 conversations=1
replay.agent_queue_depth = 1        ← ONE agent job, not two
dup_in_one_payload  (200)  rows=1   ← same wamid twice inside one delivery
```

**Deduplication covers downstream side effects, not merely persistence.** The duplicate branch `continue`s *before* projection, so a replay produces no second message, no second conversation touch, no second AI job, no second follow-up cancellation, no second opt-out, and no second usage event. This is the single most commonly-botched property in WhatsApp integrations and it is right here.

**Mutation M20** (duplicate events re-projected instead of skipped) — **KILLED**.
**Mutation M01** (dedup read removed) — **KILLED**.
**Mutation M22** (inbound message-level dedup read removed) — **KILLED**.

### Concurrent dedup — real PostgreSQL, real sockets, `asyncio.Barrier`

```
dup_inbound x2:  http={500:1, 200:1}  events=1 messages=1 conversations=1 contacts=1 agent_jobs=1
dup_inbound x4:  http={500:1, 200:3}  events=1 messages=1 conversations=1 contacts=1 agent_jobs=1
dup_inbound x8:  http={500:3, 200:5}  events=1 messages=1 conversations=1 contacts=1 agent_jobs=1
dup_status  x8:  http={500:4, 200:4}  events=1  message=('delivered', <one timestamp>)
```

**Every data invariant holds under 8-way concurrency.** Exactly one canonical event, one message, one conversation, one contact, one agent job. The read is the fast path; `UNIQUE(tenant_id, event_id)` is the guarantee, exactly as the docstring claims — and unlike most such claims, this one was tested.

The losing requests receive `500`. Meta retries them and the retry finds the row and answers `200`, so the system converges. It is still noise that an operator will see and that counts toward a subscription Meta may eventually disable. See MSG-09.

---

## 12. Contact Resolution

Identity is `wa_id`, scoped `UNIQUE(tenant_id, wa_id)`. The display name is metadata: refreshed when Meta sends a newer one, never used as identity, and an absent name never erases a known one.

| Case | Behaviour | Verdict |
| --- | --- | --- |
| Same customer writes again | same contact, `last_seen_at` advanced only forwards | correct |
| Same `wa_id`, second number of the same tenant | **one** contact, two conversations | correct and intentional |
| Same `wa_id`, a different tenant | two separate contacts | correct |
| Display name changes | stored name refreshed | correct |
| `contacts` block absent | `profile_name = None`, no failure | correct |
| Same name, different `wa_id` | two contacts | correct |

---

## 13. Conversation Resolution

**One conversation per `(tenant, contact, account)`, for all time.** Not time-bounded, not status-bounded: a `CLOSED` conversation that receives inbound is reopened by `touch_inbound` rather than superseded. A business with a sales number and a support number is genuinely holding two conversations with one person, which is the right model.

`ForeignKeyConstraint(["tenant_id","contact_id"] → contacts)` and the equivalent for `account_id` mean a conversation cannot reference another workspace's contact or account **at the schema level** (ADR-100) — not merely because no code path builds one. The sweep confirms `cross_tenant_conversation_contact = 0`, `cross_tenant_conversation_account = 0`.

### Concurrent first message

```
first_message x2:  http={500:1, 200:1}  contacts=1 conversations=1 messages=1  empty_conversations=0
first_message x4:  http={500:3, 200:1}  contacts=1 conversations=1 messages=1  empty_conversations=0
```

**No duplicate conversation, no duplicate contact, and — importantly — no empty orphan conversation.** The duplicate branch cannot leave a half-built aggregate behind, because the whole transaction unwinds. The losing deliveries are rolled back entirely and depend on Meta's retry to land; since Meta retries non-2xx for up to 7 days and dedup makes the retry idempotent, they converge. This is MSG-09, not message loss.

**Mutation M15** (conversation scoped to contact only, dropping the account) — **KILLED**.

---

## 14. Inbound Ordering

* Business ordering is `messages.created_at`, with `id` as tie-break (`list_for_conversation`, `latest_inbound`), indexed by `ix_messages_conversation_id_created_at`.
* `messages.sent_at` carries Meta's own timestamp, and falls back to arrival time when the provider timestamp is absent or unparseable.

**This means webhook arrival order, not customer send order, determines display order.** Two messages sent at 10:00 and 10:01 but delivered 10:01-first will be shown in delivery order. `latest_inbound` — which sentiment analysis reads — is explicit about accepting this, and the reasoning is sound for the common case (messages in one delivery share a transaction timestamp and share a mood). It remains a real, if minor, fidelity gap; `sent_at` is present on every row, so a UI could order by it without a schema change.

Malformed provider timestamps cannot break anything:

```
timestamp[not_a_number] 200   timestamp[negative] 200   timestamp[huge]  200
timestamp[float]        200   timestamp[missing]  200
```

`_timestamp()` catches `TypeError | ValueError | OSError | OverflowError` and returns `None`; the projection substitutes arrival time. Every case stored a row and answered `200`.

---

## 15. Status Ordering / Reconciliation

Outbound messages carry `wa_message_id`, `status`, `delivery_state`, `failure_reason`, `sent_at`, `delivered_at`, `read_at`. A status webhook resolves **by `wa_message_id`, tenant-scoped**, with `UNIQUE(tenant_id, wa_message_id)` guaranteeing a single match.

Monotonicity is enforced by `_STATUS_ORDER` and by per-timestamp idempotence (`if message.delivered_at is None`), which together mean a late `sent` cannot downgrade a `read` message and a duplicate `delivered` cannot move a timestamp that is already set. Proven across the nine sequences in §7.

**Unknown message id:**

```
unknown_status: http=200  placeholder_rows=0
```

Acknowledged, logged at INFO as `whatsapp.status_for_unknown_message`, no placeholder created, no crash, no retry loop. Repeated unknown ids behave identically. Correct — this is normal traffic for a template sent from Meta's own console or for the period before a number was connected.

**Mutation M02** (allow status downgrade) — **KILLED**. **Mutation M21** (`failed` no longer terminal) — **KILLED**.

**The reconciliation gap is ownership, not ordering.** Because the status webhook resolves the tenant from the *current* owner of the number and only then looks the message up within that tenant, a status arriving after the number has moved finds nothing. See MSG-04 and §25.

---

## 16. Outbound Architecture

Four producers, one funnel (`MessagingService._dispatch`):

| Producer | Entry point | `sent_by_id` | Window | Registry checked |
| --- | --- | --- | --- | --- |
| Human reply | `POST /conversations/{id}/messages` | the authenticated user | enforced | n/a |
| Human template | `POST …/messages/template` | the authenticated user | bypassed (correct) | **no** — MSG-10 |
| Human media | `POST …/messages/media` | the authenticated user | enforced | n/a |
| AI reply | `AgentWorker._handle` | `None` | enforced | n/a |
| Campaign | `CampaignService._deliver` | campaign creator | bypassed (template) | **yes** |
| Follow-up | `FollowUpService.dispatch` | `None` | branches on window | **yes** |

**Every one of them derives recipient and account from committed conversation state.** There is no route, schema field or tool argument through which a recipient, a `phone_number_id` or a `tenant_id` can be supplied. `SendTextRequest`, `SendTemplateRequest` and every sibling carry `model_config = ConfigDict(extra="forbid")`, so an injected `to`/`recipient`/`wa_id` is a `422`, not a redirect.

Runtime confirmation:

```
recipient_sent_to=201777000001              ← conversation.contact.wa_id
path=/v21.0/PN-OUTBOUND/messages            ← conversation.account.phone_number_id
authorization_header_present=True           ← CredentialService.resolve(account)
```

**Mutation M14** (skip the `account.is_active` check) — **KILLED**.

---

## 17. Outbound Idempotency

### The delivery protocol, proven at runtime

The fake provider was made to hold a send open for two seconds while an **independent database connection** read the row:

```
state_visible_during_provider_call = requested
slow_send_final = ('sent', 'sent', 'wamid.SLOW', None)
```

**The intent is genuinely committed before Meta can deliver anything.** A process that dies at any point after this leaves a row saying "a message may have gone out" rather than no row at all — which is the whole point, because the alternative leaves a customer holding a message the system cannot find.

**Mutation M06** (move the `REQUESTED` commit to after the provider call) — **KILLED**.

### The three boundaries the brief asks about

**(a) Provider accepts, local commit then fails.** The commit that matters happens *before* the call. After the call, `mark_sent` stages `wamid` + `SENT` and the outer boundary commits. If that commit fails, the row remains `PENDING`/`REQUESTED` — which correctly reads as "Meta may have this", is never re-sent, and is discoverable through `ix_messages_unresolved_delivery`. The customer has the message; Wasla says "unknown" rather than "failed". Honest, and the safe direction. The only gap is that nothing surfaces those rows to a person (MSG-11).

**(b) Local commit succeeds, provider call never happens.** The row sits at `CLAIMED`, which provably means nothing was delivered. For a worker-driven send the job is requeued (stage `reserved` is safe to repeat). For an API-driven send the caller sees the error and may send again — legitimately, because `CLAIMED` is a proof of non-delivery.

**(c) Two workers claim the same outbound job.** `blmove` is atomic, and every terminal action funnels through `_claim_inflight`, whose `LREM` can return 1 for exactly one caller.

```
two_workers_one_job: claims=1  depths={'pending':0,'inflight':1,'delayed':0,'failed':0}
two_reapers:         outcomes=[1, 0]
```

**Mutation M16** (`_claim_inflight` always returns True) — **KILLED**. **Mutation M17** (treat the engaged stage as safe to repeat) — **KILLED**.

### Where idempotency is absent

```
double_submit_identical:   provider_sends=2  message_rows=2
sequential_retry_identical: provider_sends=2
```

The caller-facing API has **no idempotency key**. A double-clicked send button, a retried mobile request, or a proxy replay produces two customer-visible messages. Meta's send endpoint offers no idempotency key either, so this must be solved on Wasla's side. MSG-15.

---

## 18. Provider Error Classification

Every row below was produced by a **real socket** against a server instructed to misbehave (probe 5).

| Provider behaviour | Provider calls | Exception | Resulting row | Verdict |
| --- | --- | --- | --- | --- |
| `200` + message id | 1 | — | `sent` / `sent` / wamid | correct |
| `429` | **3** | `RateLimitedError` | `failed` / `undelivered` | correct; `Retry-After` ignored (MSG-17) |
| `500` | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | **correct — the important one** |
| `400` (outside window) | 1 | `SendNotAttemptedError` | `failed` / `undelivered` | correct |
| `401` (bad token) | 1 | `SendNotAttemptedError` | `failed` / `undelivered` | correct per message, wrong per workspace (MSG-18) |
| read timeout | 1 | `UncertainDeliveryError` | `pending` / **`requested`** | correct, and **not retried** |
| connection refused | 3 | `SendNotAttemptedError` | `failed` / `undelivered` | correct — nothing reached Meta |
| **connection reset** | 1 | **`httpx.RemoteProtocolError` escapes** | `pending` / `requested` | MSG-08 |
| **`200`, no message id** | 1 | `ExternalServiceError` | **`failed` / `undelivered`** | **MSG-07 — wrong direction** |
| **`200`, unreadable body** | 1 | `ExternalServiceError` | **`failed` / `undelivered`** | **MSG-07** |

The taxonomy is exactly right where it matters: a 5xx and a read timeout are *unknowns*, not failures, and the type says so rather than leaving a caller to parse an error string. `_log_failure` records Meta's own `code`/`type`/`error_subcode` to the log and hands the caller only a generic sentence — provider error text can echo request fragments and this client holds a live credential.

**Mutation M09** (5xx → permanent rejection) — **KILLED**. **Mutation M10** (timeout → retryable) — **KILLED**. **Mutation M05** (treat uncertain as failed) — **KILLED**.

---

## 19. Retry / Backoff

**Send path (`WhatsAppClient._post`):** `MAX_ATTEMPTS = 3`, linear backoff `0.5 × attempt`, **no jitter**. Only `429` and `ConnectError` are retried — the two cases where nothing can have been delivered. Everything else is terminal on the first attempt. This narrowness is the correct answer to an endpoint with no idempotency key.

**Read path (`_get`, media fetch):** the opposite policy — timeouts and 5xx are retried, because fetching a file twice costs a request and changes nothing anyone can see.

**Job path (`app/workers/retry.py`):** per-queue `RetryPolicy` with exponential backoff, a max delay, jitter, and a bounded attempt count. `RETRYABLE = {dependency_unavailable, provider_error, rate_limited, timeout, worker_crashed}`. `NOT_FOUND` is deliberately excluded but granted exactly one first-attempt retry via `FIRST_ATTEMPT_TRANSIENT` to cover the enqueue-before-commit window (ADR-089).

**The agent queue's policy is the strictest, and correctly so:** once `_TurnProgress.engage()` has been recorded, the policy becomes `NO_RETRY`. An agent turn is not idempotent — it reserves allowance, writes rows through tools, and ends by sending a WhatsApp message that carries no idempotency key.

Gaps: no jitter on the *send* retry (three replicas rate-limited together will retry in lockstep — small, since the attempt count is 3), and `Retry-After` is never read (MSG-17).

---

## 20. Worker Claim / Recovery

The model: `pending` list → `inflight` list → `reservations` hash (owner, `lease_until`, stage) → `delayed` sorted set → `failed` dead-letter list, capped at 1,000 records trimmed from the old end.

Real Redis, real leases (probe 8):

```
crash_before_engage: [('requeued',    'worker_crashed',      'reserved')]  delayed=1
crash_after_engage:  [('quarantined', 'uncertain_delivery',  'engaged')]   failed=1
lease_renewal:       renewed=1  reclaimed=0   ← a slow but living worker keeps its job
two_reapers:         outcomes=[1, 0]          ← exactly one outcome between them
attempt_budget:      ['attempt=1', 'attempt=2', …]  ← a crash SPENDS an attempt
```

**This is the strongest single guarantee in the messaging core.** A worker that died *after* calling the provider is quarantined rather than retried — because the engagement mark is written to Redis, not held in process memory, so the crash cannot take the knowledge with it (ADR-074). That is the difference between a customer getting one reply and getting two.

Leases are **renewed**, not merely long, so the visibility timeout does not have to cover the longest job anybody might ever run — the trap that forces lease-without-renewal designs into timeouts so large a crash goes unnoticed for an hour.

Operator surface: `entrypoint.sh queues status|list|replay`. Replaying the agent queue is refused without `--force` and prints why — "an agent turn is not idempotent, so a replayed job can send a customer a second reply". Deliberately a command rather than an HTTP route (ADR-071).

**Mutation M12** (drop the engagement mark) — **KILLED**. **Mutation M23** (media worker enqueues the agent job without checking that it won the release) — **KILLED**.

---

## 21. Human vs AI Mode

`ConversationMode.HUMAN` stops automatic AI replies, and the check happens **twice**:

1. **At the top of the turn** — `AgentOrchestrator.answer` returns `_nothing()` and logs `agent.skipped_human_mode`. Placed in the orchestrator rather than the worker "so no caller can skip it".
2. **After the last inference, before the reply is offered** — `_taken_over()` re-reads `Conversation.mode` as a *scalar column*, deliberately bypassing the identity map, because a `select` returning the mapped object would hand back the attributes it was loaded with. The provider was called with no transaction open, so this is precisely the row that could have moved underneath the decision.

**Mutation M04** (remove the mid-turn recheck) — **KILLED**.

The residual race is irreducible and small: a takeover landing between `_taken_over()` and the socket write still produces one AI message.

```
human_takeover_during_send: provider_sends=1  message_status=sent  conversation_mode_now=human
```

Closing it would need a row lock held across a Graph API call, which is a worse trade. **Correct by design.**

**But the recheck lives in the orchestrator, and the follow-up path does not go through it.** See MSG-05.

---

## 22. Handoff

`InboxService.set_mode` sets `mode`, sets or clears `handoff_reason`, and records an analytics event only on a *real* change — so editing a reason on a conversation a colleague already owns is not counted as a second handoff. `AnalyticsSource` distinguishes a colleague taking over from an agent giving up, because the conversation row cannot tell them apart afterwards. Assignment is separate and independently tenant-scoped.

A handoff ends the agent loop rather than taking another round, so it cannot be caught half-committed.

**What a handoff does not do: cancel pending follow-ups.** `cancel_for_conversation` is called from exactly one place — the inbound ingestion path, when the customer speaks. Nothing calls it on handoff, and `FollowUpService.dispatch` does not check `mode`. MSG-05.

---

## 23. Templates / Service Window

**The 24-hour window is modelled locally and enforced before Meta is contacted**, using the denormalised `conversations.last_inbound_at` so the check never scans the message table.

```
window.free_text_outside            = REFUSED ("Send an approved template instead.")
window.provider_calls_after_refusal = 0          ← refused before any network call
window.template_outside             = ('sent','sent','wamid.WINDOW', None)
window.never_inbound                = REFUSED    ← a cold conversation has no window
```

A conversation the customer has never written in has **no** open window — correct, and a mistake many integrations make.

**Mutation M13** (window not enforced) — **KILLED**.

**Template registry:** synced from Meta into `whatsapp_templates` with status and components; `refusal_reason_for` refuses a template the registry *knows* is not approved and deliberately allows one it has never heard of (a workspace that has not synced yet would otherwise lose every template-bearing follow-up, and "unknown" is indistinguishable from "never synced"). Campaigns apply the stricter rule. Follow-ups re-check at dispatch time, not only at scheduling time — right, because Meta pauses a template without warning and hours pass between the two moments.

Two gaps: the **manual** template-send route applies no registry check at all (MSG-10), and sync is **on-demand only** — a provider rejection never writes back to the registry and no worker refreshes it (MSG-24).

---

## 24. Media Messaging Seam

Not a full media-pipeline audit; only the properties messaging correctness depends on.

| Property | Result |
| --- | --- |
| Media message persisted | yes, with `message_media` row carrying `wa_media_id` |
| Download job queued once | yes — one job per file, not per conversation |
| Duplicate webhook → duplicate download | **no** — the duplicate branch returns before projection |
| Agent job for a media message | deliberately withheld until the file is read (ADR-092) |
| Media enqueue failure | logged, swallowed; message stays valid, media stays `PENDING` |
| Agent job enqueued after commit | yes — the transcript is durable before the job that reads it |
| Reaped-lease double release | prevented: `release()` returns whether *this* call claimed it |
| Unreadable file | still produces a conversation line — `[image, unreadable: …]` — rather than silence |
| Outbound upload | two-phase: upload while `CLAIMED` (delivers nothing), send at `REQUESTED` |
| Upload failure | ordinary `UNDELIVERED`, not an unknown — correct |
| `orphan_media` | 0 |

The upload/send split is the subtle part and it is right: uploading a file to Meta creates a handle and delivers nothing, so a failure there must not be classified as an unknown delivery.

---

## 25. Phone Number Release / Reclaim

**The messaging consequence of the lifecycle the authorization audit closed.** Reproduced end-to-end: Workspace A holds `PN-MOVES`, receives a message, sends `wamid.OUT-A`, releases the number; Workspace B claims it.

```
handover: A_account released; B_account live
inbound_after_handover:      http=200  tenant=B                    ← correct by design
late_status_after_handover:  http=200  message_owner=A  status=sent  delivered_at=None
                                       event_recorded_under=B
unknown_status:              http=200  placeholder_rows=0
late_inbound_for_A:          routed_to=B                           ← MSG-01
isolation: cross_tenant_messages=0  cross_tenant_accounts=0
```

Three distinct outcomes, and they are not equally acceptable:

1. **New inbound routes to B.** Correct and intended.
2. **A's old outbound status is dropped.** The event is recorded under B, `apply_status` looks the message up scoped to B, finds nothing, logs, returns. A's message stays `sent` with `delivered_at = NULL` for ever. No cross-tenant corruption — B's data is untouched — but A's delivery reporting is permanently wrong for every message in flight at release time. MSG-04.
3. **A late *inbound* message for A is delivered into B's inbox.** Body, customer phone number and profile name become a `Message`, `Contact` and `Conversation` inside Workspace B, readable by any B member through `GET /conversations`. **MSG-01 — a production blocker.**

The distinction the brief anticipates is exactly right: *current* routing and *historical* reconciliation need different keys. Inbound should be resolved by "who held this number when the message was sent"; status should be resolved by provider message id first and number second.

Note that the raw event for A's status is now stored in B's `whatsapp_events` — a small data-placement issue rather than a disclosure, because **no API route exposes `whatsapp_events`** (verified: the table is read only by the ingestion service and the purge service).

---

## 26. Campaign Messaging Seam

Not a full campaign audit; only the messaging properties.

| Property | Result |
| --- | --- |
| Recipient claiming | `FOR UPDATE SKIP LOCKED`, per campaign and per recipient |
| Send idempotency | `link()` writes `recipient.message_id` **inside TX1**, so a worker that dies mid-send leaves a row naming the message |
| Unresolved previous attempt | `_abandon_recipient` — terminal, never retried |
| Provider rejection | `_fail_recipient` — unlinks the message, retried until attempts exhaust |
| Uncertain delivery | `_abandon_recipient` — **never a second copy** |
| Opt-out | re-checked at delivery, not only at audience build |
| Template binding | re-checked per batch — account live, template approved |
| Account binding | campaign's own `account_id`, never an ambient one |
| Missing platform credential | fails the whole campaign rather than looping per recipient |
| Tenant scoping | services built from `campaign.tenant_id`, never from the sweep's context |
| Cancel during a batch | **the claimed batch still sends** — bounded by `min(batch_limit, messages_per_minute)`. MSG-22 |
| Invalid (not missing) credential | burns every recipient's attempts one at a time. MSG-18 |

**Mutation M07** (drop the abandon guard) — **KILLED**. **Mutation M11** (drop the opt-out guard) — **KILLED**.

The `link` + `abandon` invariant — *a pending recipient naming a message is one whose send did not resolve* — is the mechanism that makes crash-during-broadcast safe, and it is stated, implemented and tested consistently.

---

## 27. Follow-Up / Scheduled Messaging

Same two-phase claim as campaigns: claim + lease commit, then one transaction per follow-up, re-read under a row lock because the claim's transaction has committed and the row may have been cancelled since.

**What `dispatch` revalidates** (probe 6, real `FollowUpWorker`, real provider counter):

```
follow_up[ai_mode_baseline]:   provider_sends=1  status=sent
follow_up[closed_conversation] provider_sends=0  status=skipped  "The conversation was closed…"
follow_up[disabled_account]:   provider_sends=0  status=pending  "This WhatsApp number is disabled."
follow_up[human_mode]:         provider_sends=1  status=sent      ← NOT revalidated  (MSG-05)
follow_up[opted_out_contact]:  provider_sends=1  status=sent      ← NOT revalidated  (MSG-06)
```

Also correct: the template refusal is re-checked at dispatch rather than trusted from scheduling; an unresolved previous attempt is abandoned rather than resent; a follow-up outside the window with no template is `SKIPPED` terminally rather than queued forever against a window that will not reopen; and the inbound path cancels pending follow-ups *synchronously* rather than leaving it to the worker, because a nudge that talks over a customer who is already talking is exactly what must never happen.

**Mutation M08** (drop the abandon guard) — **KILLED**.

---

## 28. Contact Opt-Out

`contacts.marketing_opt_out_at` + `opt_out_source`, set on the inbound path (one string comparison, so there is no window in which a campaign sweep could write to somebody who has already said no). A timestamp rather than a boolean, because "since when" is the question a dispute actually turns on. Deliberately narrow: only a message that is *entirely* a stop word counts, in English and Arabic.

| Path | Honours opt-out |
| --- | --- |
| Campaign | **yes**, re-checked at delivery |
| Follow-up | **no** (MSG-06) |
| AI reply | no — and correctly so: refusing marketing is not refusing an answer |
| Manual human reply | no — a colleague replying to a live conversation |

The AI and manual exemptions are right and documented. The follow-up one is not obviously right and is not documented anywhere; a follow-up is an unsolicited automated nudge, which is much closer to a campaign than to a reply. Raised as a finding **and** as a product decision.

---

## 29. Multi-Number Behaviour

Verified at runtime in §6. One tenant, two numbers, one customer → **one contact, two conversations**. Outbound for conversation A goes to number A (`path=/v21.0/PN-OUTBOUND/messages` derives from `conversation.account_id`). No `first_account` / `default_account` / single-account assumption exists anywhere in the messaging path — searched for explicitly.

---

## 30. Message Chunking

**There is none.** `app/services/chunking.py` serves knowledge-base document ingestion only; nothing in the messaging path imports it. One logical outbound message is exactly one provider message, so the chunk-ordering, partial-failure and duplicate-chunk questions are all N/A — a simpler and safer design than the alternative.

The consequence is that the length limit must be respected by the caller. The API schema caps `body` at 4096, which is WhatsApp's own limit. **The AI reply path does not go through that schema**, and agents may be configured up to `MAX_AGENT_OUTPUT_TOKENS = 8192`:

```
reply[within_limit] requested=4000  chars_sent_to_meta=4000  provider_messages=1  ('sent',   None)
reply[over_limit]   requested=9000  chars_sent_to_meta=9000  provider_messages=1  ('failed', 'WhatsApp rejected the message.')
```

Sent whole, refused, recorded as failed, customer receives nothing, turn already billed. MSG-25.

---

## 31. Crash / Failure Matrix

| # | Crash point | Provider retries | Worker retries | Duplicates | Loss | Converges | Manual repair |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | after parse, before DB | yes (`500`) | n/a | no | no | **yes** | no |
| B | after DB, before enqueue | **no (`200`)** | **no** | no | **processing lost** | **no** | **none available** |
| C | after enqueue, before ACK | yes | once (`FIRST_ATTEMPT_TRANSIENT`) | no (dedup) | no | yes | no |
| D | outbound intent, before provider | n/a | yes (stage `reserved`) | no | no | yes | no |
| E | provider accepted, before local update | n/a | **no** (stage `engaged`) | **no** | state unknown, message delivered | partially | **read the conversation** |
| F | local update, before job ACK | n/a | quarantined | no | no | yes | inspect dead-letter |
| G | status webhook DB update, before ACK | yes | n/a | no (event dedup) | no | yes | no |
| H | PostgreSQL unavailable | yes (`500`) | n/a | no | no | **yes** | no |
| I | Redis unavailable | **no (`200`)** | **no** | no | **processing lost** | **no** | **none available** |
| J | payload PostgreSQL rejects | **yes, for 7 days** | n/a | no | that message | **never** | **none available** |

Rows **B**, **I** and **J** are the failures without a recovery path. Row **E** is handled as well as Meta's API permits — there is no idempotency key and no lookup keyed on anything Wasla holds before Meta answers, so `REQUESTED` is terminal by construction and a person decides from the conversation.

Recovery after a PostgreSQL outage was verified end to end:

```
pg_down.http=500
pg_recovered.http=200                       ← Meta's redelivery
after_recovery: nopg_messages=1             ← exactly once
```

---

## 32. Concurrency Matrix

All against **real** PostgreSQL and **real** Redis, with `asyncio.Barrier` or forced lease expiry. No sequential request was reported as concurrency.

| Scenario | Fan-out | Result |
| --- | --- | --- |
| Same inbound message | 2, 4, 8 | 1 event, 1 message, 1 conversation, 1 contact, 1 agent job |
| Distinct first messages, new contact | 2, 4 | 1 contact, 1 conversation, 0 orphans; losers roll back to retry |
| Same status event | 8 | 1 event, 1 timestamp, correct status |
| Two workers, one agent job | 2 | 1 claim |
| Two reapers, one expired job | 2 | 1 outcome |
| Crash before provider engagement | — | requeued, attempt spent |
| Crash after provider engagement | — | quarantined, never requeued |
| Lease renewal vs reaper | — | living worker keeps its job |
| Human takeover during a send | — | 1 provider send (irreducible) |
| Concurrent identical outbound sends | 2 | **2 provider sends, 2 rows** (MSG-15) |

---

## 33. Database Integrity

Swept after every probe had run, against the migration-built database.

```
duplicate_inbound_wamid_within_tenant   0      cross_tenant_message_conversation    0
same_wamid_in_two_tenants               0      cross_tenant_conversation_contact    0
duplicate_live_conversation             0      cross_tenant_conversation_account    0
message_without_conversation            0      sent_message_without_provider_id     0
delivered_but_marked_failed             0      read_without_delivered               0
inbound_with_delivery_state             0      outbound_missing_delivery_state      0
unresolved_sends_still_open             0      two_live_accounts_same_number        0
event_without_account                   0      event_tenant_disagrees_with_account  0
orphan_media                            0
events_never_advanced_past_received    48      ← every event ever stored
```

**Every integrity invariant is clean.** The last line is not an integrity failure but a dead-column finding: `whatsapp_events.state`, `processed_at` and `error` are written once at insert and **never** advanced by anything. `WhatsAppEventState.PROCESSED` and `FAILED` are unreachable. MSG-13 — and the same absence is why crash-point **B** has no recovery path.

Schema quality worth noting: composite foreign keys (`ADR-100`) make cross-tenant references *structurally* impossible rather than merely unbuilt by today's code; the partial index `ix_messages_unresolved_delivery` is correctly scoped so a healthy deployment pays nothing for it; `alembic check` reports no drift between models and migrations.

---

## 34. Analytics Side Effects

Duplicate deliveries cannot double-count, for one structural reason: **every side effect lives behind the dedup `continue`.** `WHATSAPP_MESSAGE_RECEIVED`, `CONVERSATION_CREATED`, the analytics event, the follow-up cancellation and the opt-out are all downstream of it, and `usage_events` counts *messages*, not deliveries — so a batch of three does not become one.

Campaign messages are metered twice on purpose (`WHATSAPP_MESSAGE_SENT` for the allowance, `CAMPAIGN_MESSAGE` for the broadcast view); the recipient row moving to `SENT` in the same transaction is what keeps the pair safe.

`AI_REQUEST` is written by the per-round reservation and explicitly passed `requests=0` in the post-turn recorder, so a turn is not billed twice.

No unread counters exist, so §73's questions are N/A.

---

## 35. Security / Secret Redaction

| Check | Result |
| --- | --- |
| Token in logs | no — `_log_failure` records only Meta's `code`/`type`/`error_subcode` |
| Token in exception body | no — the caller gets a generic sentence |
| Token returned to a client | no — `has_own_credential` is the only fact exposed |
| Token at rest | encrypted (`access_token_encrypted`, ADR-034); the column name makes a screenshot unmistakable |
| Correct credential selected | per account, resolved per send, plaintext living no longer than the call |
| Stack traces in production | no — `details` is gated on `is_developer_environment` |
| DB failure disclosure | `{"exception": "TimeoutError"}` appears **only** in developer environments; production returns the request id alone |
| App secret | never echoed; comparison constant-time |
| Rate limiting on the webhook | deliberately **never applied** — a `429` to Meta sheds messages, not load (ADR-032) |
| Request body cap | 32 MB global, **1 MB for webhooks** (`webhook_max_request_bytes`) |
| Outbound SSRF | `build_guarded_client` resolves once and connects to a validated public address; `https` only |
| Customer filename | never used to build a path; storage derives its own key |
| Media served back | `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`, canonical type only |

The DB-outage response was checked specifically because an earlier audit asserted a sanitised answer. It is sanitised in production and verbose in `test`/`local`/`dev`, which is the correct split.

---

## 36. Observability

**Metrics that exist and are emitted:** `wasla_provider_requests_total{provider="whatsapp",operation="send_message"|"fetch_media"|"inbound_webhook",outcome=…}`, `wasla_provider_request_duration_seconds`, `wasla_jobs_total`, `wasla_job_failures_total`, `wasla_queue_pending_jobs`, `wasla_queue_inflight_jobs`, `wasla_queue_delayed_jobs`, `wasla_queue_dead_letter_jobs`, `wasla_queue_expired_reservations`, `wasla_queue_oldest_pending_age_seconds`, `wasla_unhandled_errors_total`, `wasla_dependency_up`.

That is a genuinely good set. The send call is timed from *before* the retry loop, so a send that took three attempts is reported as costing the customer three attempts' worth of time — a detail most instrumentations get wrong.

**The `/metrics` endpoint exists and these are scraped.** The gap is the last step:

| Signal | Metric | Emitted | Scraped | **Alerted** |
| --- | --- | --- | --- | --- |
| Inbound webhook stopped arriving | ✓ | ✓ | ✓ | **✗** |
| WhatsApp signature failures | log only | — | — | **✗** (the *email* webhook **has** an alert) |
| Meta 429 / error rate | ✓ | ✓ | ✓ | **✗** |
| Queue depth / lag / oldest pending | ✓ | ✓ | ✓ | **✗** |
| Dead-letter growth | ✓ | ✓ | ✓ | **✗** |
| `agent.enqueue_failed` | log only | — | — | **✗** |
| Unresolved (`requested`) sends | **no metric** | — | — | **✗** |
| Duplicate inbound rate | **no metric** | — | — | **✗** |

`deploy/monitoring/alerts.yml` contains 12 rules. **Not one concerns messaging.** MSG-12.

**Tracing and correlation** are strong: `request_id` on every log line, a `queue.publish` span the worker's attempt is a child of, and `job_span` rooting each attempt in the trace it was queued from. End-to-end tracing of one provider message is possible — the local message id, `wa_message_id`, tenant, conversation and worker attempt all appear, and none of the credential material does.

---

## 37. Test Quality

**4,215 tests in the full suite, 1,426 of them messaging-relevant. All passing** (`exit 0`, zero failures, against the migration-built database). `ruff`: clean. `black --check`: 541 files unchanged. `mypy`: no issues in 246 source files. See the appendix for a contamination incident in this audit's own harness that produced a spurious 14-failure run, and how it was isolated.

The suite is well above average, and specifically it avoids the false greens the brief asks about:

* `test_outbound_delivery_protocol.py` uses **real commits on its own connection pool**, because the shared `db_session` joins the test's transaction as a savepoint — which would make a committed send indistinguishable from an uncommitted one, exactly the property under test. It uses a **barrier, not a stopwatch**, so "the connection was given back" is observed at a chosen moment rather than inferred from a duration.
* `test_commit_boundary.py` runs a **real socket**, because an in-process ASGI transport cannot observe response-vs-commit ordering.
* `test_whatsapp_webhook.py` asserts that an unsigned delivery *never reaches ingestion*, not merely that it returns 403.
* Duplicate tests count **rows and agent jobs**, not just rows.
* The integration `conftest` documents why `WASLA_TEST_SCHEMA=models` is *not* the safe default and runs migration-parity in CI — written after a real incident where four enum labels lived in Python and in no migration while the suite stayed green.

**The gaps, stated precisely:**

1. **No concurrency test for inbound deduplication or conversation creation.** Only `test_whatsapp_ownership.py` and `test_queue_commit_visibility.py` use `asyncio.gather`/`Barrier`; neither covers duplicate webhook delivery or the concurrent-first-message race. The invariants hold — I proved it — but the suite would not notice if they stopped.
2. **No test covers the Redis-outage inbound path end to end.** `M19` (never enqueue) was killed, so "a stored message gets an agent job" *is* pinned — but the specific sequence *Redis down → stored → redelivery suppressed by dedup* is not.
3. **No test asserts a bound on poison-message retries**, because no bound exists.
4. **No test covers follow-up revalidation of `mode` or opt-out**, because no such guard exists to test.
5. `test_documentation_truth.py` checks the migration range, worker kinds, route count, environment table and billing importability — but nothing in `docs/WHATSAPP.md`, which is where the drift is.

---

## 38. Mutation / Non-Vacuity

**23 mutations. 23 KILLED. 0 SURVIVED. 0 EQUIVALENT.**

| # | Mutation | Expected detector | Verdict |
| --- | --- | --- | --- |
| M01 | dedup read removed | event persistence | KILLED |
| M02 | status downgrade allowed | projection | KILLED |
| M03 | released number still resolves | account + projection | KILLED |
| M04 | AI ignores human takeover | orchestrator | KILLED |
| M05 | uncertain treated as failed | delivery protocol | KILLED |
| M06 | `REQUESTED` set after the provider call | delivery protocol | KILLED |
| M07 | campaign abandon guard removed | campaigns | KILLED |
| M08 | follow-up abandon guard removed | follow-ups | KILLED |
| M09 | 5xx → permanent rejection | client + protocol | KILLED |
| M10 | timeout → retryable | client + protocol | KILLED |
| M11 | campaign opt-out guard removed | campaigns | KILLED |
| M12 | engagement mark removed | queue visibility | KILLED |
| M13 | service window not enforced | endpoints | KILLED |
| M14 | outbound account check removed | endpoints | KILLED |
| M15 | conversation scoped to contact only | projection | KILLED |
| M16 | queue claim not atomic | queue | KILLED |
| M17 | engaged stage treated as safe | queue | KILLED |
| M18 | signature check skipped | webhook | KILLED |
| M19 | agent job never enqueued | webhook + projection | KILLED |
| M20 | duplicate events re-projected | webhook + opt-out | KILLED |
| M21 | `failed` no longer terminal | projection | KILLED |
| M22 | inbound message dedup read removed | projection | KILLED |
| M23 | media release guard removed | media worker | KILLED |

**This is the most important negative result in the audit, and it should be read carefully.** No mutation survived, so there is no finding of the form "a guard exists but nothing tests it". Every guarantee the system claims is pinned by a test that actually fails when the guarantee is removed.

It follows that **the findings below are not weak guards — they are absent guards**, which mutation testing cannot discover by construction. That is why the runtime probes in §21–§31 carried the weight of this audit rather than the mutation matrix.

**Restoration discipline.** Each mutation was applied inside a `try` and restored from a byte-exact copy in a `finally`. The first batch was restored with `write_text`, which introduced CRLF line endings on Windows — `git diff` showed four files modified with **no content hunks**, only an encoding warning. The runner was switched to `write_bytes` and the four files restored with `git checkout --`. Final state:

```
$ git status --porcelain
?? AUTHORIZATION_MULTI_TENANCY_AUDIT.md
?? FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md
$ git diff --stat      # empty
```

---

## 39. Real HTTP / Webhook E2E

Every inbound result in this report came from a real `uvicorn` server on a real TCP socket, with real HMAC signatures over real bytes, and real `httpx` clients. Every outbound result came from a real HTTP server on a real socket that counted calls — so "one provider call" means one socket connection observed, not one mock assertion. Retry counts (3 for 429, 3 for connection-refused, 1 for 5xx) were **measured**, not asserted.

---

## 40. Real Provider Verification

**Not performed. No test Meta credentials, WABA, or designated test recipient were available in this environment,** and the brief forbids sending unsolicited messages to real numbers.

The following therefore **require external verification** and are not claimed here:

* that `graph.facebook.com/v21.0/{phone_number_id}/messages` accepts Wasla's exact payloads;
* that a real `wamid` round-trips through a real status webhook into `apply_status`;
* that Meta's real 4xx codes map onto the `SendNotAttempted` / `UncertainDelivery` split as this audit assumes;
* whether Meta permits a NUL byte in any webhook field (which is what decides whether MSG-03 is customer-triggerable or merely latent);
* real per-number throughput limits and `Retry-After` values.

---

## 41. Official Meta Contract Review

Primary sources only; blogs were not used where official documentation exists. **Accessed 2026-09-11.**

| Contract | Meta's statement | Wasla's behaviour | Verdict |
| --- | --- | --- | --- |
| Webhook signature | `X-Hub-Signature-256`, HMAC-SHA256 over the raw payload | exactly that, `compare_digest` | **matches** |
| Retry policy | *"Meta retries delivery with decreasing frequency until the request succeeds, for up to 7 days"* — [Cloud API webhooks](https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks/) | relies on retries for the `500` paths | **matches**, and materially worsens MSG-01 and MSG-03 |
| Duplicates | *"These retries can result in duplicate webhook notifications"* | dedup on `(tenant_id, event_id)`, side effects included | **matches** |
| Delivery semantics | at-least-once, not exactly-once | idempotent by construction | **matches** |
| Required response | `200` | `200` for everything actionable or permanently unactionable | **matches** |
| Graph API versions | v26.0 current; **v21.0 supported until 2027-01-21** — [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog/) | `META_API_VERSION = v21.0` | **supported, expiring in ~4 months** (MSG-21) |
| Status values | `sent`, `delivered`, `read`, `failed` | exactly those four mapped; others ignored safely | **matches** |
| Customer-service window | 24 h from the customer's last message; templates outside it | modelled and enforced locally | **matches** |
| Send idempotency | **no idempotency key on the send endpoint** | retries narrowed to provably-unsent failures | **matches**, and is the reason for ADR-093 |

The older generic [Graph API webhooks page](https://developers.facebook.com/docs/graph-api/webhooks/getting-started) states **36 hours** rather than 7 days. The WhatsApp-specific page is the governing one for this product; the discrepancy is noted so a reader does not mistake one for the other. Either way the retry horizon is long enough that MSG-01's window is measured in days.

---

## 42. Coexistence Readiness

Wasla does not support WhatsApp Coexistence today, and nothing here claims it does. The assumptions that would break:

| Assumption in the code | Breaks under coexistence because |
| --- | --- |
| Every outbound message originates from Wasla (`stage_outbound` is the only writer of outbound rows) | the WhatsApp Business App can originate messages on the same number |
| A status webhook maps to a message Wasla sent, or to nothing | statuses would arrive for sends Wasla never made |
| `MessageDirection` + `sent_by_id` are enough to attribute a message | there is **no origin column** (MSG-16); an app-originated message would be indistinguishable |
| `conversations.last_inbound_at` is the whole service-window state | an app-originated outbound message changes the window Wasla cannot see |
| `answering` enqueues an agent turn for every inbound message | an inbound message a human already answered in the app would still get an AI reply |
| Contact identity is `wa_id` per tenant | unchanged — this one survives |

**Classification: future architectural work, not a current defect.** The one item that is worth doing *now* regardless is the origin column (MSG-16) — it is needed for auditability and analytics today, and it is the foundation coexistence would build on.

---

## 43. Documentation Drift

| Document | Claim | Reality |
| --- | --- | --- |
| `docs/WHATSAPP.md:162` | *"A `requested` row is shown to a person rather than resolved by a sweep; `ix_messages_unresolved_delivery` is the query that finds them"* | The index exists. **No query, route, command, metric or runbook section reads it.** Nothing is shown to anyone. MSG-11 |
| `docs/RUNBOOK.md:39` | *"Those conversations wait for a person until somebody requeues them"* | **There is no requeue mechanism.** `queues replay` acts only on dead-letter records; a never-enqueued message has none. MSG-02 |
| `docs/RUNBOOK.md:48` | *"Rows in `whatsapp_events` but silence from the agent means the queue"* | Correct diagnosis, but no query is given and `state` never changes, so there is nothing to filter on |
| `docs/WHATSAPP.md:119` | *"`WhatsAppClient` covers … location, reply buttons, lists"* | Literally true of the client; `send_buttons`, `send_list` and `send_location` are **unreachable dead code** and inbound interactive replies carry no content. A reader would infer a capability that does not exist |
| `docs/RUNBOOK.md:848` | `agent.enqueue_failed` rated **High** | Correct, and there is no alert rule for it |
| `docs/RUNBOOK.md` §payments | a full unresolved-`requested` procedure for **payments** | No equivalent for messages, though the delivery model is the same shape |

`docs/RUNBOOK.md:39` deserves credit: it states the Redis trade-off honestly rather than hiding it. The drift is that it promises a remedy that was never built.

---

## 44. Findings Ledger

---

### MSG-01 — A late inbound webhook is delivered into the workspace that now owns the number

**Severity: High** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME + CONFIRMED BY PROVIDER**
**Stage:** tenant resolution · **Files:** `app/repositories/whatsapp_repository.py:46`, `app/services/whatsapp_service.py:103`

**Problem.** Tenant is resolved from *current* live ownership of `phone_number_id`. Nothing compares the event's own timestamp against when each workspace held the number. Meta retries undelivered webhooks for **up to 7 days**, so a message sent to Workspace A before it released a number can be delivered after Workspace B has claimed it — and is then projected into B's data.

**Expected contract.** A customer message belongs to the workspace that held the number **when the message was sent**. A workspace must never see another workspace's customer content.

**Actual behaviour.** The message body, the customer's WhatsApp number and their profile name become a `Message`, `Contact` and `Conversation` inside Workspace B, readable by any B member through `GET /conversations` and `GET /conversations/{id}/messages`. B's AI agent is then enqueued to answer it.

**Reproduction** (`p04_handover.py`): A receives `wamid.IN-A`; A releases `PN-MOVES`; B claims it with its own ownership proof; a webhook carrying `wamid.LATE-FOR-A` with body *"sent while A still owned the number"* is delivered.

```
handover:            A_account released; B_account live
late_inbound_for_A:  routed_to=B
```

**Database evidence.** `select tenant_id from messages where wa_message_id='wamid.LATE-FOR-A'` → B's tenant id. Contact and conversation created under B. No constraint prevents it: the composite foreign keys only require internal agreement, which this satisfies.

**Provider evidence.** Meta, *Set up Webhooks* (accessed 2026-09-11): *"Meta retries delivery with decreasing frequency until the request succeeds, for up to 7 days."* The exposure window is days, not seconds — and it widens whenever Wasla returns `500`, which MSG-03 and MSG-09 both cause.

**Root cause.** `get_by_phone_number_id` answers "who holds this number now". The messaging pipeline needs "who held it when this event happened", and the released row that carries the answer is excluded from the query by the same filter that makes handover work at all.

**Customer impact.** One business reads another business's customer conversations. Both parties proved ownership of the number to Meta, so neither did anything wrong — which makes it worse, not better, since there is no misuse to detect.

**Production blocker: yes.**

**Recommended remediation.** Resolve inbound against the account whose tenure contains the event timestamp: select the live account when `message.timestamp >= account.created_at`, otherwise the most recent released row for that number whose `[created_at, released_at)` interval contains it. Where no interval matches, count the event as `unknown_accounts` and drop it — losing a stray message is strictly better than handing it to a stranger. A grace margin (say, released within the last 7 days) keeps the extra lookup off the hot path for numbers that never moved.

**Required regression tests.** A handover fixture asserting that (a) a pre-release-timestamped inbound is attributed to A or dropped, never to B; (b) a post-claim inbound is attributed to B; (c) B's conversation list never contains A's contact.

**Mutation proof.** None applies — this is an absent guard, not a weak one. M03 confirms the *existing* `released_at` filter is tested.

**External verification required:** confirm Meta's real retry cadence against a live WABA.

---

### MSG-02 — A message can be acknowledged, stored, and never processed, with no recovery path

**Severity: High** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** enqueue after persistence · **Files:** `app/services/whatsapp_service.py:296-318`, `app/db/models/whatsapp.py:55-60`, `docs/RUNBOOK.md:39`

**Problem.** When Redis is unavailable the webhook logs `agent.enqueue_failed`, swallows it, and returns `200`. That decision is **correct in isolation** — a non-2xx would make Meta retry the whole delivery and eventually disable the subscription. What is missing is everything that should follow it: there is no sweeper, `whatsapp_events.state` never leaves `received`, and no query, command or metric can find the affected messages. Worse, because dedup is keyed on the event id alone, a later redelivery is discarded **without re-enqueuing**.

**Expected contract.** Either the acknowledgement implies eventual processing, or an operator can find and requeue what was missed. The `RUNBOOK` promises the second.

**Actual behaviour.** Neither. The message is visible in the inbox and no agent will ever answer it, and nothing reports how many such messages exist.

**Reproduction** (`p03_outage.py`), app pointed at a dead Redis:

```
redis_down.http=200 body={"status":"accepted"}
redis_down.persisted  events=1  messages=1  real_queue_depth=0
redis_down.event_states=[('received', 1)]
redis_recovered_redelivery.http=200          ← Meta redelivers
after_recovery: noredis_messages=1  queue_depth=1  ← the 1 belongs to a DIFFERENT message
```

The redelivery of `wamid.NOREDIS` hit the duplicate branch and produced **no** agent job.

**Database evidence.** `events_never_advanced_past_received = 48` across the whole audit — the state column is write-once dead metadata; `WhatsAppEventState.PROCESSED` and `FAILED` are unreachable from any code path.

**Root cause.** Two decisions that are individually right and jointly lossy: enqueue failures are swallowed to protect the subscription, and deduplication short-circuits *before* projection so that a replay produces no side effects — including, unavoidably, the side effect that is missing.

**Customer impact.** A customer writes during a Redis outage and is never answered. Nobody knows how many. The `RUNBOOK` tells the operator to requeue them and there is nothing to requeue them with.

**Production blocker: yes.**

**Recommended remediation.** Make `whatsapp_events.state` mean what its name says. Advance it to `PROCESSED` when the job is successfully enqueued (and for statuses, once projected); leave it `RECEIVED` when the enqueue fails. Then add a sweeper — or at minimum an operator command and a `wasla_unprocessed_inbound_events` gauge — over `state = 'received' AND created_at < now() - interval '5 minutes'`, re-deriving the conversation from the event's payload. The partial index `ix_whatsapp_events_tenant_id_state` already exists for exactly this query.

**Required regression tests.** Redis down → message stored, `state = received`; Redis up → sweeper enqueues exactly one job; sweeper run twice → still one job.

**Mutation proof.** M19 (never enqueue) and M20 (re-project duplicates) both KILLED, which shows the happy path is pinned. Neither can reach this case, because the behaviour under audit is the *absence* of a sweeper.

---

### MSG-03 — A payload PostgreSQL rejects poisons its delivery for seven days

**Severity: Medium** (High impact, low-to-unknown likelihood) · **Confidence: High for the behaviour, Low for reachability** · **Label: CONFIRMED BY RUNTIME · REQUIRES EXTERNAL VERIFICATION**
**Stage:** event persistence · **Files:** `app/services/whatsapp_service.py:126-141`, `app/api/v1/webhooks.py:136`

**Problem.** The raw payload is stored as `JSONB`. PostgreSQL cannot represent `\u0000` in `text` or `jsonb`. A message containing a NUL byte anywhere — body, caption, filename, profile name — raises `UntranslatableCharacterError`, the transaction unwinds, and the webhook answers `500`. Meta retries for up to 7 days. **Every retry fails identically.** There is no dead-letter, no quarantine and no bound.

**Reproduction** (`p07_states_and_payloads.py`): a text message whose body is `"before" + chr(0) + "after"`.

```
unicode[nul_byte]: http=500 stored=None
```

versus every other Unicode case, all of which round-tripped exactly:

```
arabic ✓  arabic_emoji ✓  combining ✓  arabic_indic_digits ✓  bidi ✓
zero_width ✓  newlines ✓  rtl_override ✓  emoji_zwj ✓  60k_chars ✓ (60000 stored)
```

**Root cause.** No sanitisation between `json.loads` and the `JSONB` insert, and no bounded failure record for a signed event that cannot be stored. §64's question — *what prevents Meta retrying forever?* — currently answers: nothing.

**Customer impact.** That customer's message never lands. If it is at the head of a batch it takes its siblings with it. A stream of such messages drives the subscription's failure rate toward the threshold at which Meta disables it, which is an outage for every workspace on the deployment.

**Production blocker: no** (reachability unproven), **but the unbounded-retry shape is a blocker in general** — any future payload PostgreSQL rejects behaves identically.

**Recommended remediation.** Two independent fixes, both cheap. (1) Strip `\u0000` from the decoded payload before storage, recording that it was stripped. (2) Add a bounded failure record: on a *persistent* storage failure for a signed event, write a quarantine row and answer `200`, so one bad message stops being an integration-wide liability.

**Required regression tests.** A signed delivery with a NUL in the body → `200`, message stored with the NUL removed, one agent job. A signed delivery that cannot be stored for any reason → `200` plus a quarantine record, never an unbounded `500`.

**External verification required:** whether Meta permits a NUL in any webhook field. If it sanitises, this is latent; if not, it is customer-triggerable.

---

### MSG-04 — A delivery status arriving after a number changes hands is silently dropped

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** status reconciliation · **Files:** `app/services/conversation_service.py:150-170`, `app/repositories/conversation_repository.py:346`

**Problem.** A status webhook resolves the tenant from the number's *current* owner, then looks the message up scoped to that tenant. After a handover the message belongs to the previous owner, so the lookup finds nothing.

**Reproduction** (`p04_handover.py`):

```
late_status_after_handover: http=200  message_owner=A  status=sent
                            delivered_at=None  event_recorded_under=B
```

**Actual behaviour.** `whatsapp.status_for_unknown_message` at INFO. A's message stays `sent` with `delivered_at = NULL` for ever. Every message A had in flight at release time is permanently un-reconciled. The raw event is filed in **B's** `whatsapp_events` — not a disclosure, since no API exposes that table, but it is A's data in B's rows.

**Root cause.** Number-first resolution for something whose identity is the provider message id. A `wamid` is globally unique and `UNIQUE(tenant_id, wa_message_id)` already makes at most one row match platform-wide.

**Customer impact.** Wrong delivery reporting and wrong analytics for the releasing workspace. No corruption of the new owner's data.

**Production blocker: no.**

**Recommended remediation.** Resolve a status by `wa_message_id` **first**, across the accounts that have ever held the number, and only then apply the tenant scope of whichever message matched. A status whose id matches nothing keeps today's behaviour exactly.

**Required regression tests.** A status for A's message after B claims the number updates A's row and leaves B's data untouched.

**Mutation proof.** M02 and M21 confirm the status projection itself is tested; this is the resolution step in front of it.

---

### MSG-05 — A follow-up fires into a conversation a human has taken over

**Severity: Medium-High** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** scheduled outbound · **Files:** `app/services/follow_up_service.py:292-340`, `app/services/inbox_service.py:86`

**Problem.** `FollowUpService.dispatch` revalidates conversation status, the service window, the template and the account — but not `conversation.mode`. `InboxService.set_mode` does not cancel pending follow-ups. So an AI-scheduled nudge is delivered underneath a colleague who has taken the conversation over.

**Expected contract.** `ConversationMode.HUMAN` is documented as stopping automatic AI replies *entirely*. A follow-up is an automatic AI-originated message.

**Reproduction** (`p06_state_revalidation.py`, real `FollowUpWorker`, provider calls counted):

```
follow_up[ai_mode_baseline]: provider_sends=1 status=sent
follow_up[human_mode]:       provider_sends=1 status=sent   ← mode='human'
```

**Root cause.** The human-mode recheck lives in `AgentOrchestrator`, which the follow-up path does not go through. `cancel_for_conversation` has exactly one caller — inbound ingestion.

**Customer impact.** A customer talking to a person receives *"Just checking in!"* from the machine mid-conversation. It undermines the handoff feature and looks careless to the customer. A colleague cannot prevent it: taking a conversation over is the documented way to stop the AI.

**Production blocker: yes** — handoff that does not actually stop automated outbound is a broken promise about a feature the product sells.

**Recommended remediation.** Two layers, as elsewhere in this codebase. (1) `InboxService.set_mode(HUMAN)` calls `cancel_for_conversation`, cancelling with a reason. (2) `dispatch` checks `conversation.mode is HUMAN` and `_skip`s — belt and braces, since the mode can change between claim and send.

**Required regression tests.** A due follow-up on a `HUMAN` conversation → zero provider calls, status `skipped`. Handing a conversation over cancels its pending follow-ups.

**Mutation proof.** No guard exists to mutate. M08 confirms the *other* follow-up guard is tested.

---

### MSG-06 — A follow-up is sent to a contact who has opted out

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME · PRODUCT DECISION**
**Stage:** scheduled outbound · **Files:** `app/services/follow_up_service.py:292`, `app/services/campaign_service.py:546`

**Problem.** `CampaignService._deliver` re-checks `contact.accepts_campaigns` at delivery time, explicitly so somebody who opts out mid-campaign does not receive the rest of it. `FollowUpService.dispatch` never reads it.

**Reproduction:**

```
follow_up[opted_out_contact]: provider_sends=1 status=sent
```

with `marketing_opt_out_at` set and `opt_out_source = 'customer'`.

**The policy question, stated rather than decided.** The codebase's position is that opting out refuses *marketing*, not answers — which is why the AI reply and the manual human reply are deliberately exempt, and that is right. A follow-up sits between the two: it is unsolicited and automated like a campaign, but conversational like a reply. Nothing in the documentation says which it is.

**Customer impact.** A customer who wrote "STOP" receives an automated nudge. In markets with a WhatsApp opt-out expectation this is a compliance exposure and a route to the complaints that get a number's quality rating cut.

**Production blocker: no** — this needs a product decision first.

**Recommended remediation.** Decide, write it in `docs/CAMPAIGNS.md`, then implement. The defensible default is to honour opt-out for follow-ups, because a follow-up is not a reply to anything the customer just said.

**Required regression tests.** Whichever way it goes, a test asserting the decided behaviour with the reason in its docstring.

**Mutation proof.** M11 confirms the campaign guard is tested; the follow-up path has none.

---

### MSG-07 — A `200` from Meta with no message id is recorded as a *failed* send

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** provider outcome classification · **Files:** `app/integrations/whatsapp/client.py:815-828`, `app/services/messaging_service.py:_dispatch`

**Problem.** `_message_id` and `_decode` raise `ExternalServiceError` when a `200` response has no usable id or no readable body. `_attempt` catches it and `_undelivered` records `FAILED` / `UNDELIVERED` — a state whose entire documented meaning is *"nothing was delivered, and that is known rather than assumed"*, and which explicitly licenses a new send.

**A `200` is Meta saying it accepted the message.** The customer very probably received it.

**Reproduction:**

```
accepted_without_id: provider_calls=1  row=('failed','undelivered',None,'WhatsApp accepted the message without an identifier.')
unreadable_body:     provider_calls=1  row=('failed','undelivered',None,'WhatsApp accepted the message without an identifier.')
```

Note the failure reason says *"accepted"* while the state says *undelivered*. The code already knows.

**Root cause.** An inconsistency in the ADR-093 taxonomy. A 5xx — *less* likely to have been delivered than a 200 — is correctly `UncertainDeliveryError`. A malformed 200 falls through to the generic `ExternalServiceError` branch and lands on the wrong side.

**Customer impact.** For a campaign, `_fail_recipient` retries → **the customer receives the message twice**. For a follow-up, the same. This is the one path in the whole outbound design that can produce a duplicate customer-visible send.

**Production blocker: no** (requires a malformed 200 from Meta, which is uncommon), **but it contradicts the core doctrine** and should be fixed on principle.

**Recommended remediation.** Raise `UncertainDeliveryError` from `_message_id` and `_decode` when the status was 2xx. The row then stays `REQUESTED`, campaigns `_abandon` instead of `_fail`, and no second copy is sent.

**Required regression tests.** A 200 with `{"messages": []}` leaves `requested`; a campaign recipient in that state is abandoned, not retried.

**Mutation proof.** M05 and M09 confirm the surrounding classification is tested; this specific branch is not.

---

### MSG-08 — A reset connection escapes the client's error taxonomy

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** provider transport · **Files:** `app/integrations/whatsapp/client.py:728-748`

**Problem.** `_post` catches `httpx.ConnectError` and `httpx.TimeoutException`. It does not catch `httpx.RemoteProtocolError`, `httpx.ReadError`, `httpx.WriteError` or `httpx.NetworkError`. A connection reset while the response is being read — routine behind a load balancer — propagates as a raw `httpx` exception through `_attempt`, which only catches `ExternalServiceError` and `RateLimitedError`, and then out of `MessagingService._dispatch` entirely.

**Reproduction:**

```
connection_reset: provider_calls=1  raised=RemoteProtocolError: Server disconnected without sending a response.  row=None
```

**What actually happens downstream.** The `delivery_state` is left at `REQUESTED`, which is the *correct* state — so the outcome is accidentally right. But: the API returns `500` with a raw exception name rather than `201` with a recorded attempt; the AI worker dead-letters the job (already engaged, so `NO_RETRY`); `CampaignService._deliver` does not catch it, so the rest of the batch is abandoned for that campaign; `FollowUpService.dispatch` does not catch it either.

**Root cause.** An incomplete exception list. `httpx.TransportError` is the parent class that covers all of these.

**Customer impact.** No duplicate and no loss — the delivery-state model saves it. But callers see an unclassified error, a campaign batch stops early, and the failure is harder to diagnose than it should be.

**Production blocker: no.**

**Recommended remediation.** Catch `httpx.TransportError` and raise `UncertainDeliveryError` (the request left the process; the response did not arrive — the same epistemic position as a read timeout). Keep `ConnectError` handled *first*, since it provably never reached Meta and is the one case worth retrying.

**Required regression tests.** A server that resets mid-response leaves the row `requested` and raises `UncertainDeliveryError`, not `RemoteProtocolError`.

---

### MSG-09 — Concurrent deliveries answer `500` to the losers

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Stage:** event persistence, conversation creation · **Files:** `app/repositories/whatsapp_repository.py:180`, `app/repositories/conversation_repository.py:122`

**Problem.** Both `record()` and `get_or_create()` read-then-insert. Concurrent deliveries all miss the read, all insert, and the unique constraint rejects the losers with an `IntegrityError` that becomes a `500`.

**Reproduction:**

```
dup_inbound x8:  http={500:3, 200:5}   invariants all correct
first_message x4: http={500:3, 200:1}  invariants all correct
dup_status x8:   http={500:4, 200:4}   invariants all correct
```

**This is not data loss.** Every invariant holds and Meta retries a `500` for up to 7 days, so the losers land on a subsequent attempt — verified for the PostgreSQL-outage case, which follows the same path. But it is a `500` rate proportional to burst concurrency, on an endpoint whose failure rate Meta watches, and it widens MSG-01's window every time it happens.

**Root cause.** No conflict handling on the insert. `ON CONFLICT DO NOTHING` with a `RETURNING` check would let the loser distinguish "somebody else stored this" from "something broke".

**Production blocker: no.**

**Recommended remediation.** For the event insert, `INSERT … ON CONFLICT (tenant_id, event_id) DO NOTHING RETURNING id` — no row returned means duplicate, so return `created=False` instead of raising. For `get_or_create`, wrap the insert in a savepoint and re-read on `IntegrityError`, exactly as `WhatsAppAccountRepository.connect` already does for the number-claim race. That pattern is already in this codebase and is a good one.

**Required regression tests.** The concurrency tests from §32 promoted into the suite — 8 concurrent identical deliveries must answer `200` eight times with the same invariants.

---

### MSG-10 — The manual template send bypasses the registry guard

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY CODE**
**Stage:** outbound authority · **Files:** `app/api/v1/conversations.py:163`, `app/services/messaging_service.py:send_template`

**Problem.** `CampaignService` and `FollowUpService` both call `refusal_reason_for` before sending a template — and `follow_up_service.py:341` explains why in the strongest terms available: *"sending one it has since withdrawn is the thing that costs a workspace its number."* `POST /conversations/{id}/messages/template` calls `MessagingService.send_template` directly with no such check, even though `TemplateService.refusal_reason` exists and is unused by any route.

**Actual behaviour.** A member may send a template the local registry records as `REJECTED`, `PAUSED` or `DISABLED`. Meta refuses it (a `400`), so no policy violation reaches a customer — but the account accrues exactly the rejected-template attempts the automated paths are careful to avoid.

**Root cause.** A guard applied in the service layer for two callers and at no shared choke point.

**Production blocker: no.**

**Recommended remediation.** Move the check into `MessagingService.send_template` so every caller inherits it, and let campaigns keep their stricter "must exist and be approved" rule on top.

**Required regression tests.** `POST …/messages/template` naming a `PAUSED` template → `422`, zero provider calls.

---

### MSG-11 — No operator surface for unresolved sends, contrary to the documentation

**Severity: Medium** · **Confidence: High** · **Label: DOCUMENTATION DRIFT · OPERATIONS**
**Stage:** outbound reconciliation · **Files:** `docs/WHATSAPP.md:162`, `app/db/models/conversation.py:__table_args__`

**Problem.** `docs/WHATSAPP.md` states that a `requested` row *"is shown to a person rather than resolved by a sweep; `ix_messages_unresolved_delivery` is the query that finds them."* The index exists and is correctly partial. **Nothing reads it** — no API field, no route, no `queues` subcommand, no metric, no runbook section. Grepping `delivery_state` across the app finds only writers plus the two `delivery_uncertain` reads in the campaign and follow-up services.

By contrast, `docs/RUNBOOK.md` has a full procedure for unresolved **payments**, including the query and an explicit *"do not clear these by hand"*. Messages have the same model and none of the support.

**Customer impact.** Messages that may or may not have reached a customer accumulate invisibly. Nobody can answer "did this go out?" without direct SQL nobody has been given.

**Production blocker: no**, but it is the difference between a good design and an operable one.

**Recommended remediation.** Three small pieces: a `wasla_unresolved_outbound_messages` gauge beside the existing `wasla_oldest_pending_payment_age_seconds`; a `RUNBOOK` section mirroring the payments one; and either an admin route or a `queues`-style command listing unresolved sends per workspace.

---

### MSG-12 — Not one alert rule concerns messaging

**Severity: Medium** · **Confidence: High** · **Label: OPERATIONS**
**Stage:** observability · **Files:** `deploy/monitoring/alerts.yml`

**Problem.** Twelve alert rules: lifecycle, orphaned workspaces, billing wind-down, purge, reauthentication, refresh replay, password logins, auth rate limits, **the email webhook's signature failures**, unhandled errors, scrape target down. **None for WhatsApp.** The messaging metrics all exist and are scraped; nothing fires on them.

Unmonitored: inbound deliveries stopping (Meta disabling the subscription), WhatsApp signature failures, Meta 429 rate, provider error rate, queue depth and oldest-pending age, dead-letter growth, `agent.enqueue_failed` — which the `RUNBOOK` itself rates **High**.

The email asymmetry is the clearest evidence that this is an oversight rather than a decision.

**Customer impact.** Every failure mode in this report is silent. MSG-02 in particular produces no signal at all.

**Production blocker: no**, but it should be closed before real traffic.

**Recommended remediation.** At minimum: `rate(wasla_provider_requests_total{provider="whatsapp",operation="inbound_webhook"}[15m]) == 0` during business hours; a sustained non-success ratio on `operation="send_message"`; `wasla_queue_oldest_pending_age_seconds` above a threshold; `increase(wasla_queue_dead_letter_jobs[1h]) > 0`; and a counter plus alert for webhook signature failures, matching `EmailWebhookSignatureFailures`.

---

### MSG-13 — `whatsapp_events.state` is never advanced

**Severity: Low** (but it is the *mechanism* MSG-02 needs) · **Confidence: High** · **Label: CONFIRMED BY DATABASE**
**Files:** `app/db/models/whatsapp.py:55-60`, `app/repositories/whatsapp_repository.py:198`

`state`, `processed_at` and `error` are written once at insert and never touched. `WhatsAppEventState.PROCESSED` and `FAILED` are unreachable. Sweep: `events_never_advanced_past_received = 48` — every event ever stored during this audit.

The model's docstring says the raw log means *"a projection bug can be fixed and replayed rather than losing the traffic"*, but with no state there is nothing to distinguish what has been replayed from what has not. Fix this and MSG-02 largely fixes itself.

---

### MSG-14 — Two reachable status-state contradictions

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/repositories/conversation_repository.py:346-380`

```
read_then_failed:      read→failed     ⇒ status=failed,  read_at set
failed_then_delivered: failed→delivered ⇒ status=failed,  delivered_at set
```

`apply_status` returns early on `FAILED` and sets it unconditionally, so `failed` wins from any state — including one the customer demonstrably read. And a row can carry `status='failed'` alongside a non-null `delivered_at`, which is a contradiction a UI would have to pick a side on.

The docstring promises *"the projection never moves a message backwards"*. It does, in this one case. WhatsApp is not expected to send `failed` after `read`, so the practical impact is near zero — but the behaviour and the stated rule should agree.

**Remediation.** Apply the same `_STATUS_ORDER` monotonicity to `FAILED`, or state explicitly that a provider `failed` is authoritative and record `failed_at` separately rather than overwriting.

---

### MSG-15 — The outbound API has no idempotency key

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/api/v1/conversations.py:146`, `app/schemas/conversation.py:35`

```
double_submit_identical:    provider_sends=2  message_rows=2
sequential_retry_identical: provider_sends=2
```

`SendTextRequest` carries `body` and `preview_url` and nothing else. A double-clicked button, a retried mobile request or a proxy replay puts two copies on the customer's phone.

Meta offers no idempotency key either, so this must be solved here. **Remediation:** accept an `Idempotency-Key` header, store it on the message with `UNIQUE(tenant_id, idempotency_key)`, and return the existing message on a repeat. The delivery-state machinery to make that safe already exists.

**Product decision:** whether to key on the header alone, or additionally suppress an identical body to the same conversation within a short window.

---

### MSG-16 — A message does not record what produced it

**Severity: Low-Medium** · **Confidence: High** · **Label: CONFIRMED BY CODE**
**Files:** `app/db/models/conversation.py:Message`

There is no origin column. Attribution today is inferred from `sent_by_id`, and the inference is wrong twice:

| Producer | `sent_by_id` | Reads as |
| --- | --- | --- |
| Human reply | the user | human ✓ |
| AI reply | `None` | AI ✓ |
| **Campaign** | **the campaign creator** | **a human reply ✗** |
| **Follow-up** | `None` | **an AI reply ✗** |

It is recoverable by joining `campaign_recipients` / `follow_ups` on `message_id`, but not by reading the transcript — which is what an auditor, an analytics query and a colleague all actually do. It is also the foundation any coexistence work would need (§42).

**Remediation.** A `MessageOrigin` enum column (`human`, `agent`, `campaign`, `follow_up`, `system`, and later `provider`), defaulted for existing rows from the same joins.

---

### MSG-17 — `Retry-After` is ignored and the send backoff has no jitter

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/integrations/whatsapp/client.py:748-755, 776`

A `429` is retried three times with linear backoff `0.5 × attempt` and no jitter; `Retry-After` is never read. Measured: `rate_limited_429: provider_calls=3`.

Three attempts 0.5 s and 1.0 s apart against an account Meta is already throttling is unlikely to help and may deepen the throttle. With several replicas the retries are synchronised. The blast radius is small because the attempt count is 3.

**Remediation.** Honour `Retry-After` when present; add jitter; consider not retrying `429` inline at all and letting the job-level policy — which already has exponential backoff and jitter — own it.

---

### MSG-18 — An invalid credential fails every recipient individually

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/integrations/whatsapp/client.py:766-772`, `app/services/campaign_service.py:_deliver`

Every 4xx becomes `SendNotAttemptedError("WhatsApp rejected the message.")`. A `401`/`403` (expired or revoked token, `code 190`) is therefore indistinguishable from `131047` (outside the window) or `131009` (bad parameter). Measured: `auth_401: row=('failed','undelivered',…,'WhatsApp rejected the message.')`.

A *missing* platform credential is handled well — `DependencyUnavailableError` fails the whole campaign rather than looping per recipient, with a comment explaining exactly why. An *invalid* one is not, so a workspace whose token was revoked burns every recipient's attempt budget one at a time and ends with an audience of individually-failed recipients and no single explanation.

**Remediation.** Classify Meta's `code 190` / HTTP 401 / 403 as a distinct `ProviderAuthError`, and let `dispatch_batch` treat it like `DependencyUnavailableError` — fail the campaign once, with a message naming the credential.

---

### MSG-19 — Reactions and other unsupported types trigger a billed AI turn with no content

**Severity: Low-Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/services/whatsapp_service.py:151`, `app/services/conversation_service.py:MESSAGE_KINDS`

Every non-media inbound message is added to `answering`, whatever its type. Measured:

```
type[reaction]:  stored=('unsupported', None)
type[order]:     stored=('unsupported', None)
type[system]:    stored=('unsupported', None)
type[contacts]:  stored=('unsupported', None)
```

Each of these creates a message row **and an agent job**. `memory._text` renders it as `[unsupported]`, so the model is told a customer sent something with no content and answers anyway — one billed OpenAI turn, possibly one WhatsApp message back. A customer tapping 👍 on three messages costs three AI requests and may get three replies to nothing.

Safety is fine: acknowledged, no crash, no retry loop, correctly stored as `unsupported`. It is the cost and the customer experience that are wrong.

**Remediation.** Skip the agent enqueue for `MessageKind.UNSUPPORTED`, or handle reactions explicitly as metadata on the referenced message rather than as messages in their own right.

---

### MSG-20 — Interactive replies carry no content; interactive sends are unreachable

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME · PRODUCT DECISION**
**Files:** `app/integrations/whatsapp/payload.py:_message_text`, `app/integrations/whatsapp/client.py:send_buttons/send_list/send_location`

```
type[button]:      stored=('interactive', None)   ← payload "YES"/"Yes" discarded
type[interactive]: stored=('interactive', None)   ← button_reply id "opt-a" discarded
type[location]:    stored=('location',    None)   ← coordinates discarded
```

The values survive in the raw event and nowhere else. The agent is shown `[interactive]`.

`send_buttons`, `send_list` and `send_location` exist on the client and are **called by nothing** — no route, no service, no agent tool. So interactive messaging is not a shipped capability, and the inbound gap is consistent with that. `docs/WHATSAPP.md:119` listing them under "covers" is true of the client and misleading about the product.

**Remediation (when the feature is built).** Carry `interactive.*_reply.id` — never the title, which is display text — into `body` or a dedicated column, and the location coordinates into structured fields. Until then, correct the documentation.

---

### MSG-21 — The configured Graph API version expires in four months

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY PROVIDER**
**Files:** `app/core/config.py:679`

`META_API_VERSION = "v21.0"`. Per Meta's changelog (accessed 2026-09-11), v21.0 is supported until **2027-01-21**; v26.0 is current. The setting is configurable, so this is a planning item rather than a defect — but four months is short, and there is nothing that warns when it lapses.

**Remediation.** Plan a move to a current version, re-verify the send and media contracts against it, and add a start-up warning when the configured version is within 90 days of its published sunset.

---

### MSG-22 — Cancelling a campaign does not stop the batch already claimed

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY CODE**
**Files:** `app/services/campaign_service.py:402, 463-484`

`dispatch_batch` claims up to `min(batch_limit, messages_per_minute)` recipients, then sends them all without re-reading the campaign's status. `cancel()` uses `require_by_id`, not a locked read, and does not touch pending recipients. Since the send commits part-way through, the worker no longer holds a lock a cancel would block on.

Bounded and small — at most one batch — and "what was sent stays sent" is the documented intent. Worth stating because the brief asks specifically.

**Remediation (optional).** Re-check `campaign.status` inside the recipient loop, breaking on `CANCELLED`.

---

### MSG-23 — No concurrency test for inbound deduplication or conversation creation

**Severity: Informational** · **Label: TEST QUALITY**

Of 1,426 messaging-relevant tests, only `test_whatsapp_ownership.py` and `test_queue_commit_visibility.py` exercise real concurrency. `test_recording_the_same_event_twice_stores_one_row` is sequential; `test_the_database_rejects_a_duplicate_event_for_one_workspace` adds two rows to one session and asserts `IntegrityError`, which tests the constraint rather than the race.

The invariants hold — §11 and §13 prove it — but nothing in CI would notice if they stopped. **Remediation:** promote the probe scenarios into `tests/integration`.

---

### MSG-24 — The template registry is refreshed only on demand

**Severity: Low** · **Confidence: High** · **Label: CONFIRMED BY CODE**
**Files:** `app/api/v1/templates.py:56`, `app/services/template_service.py:sync`

Sync is an admin-triggered route. No worker refreshes it, and a Meta rejection at send time never writes back to the registry row. A template Meta paused stays `APPROVED` locally until somebody clicks sync, and every follow-up and campaign using it is refused by Meta one at a time.

`refusal_reason_for`'s "unknown means allow" asymmetry is the right call and is well argued — but it leans on the registry being reasonably current, and nothing keeps it current.

**Remediation.** A periodic sync per live account, and a write-back that marks a template `PAUSED`/`REJECTED` when Meta refuses a send naming it.

---

### MSG-25 — An over-long agent reply is refused and the customer gets nothing

**Severity: Medium** · **Confidence: High** · **Label: CONFIRMED BY RUNTIME**
**Files:** `app/workers/ai_worker.py` (final `send_text`), `app/core/config.py:55`

The API caps `body` at 4096 (WhatsApp's own limit). The AI reply path does not go through that schema, and agents may be configured up to `MAX_AGENT_OUTPUT_TOKENS = 8192` (default 2048 — still well over 4096 characters for most languages).

```
reply[within_limit] requested=4000  chars_sent_to_meta=4000  ('sent',   None)
reply[over_limit]   requested=9000  chars_sent_to_meta=9000  ('failed', 'WhatsApp rejected the message.')
```

Sent whole, refused with a `400`, recorded `failed`, **customer receives nothing**, turn already billed. No truncation, no split, no retry, and — because there is no alert on provider failures (MSG-12) — no signal.

**Remediation.** Validate the reply length in `MessagingService.send_text` for every caller. Then decide: refuse and log, truncate at a sentence boundary with a marker, or split into ordered chunks. Splitting reintroduces every question §30 is currently free of, so refuse-and-log is the smallest correct first step.

**Product decision:** which of the three.

---

## 45. Production Blockers

Confirmed, reproduced, and each one a case where a customer message is mishandled in a way nobody can detect or repair.

| ID | Blocker | Why it blocks |
| --- | --- | --- |
| **MSG-01** | Late inbound delivered to the number's new owner | Cross-tenant disclosure of customer content, triggered by an ordinary product operation plus a 7-day provider retry window. No attacker needed. |
| **MSG-02** | Message acknowledged, stored, never processed, unrecoverable | Silent loss of the *answer*, with no signal, no query and no remedy — and the documented remedy does not exist. |
| **MSG-05** | Follow-up fires into a human-owned conversation | Human takeover is the product's own mechanism for stopping the AI, and it does not stop this. Customer-visible. |

**Not blockers** — improvements, product decisions, or operational work: MSG-03 (reachability unproven, though the unbounded-retry shape should be fixed), MSG-04, MSG-06 through MSG-25.

---

## 46. Product Decisions Required

Genuinely open questions, not silently decided here.

1. **Does opt-out cover follow-ups?** (MSG-06) Campaigns yes, AI replies no. A follow-up sits between. Recommend honouring it.
2. **What should happen to an over-long AI reply?** (MSG-25) Refuse, truncate, or split. Recommend refuse-and-log first.
3. **Should the send API be idempotent, and keyed how?** (MSG-15) Header only, or header plus a short-window content check.
4. **Should a late inbound for a released number be dropped or quarantined?** (MSG-01) Dropping is simplest and safe; quarantining preserves it for support.
5. **Should reactions produce a message row at all?** (MSG-19) They currently do, and cost an AI turn each.
6. **Are interactive messages a roadmap item?** (MSG-20) The client half exists and is unreachable; either build it or trim it.
7. **Should cancelling a campaign stop the in-flight batch?** (MSG-22) Currently it does not.
8. **Should the inbox order by provider timestamp rather than arrival?** (§14) Both columns exist.

---

## 47. External Verification Required

Everything below is outside what this environment can establish. **None of it is claimed in this report.**

| Item | Evidence needed |
| --- | --- |
| Real Meta send contract | A test WABA and designated test recipient; one send, one `wamid`, one status webhook round trip |
| Real 4xx code mapping | Live error codes checked against the `SendNotAttempted` / `UncertainDelivery` split |
| NUL-byte reachability (MSG-03) | Whether Meta permits `\u0000` in any webhook field |
| Real retry cadence | Observed retry timing against a deliberately failing endpoint |
| Per-number throughput limits | Meta's real rate limits and `Retry-After` values for the target tier |
| Production webhook delivery | Meta reaching the deployment through its real reverse proxy and TLS |
| Production queue topology | Replica counts, `queue_visibility_timeout_seconds`, worker concurrency as deployed |
| Alert delivery | That Alertmanager routes to a receiver a human reads |
| Ownership-verification flow | `ownership_verified_at` written against a real Meta ownership check |

---

## 48. Things That Can Be Fixed Later

MSG-13 (dead state column — *unless* it is the vehicle for fixing MSG-02, in which case it is now), MSG-14 (status contradictions), MSG-16 (origin column — earlier if coexistence is on the roadmap), MSG-17 (`Retry-After`, jitter), MSG-19 (reaction turns), MSG-20 (interactive), MSG-21 (API version — before 2027-01-21), MSG-22 (campaign cancel), MSG-23 (concurrency tests), MSG-24 (template sync).

---

## 49. Messaging Security / Reliability Score

Out of 10. Scored on what was proven, not on what is claimed.

| Dimension | Score | Basis |
| --- | --- | --- |
| Inbound durability | 6 | Commit-before-response is right; MSG-02 leaves processing unrecoverable |
| Webhook trust boundary | 10 | Raw-body HMAC, constant-time, nothing persists before it, fail-open correctly gated |
| Deduplication | 9 | Covers side effects, holds at 8-way concurrency; loses a point to MSG-09's 500s |
| Conversation integrity | 9 | No duplicates, no orphans, composite FKs enforce it structurally |
| Message ordering | 7 | Correct and indexed, but arrival-ordered rather than provider-ordered |
| Status reconciliation | 6 | Monotonic and idempotent; MSG-04 drops statuses across a handover |
| Outbound idempotency | 8 | ADR-093 is excellent; MSG-15 and MSG-07 are the gaps |
| Retry correctness | 8 | Narrow where it must be; no `Retry-After`, no jitter |
| Provider error handling | 7 | Taxonomy right where it matters; MSG-07, MSG-08, MSG-18 |
| Worker recovery | 10 | Durable engagement barrier, renewed leases, atomic claims, attempt budget spent by crashes |
| Human / AI coordination | 6 | Two-layer recheck on the AI path; MSG-05 defeats handoff on the follow-up path |
| Phone-number lifecycle | 4 | Claim uniqueness is a database fact; MSG-01 and MSG-04 are both here |
| Template / window correctness | 8 | Window modelled and enforced pre-network; MSG-10, MSG-24 |
| Media messaging seam | 9 | Read-then-answer ordering, dedup-safe, release guard correct |
| Campaign / follow-up seams | 7 | Campaign excellent; follow-up under-revalidates |
| Tenant isolation | 9 | Every sweep clean, outbound binding unspoofable; MSG-01 is a routing defect, not an isolation one |
| Database integrity | 10 | Every invariant clean; composite FKs; `alembic check` clean |
| Observability | 4 | Good metrics and tracing, **zero** messaging alerts |
| Testing | 8 | 1,426 tests, 23/23 mutations killed, real sockets and real commits; no inbound concurrency tests |
| Operations | 4 | Queue tooling is good; nothing for unresolved sends or unprocessed inbound |
| Coexistence readiness | 2 | Not supported, and no origin column to build on |

**Weighted overall: 7.2 / 10.**

---

## 50. Final Verdict

# MESSAGING READY WITH REQUIRED CONFIGURATION / EXTERNAL VERIFICATION

**Why not `MESSAGING NOT READY`.** The properties that are hardest to retrofit are already correct and are *proven* correct. The outbound delivery protocol commits before the provider can act — observed from an independent connection mid-call, not inferred. The engagement barrier survives process death in Redis, so a worker killed after calling OpenAI is quarantined rather than retried. Deduplication covers downstream side effects, not just rows, and holds at 8-way concurrency against real PostgreSQL. Tenant isolation is enforced by composite foreign keys rather than by convention. Every database invariant swept clean. Twenty-three mutations of these guarantees were all killed. **No test produced a duplicate customer-visible message from any provider failure, any worker crash, or any concurrent retry.** That is the bar most WhatsApp integrations fail, and this one clears it.

**Why not `MESSAGING READY`.** Three defects, each reproduced end to end, each in a place where no guard exists:

* a phone number changing hands leaks the previous workspace's inbound traffic into the new one, for as long as Meta retries — up to seven days (**MSG-01**);
* a message can be acknowledged, stored, and never processed, with no signal, no query and no remedy, while the runbook promises one (**MSG-02**);
* an automated follow-up fires into a conversation a colleague has taken over, defeating the product's own mechanism for stopping the AI (**MSG-05**).

None of the three is architectural. MSG-01 is a timestamp comparison in one resolver. MSG-02 is a column the schema already has, a sweeper, and a gauge. MSG-05 is a mode check and a cancellation call. All three are days of work, not a redesign — which is itself evidence the architecture is sound.

**Two conditions attach to the verdict.** First, there is **no alert rule anywhere in the deployment that concerns messaging** (MSG-12), so every failure in this report is currently silent. Second, **no verification against a real Meta account was possible** (§40); the provider contract was checked against Meta's own documentation, not against a live WABA.

**Would I trust Wasla today to carry real customer WhatsApp traffic without silent loss or duplicate customer-visible sends?**

For **duplicates — yes.** The delivery-state model, the narrow retry policy and the engagement barrier are collectively the best answer available to an API with no idempotency key, and they were tested rather than trusted. The one path that can duplicate (MSG-07) requires Meta to return a malformed `200`.

For **silent loss — not yet.** Not because messages vanish — they do not; every message that is acknowledged is stored — but because a message can be stored and never answered, with nothing to say so and nothing to fix it. Close MSG-02, MSG-01 and MSG-05, add the messaging alerts, and the answer becomes yes.

---

## 51. Required Final Questions

1. **Can a valid inbound message be lost after a 200?** The **message** is never lost — it is committed before the response is written. Its **processing** can be: Redis-outage enqueue failures are unrecoverable (MSG-02).
2. **Can Meta retry one webhook and cause two message rows?** No. `UNIQUE(tenant_id, event_id)` plus the pre-projection `continue`. Proven at 8-way concurrency.
3. **Can it cause two AI replies?** No. `replay.agent_queue_depth = 1`.
4. **Two push/notification effects?** N/A — no notification subsystem exists in this tree.
5. **Is `wamid` deduplicated atomically?** Yes. The read is the fast path; the unique constraint is the guarantee. Losers get `500` and Meta's retry finds the row.
6. **Global or tenant-scoped?** Tenant-scoped, `(tenant_id, event_id)`.
7. **Does that match provider guarantees?** Yes. Meta guarantees at-least-once and warns of duplicates; one number resolves to one live tenant, so a `wamid` maps deterministically to one tenant. Sweep: `same_wamid_in_two_tenants = 0`.
8. **Can concurrent first messages create duplicate conversations?** No. 4-way: 1 contact, 1 conversation, 0 orphans.
9. **Can out-of-order inbound be displayed incorrectly?** Yes — ordering is by `created_at` (arrival), not by the provider timestamp, which is stored in `sent_at` but unused for ordering.
10. **Can out-of-order statuses downgrade a message?** No, except `failed`, which wins from any state including `read` (MSG-14).
11. **Can duplicate statuses double-count analytics?** No. Every side effect is behind the dedup `continue`.
12. **Can a status for an unknown message crash processing?** No. `200`, INFO log, no placeholder row.
13. **Does a foreign `tenant_id` in the payload matter?** No. Nothing reads one.
14. **How is inbound tenant resolved?** `phone_number_id` → live `whatsapp_accounts` row → `tenant_id`. Never from the customer's number.
15. **Can one number resolve to two live tenants?** No. Partial unique index; sweep `two_live_accounts_same_number = 0`.
16. **What happens after release/reclaim?** New inbound → new owner (correct). Late inbound → **new owner (MSG-01)**. Old statuses → **dropped (MSG-04)**.
17. **Can an old outbound status update the new owner's data?** No. It updates nothing; B's data is untouched.
18. **Can an old queued send use a number after ownership changed?** No. `_dispatch` loads the account through the tenant-scoped repository and refuses a non-active one; a released account is not active.
19. **Are contact identities tenant-safe?** Yes. `UNIQUE(tenant_id, wa_id)`; proven with the same customer across two tenants.
20. **Are conversation identities tenant-safe?** Yes, and enforced by composite foreign keys, not convention.
21. **What defines one conversation?** `(tenant_id, contact_id, account_id)`, unbounded in time. A closed conversation reopens on inbound.
22. **Can two workers send one outbound message?** No. `blmove` plus `_claim_inflight`. Measured: 1 claim of 2, 1 outcome of 2 reapers.
23. **Provider accepts and Wasla times out?** `UncertainDeliveryError`; row stays `PENDING`/`REQUESTED`; terminal; never resent.
24. **Can retry double-send?** Not from any tested provider-failure or crash path. Only through MSG-15 (client retry) or MSG-07 (malformed 200).
25. **Local commit fails after provider success?** Row stays `REQUESTED` — correct, discoverable via `ix_messages_unresolved_delivery`, but nothing surfaces it (MSG-11).
26. **Crash after persistence, before queue publish?** The job is published *before* commit, so this is inverted: a job can name an uncommitted conversation, covered by `FIRST_ATTEMPT_TRANSIENT`.
27. **Recovery path for persisted-but-unprocessed inbound?** **None** (MSG-02).
28. **Which provider errors retry?** `429` (×3) and `ConnectError` (×3) — the two that provably delivered nothing.
29. **Which never retry?** 4xx, 5xx, read timeouts. 5xx and timeouts are *unknowns*, not failures, and are terminal by construction.
30. **Are 429 and `Retry-After` handled?** 429 yes; `Retry-After` **no** (MSG-17).
31. **Is backoff bounded and jittered?** Job-level: bounded, exponential, jittered. Send-level: bounded, linear, **no jitter**.
32. **Can a poison message loop forever?** **Yes** — a payload PostgreSQL rejects retries for 7 days with no dead-letter (MSG-03).
33. **Does human takeover stop queued AI replies?** Yes for AI replies (two-layer recheck). **No for follow-ups** (MSG-05).
34. **Does the worker recheck mode at execution time?** Yes — at the top of the turn and again after the last inference, via a scalar read that bypasses the identity map.
35. **Can human and AI send simultaneously?** In a window of milliseconds, yes — irreducible without holding a lock across a Graph API call. Correct by design.
36. **Can handoff and AI-send race?** Yes, same window, same conclusion. A handoff ends the loop, so it cannot be caught half-committed.
37. **Are scheduled jobs revalidated?** Partly. Status, window, template and account: yes. **Mode and opt-out: no** (MSG-05, MSG-06).
38. **Is opt-out checked on every automated send?** No — campaigns yes, follow-ups no (MSG-06).
39. **Can a campaign send after cancellation?** Yes, for the batch already claimed — bounded by the per-minute rate (MSG-22).
40. **Is the recipient derived from trusted state?** Yes. `conversation.contact.wa_id`. Schemas `forbid` extra fields.
41. **Is the account derived from trusted state?** Yes. `conversation.account_id` → the tenant-scoped account → its `phone_number_id` and its credential.
42. **Can a request or LLM argument redirect a send?** No. No route, schema field or tool argument accepts a recipient, a number or a tenant.
43. **Is the 24-hour window handled locally or by Meta?** **Locally**, before any network call, using `conversations.last_inbound_at`. A never-contacted conversation has no window.
44. **Are templates bound to the correct account?** Yes. Campaigns and follow-ups check the registry; **the manual route does not** (MSG-10).
45. **Can a stale template cause retry storms?** Not a storm — attempts are bounded per recipient — but a paused template fails an entire audience one at a time, and sync is manual (MSG-24).
46. **Are unsupported types safely acknowledged?** Yes — all ten tested returned `200` and stored `unsupported`. But each also triggers a billed AI turn (MSG-19).
47. **Does media failure break the message record?** No. The message stands; media carries its own status; an unreadable file still produces a conversation line.
48. **Are media jobs deduplicated?** Yes — one job per file, and a duplicate webhook creates neither a media row nor a job.
49. **Are provider secrets absent from logs and errors?** Yes. Only Meta's `code`/`type`/`error_subcode` are logged; callers get a generic sentence; tokens are encrypted at rest and never returned.
50. **Are ids sufficient for end-to-end tracing?** Yes — `request_id`, tenant, conversation, local message id, `wa_message_id`, worker attempt, and a `queue.publish` span the attempt is a child of.
51. **Does a Redis outage lose inbound messages?** Not the messages — the **processing**, permanently (MSG-02).
52. **Does a PostgreSQL outage produce a retryable response?** Yes — `500`, nothing persisted, and redelivery processes exactly once. Verified.
53. **Can a queue outage leave durable messages unprocessed forever?** **Yes** (MSG-02).
54. **Are queue consumers idempotent?** Where they can be. The agent turn is *not*, which the design acknowledges by refusing to retry it once engaged rather than pretending otherwise.
55. **Can campaign volume starve manual replies or another tenant?** Architecturally possible — one Redis queue per job type, FIFO, no per-tenant fairness — but campaigns are database-rate-limited per campaign and bounded per batch, and manual replies are synchronous API calls that do not use the queue at all. A scalability item, not a correctness defect.
56. **Are multiple numbers per tenant routed correctly?** Yes. Two numbers, two conversations, each outbound on its own number.
57. **Is the same customer across numbers handled intentionally?** Yes — one contact, one conversation per number, matching the documented model.
58. **Is the same customer across tenants isolated?** Yes. Separate contacts, conversations and history.
59. **If chunking exists, can retry duplicate earlier chunks?** **No chunking exists.** One logical message is one provider message. But an over-long reply is refused outright (MSG-25).
60. **Can a message be delivered/read without a valid send mapping?** No. Statuses resolve by `wa_message_id` under `UNIQUE(tenant_id, wa_message_id)`; sweep `sent_message_without_provider_id = 0`.
61. **Are impossible transitions rejected?** Mostly. `read → sent` no-ops; `failed` overrides everything including `read`, and `failed` + `delivered_at` is reachable (MSG-14).
62. **Are invariants structurally enforced?** Yes — composite foreign keys make cross-tenant references impossible at the schema level, not merely unbuilt (ADR-100).
63. **Which guarantees have real concurrency tests?** Number ownership and queue commit visibility. **Not** inbound dedup or conversation creation (MSG-23).
64. **Which tests are false green?** None found. The suite avoids the classic traps deliberately and documents why.
65. **Which mutations survive?** **None.** 23 of 23 killed.
66. **Are survivors exploitable?** N/A — there are none. The findings are absent guards, which mutation testing cannot reach.
67. **What is unsupported today?** Interactive send (dead code), interactive-reply content, location content, reactions as first-class, message edit/delete/revoke, outbound chunking, template auto-sync, coexistence.
68. **What must change for coexistence?** An origin column, statuses for sends Wasla did not make, app-originated outbound in the window calculation, and not auto-answering an inbound message a human already handled in the app. Future work, not a current defect.
69. **What are the production blockers?** MSG-01, MSG-02, MSG-05.
70. **Would you trust Wasla today with real customer WhatsApp traffic?** **For duplicates, yes** — that is the property this system is built around and it holds under every failure tested. **For silent loss, not yet** — not because messages disappear, but because one can be stored and never answered with nothing to say so. Close the three blockers, add the messaging alerts, and the answer is yes.

---

## Appendix — Audit Artefacts

| Probe | Covers |
| --- | --- |
| `p01_trust_boundary.py` | Verification GET, six signature cases, persistence before/after, replay, duplicate-in-payload, four decode failures |
| `p02_concurrency.py` | Duplicate inbound ×2/×4/×8, concurrent first messages ×2/×4, duplicate status ×8 |
| `p03_outage.py` | Redis down, PostgreSQL down, recovery and redelivery |
| `p04_handover.py` | Release/reclaim: new inbound, late status, unknown status, late inbound, isolation sweep |
| `p05_outbound.py` | Nine provider behaviours, commit-before-call proof, service window, recipient binding |
| `p06_state_revalidation.py` | Follow-up revalidation matrix, human takeover during a send |
| `p07_states_and_payloads.py` | Nine status sequences, five malformed timestamps, ten message types, partial batch, multi-number, cross-tenant, eleven Unicode cases |
| `p08_queue_and_workers.py` | Two-worker claim, crash before/after engagement, lease renewal, two reapers, attempt budget |
| `p09_idempotency_and_invariants.py` | Double-submit, sequential retry, 18-query invariant sweep |
| `p10_reply_length.py` | Over-long agent reply |
| `mutate.py`, `mutate2.py` | 23 mutations with byte-exact restore and git verification |

All artefacts live in the session scratchpad. None was committed. Working tree verified identical to `a486321` plus this report.

**Quality gates at the close of the audit:**

| Gate | Result |
| --- | --- |
| `ruff check .` | All checks passed |
| `black --check .` | 541 files unchanged |
| `mypy app` | no issues in 246 source files |
| `pytest tests` (full suite) | **4,215 collected, 0 failed, exit 0** |
| `pytest -k <messaging>` | **1,426 collected, 0 failed, exit 0** |
| `alembic check` | no new upgrade operations detected |
| `alembic heads` | `0053` |

### A contamination incident in this audit's own harness, and what it cost

The first full-suite run exited `1` with 14 failures: `test_fixture_isolation::test_the_database_starts_empty` (`assert 1 == 0`), then every later global-count assertion in `test_platform_analytics` and `test_usage_metering`.

**The cause was this audit, not the product.** `p10_reply_length.py` creates and commits a tenant, and it was run against `wasla_msgaudit` **while the full suite was executing on that same database**. The suite's `conftest` does `DROP SCHEMA public CASCADE` at session start, so every probe that ran *before* it was correctly wiped; this one row landed mid-session and poisoned every subsequent global count.

Established rather than assumed:

* `test_fixture_isolation.py` + `test_usage_metering.py` in isolation — **21 passed**.
* `test_commit_boundary.py` → `test_fixture_isolation.py` in that order, testing the alternative hypothesis that a deliberately-committing messaging test leaks — **13 passed**. Hypothesis refuted and discarded.
* The failure was exactly one surplus `Tenant`, which is exactly what `p10` commits.
* A re-run with no concurrent probe — **exit 0, zero failures**.

Recorded here because it is a methodological finding about the audit and because the intermediate result would otherwise stand unexplained in the artefacts: **a probe harness sharing a database with a pytest session is a contamination channel.** The probes should have used a second database. Every other result in this report — the 1,426-test baseline, all 23 mutations, and each of the ten probes — ran sequentially with nothing else against the database, verified by checking each background task had completed before the next began. None of the findings depends on the contaminated run.
