# Wasla — CRM / Human Handoff Independent Audit

```text
Audit id        crm-c4h7
Frozen HEAD     4949135eb698b4143e1d7e66654bb5c1df78e260  (docs(media): close release recovery residual)
Branch          worktree-billing-google-auth
Worktree        E:\wasla-crm-handoff-audit (detached, clean before and after)
Mutation lane   E:\wasla-crm-mutation (second detached worktree, same HEAD, clean after)
Alembic head    0067
Evidence        E:\wasla-crm-c4h7\{infra,probes,mutations,logs}
Type            AUDIT ONLY - no production code, test, migration or config changed
```

---

## 1. Executive Summary

The part of this subsystem that protects **customers** is sound. A colleague who takes a conversation over stops the AI: a takeover committed while the model is composing produces **zero** AI sends (P2b), the sentiment escalation and the handoff tool also send nothing, and a follow-up never fires under a human-owned conversation. Every one of 22 cross-tenant operations against another workspace's conversation, lead, note, follow-up and contact answered 404 with no canary leaked, and the listing, count and cursor paths leaked nothing. Suspended workspaces get 403 on every CRM write. No CRM personal data — emails, names, notes, handoff reasons — reached a log line.

The part that protects **the CRM record and the colleagues working it** is not. There is not one lock, version check or state precondition on any human CRM write path (`pg_locks`: 0 row locks while two takeovers were in flight). Almost every finding here comes from that one fact, surfacing in a different place each time:

* **One cross-tenant write exists (CRM-01, HIGH).** `POST /follow-ups` stores another workspace's `lead_id` verbatim, answers 201, and answers 409 for an id that does not exist. That is a cross-tenant reference plus an existence oracle. The database cannot stop it (`follow_ups.lead_id` is a plain FK). It also falsifies the premise of ADR-100 ("nothing in the application builds such a row").
* **Automation overwrites a colleague's handoff note (CRM-02).** A takeover committed while the sentiment classifier is reading the message ends with the colleague's reason replaced by `"Escalated automatically: …"` and a second handoff counted. The handoff tool racing a takeover overwrites the reason too, files an `agent_handoff_requested` audit row, and records the turn as `handed_off`. That is the exact TOOL-07 outcome, reopened by a narrower window.
* **Human-verified lead data is not protected under concurrency (CRM-06).** An AI extraction overwrote a human email correction committed mid-tool, even though `human_verified_fields` lists `email`. Two colleagues editing different fields at once lose one field's verification, and the next extraction then overwrote that field.
* **The lead pipeline can leave its terminal state (CRM-07).** Two concurrent status moves turned a `WON` lead back into `PROPOSAL` with `closed_at` still set.
* **Ownership is last-writer-wins with no trail (CRM-04, CRM-05, CRM-11).** Two colleagues self-assigning are both told they own the conversation. A stale "unassign me" erases a newer reassignment. A removed member stays the owner of their conversations and leads, and an assignment can race a removal and land on a revoked member. Takeover, release, assign, unassign, close and reopen write **no audit row at all**.
* **Follow-ups disagree with themselves (CRM-08/09/10).** A colleague's own follow-up on a conversation they own is accepted with 201 and always `SKIPPED` at dispatch. A reschedule landing inside the sweep's lease is ignored, and the old nudge goes out immediately with the new text. A cancel racing a dispatch reports `cancelled`, and the row ends `CANCELLED` although the customer received the message.

```text
Findings   CRITICAL 0 · HIGH 1 · MEDIUM 10 · LOW 5 · INFO 4 · TEST GAPS 5
Score      5.84 / 10
Verdict    NOT CLOSED - 1 blocker, 10 medium remediations, 10 product decisions
```

---

## 2. Frozen Repository State

```text
$ git branch --show-current           worktree-billing-google-auth
$ git rev-parse HEAD                  4949135eb698b4143e1d7e66654bb5c1df78e260
$ git status --short                  the nine pre-existing untracked reports (AI_FINDINGS_REMEDIATION.md …
                                      WORKERS_QUEUES_FINDINGS_REMEDIATION.md) + two unreadable .tmp_pytest dirs.
                                      None touched.
$ git log --oneline -3                4949135 docs(media) · a46a9aa fix(media) · 6799f45 Media merge
$ alembic heads                       0067 (head)
```

The branch had not moved: HEAD is the expected `4949135`, and `6799f45` and `a46a9aa` are its ancestors. The pre-existing worktrees (including two prunable ones) were left alone. Two detached worktrees were added at the frozen HEAD: `E:\wasla-crm-handoff-audit` for the audit and `E:\wasla-crm-mutation` for mutations. Both were `git status --short`-clean after the audit. **This report is the only file this audit adds to `E:\wasla`.**

Source did not change during the audit, so every count below comes from one source state.

---

## 3. Isolation / Test Hygiene

```text
Docker project          wasla-crm-audit-c4h7  (own network, volumes, loopback ports 57141-57143)
PostgreSQL              16.15, system_identifier 7687082338105372717
                        dbs: wasla_models, wasla_migrations, wasla_dev, wasla_alembic, wasla_invariants, wasla_mut
Redis (lane 1)          run_id 3c385164b1c9656c106f6e774abd8f4e75ce5d45  (runner joins its netns)
Redis (lane 2)          run_id b5385fe1e638e02f8eab5a31d1eae23da783dbe8  (mutation runner joins its netns)
Object store            MinIO in-project, bucket wasla-media (baseline suites only; no CRM path touches media)
Runner image            wasla-crm-runner:c4h7 (re-tag of wasla-media-runner:e5f1; pyproject unchanged since)
app imported from       /work/app/__init__.py (the audit worktree)
```

* The developer stack (`wasla-api-1`, `wasla-postgres-1`, `wasla-redis-1`) and other sessions' stacks (`wasla-media-rem-m7c2`, `wasla-media-audit-e5f1`, `wasla-tools-rem-4b2a`) were never stopped, flushed, written or queried. The one exception is a read-only `INFO server` against `wasla-redis-1`, used to record that its run_id (`598df7c4…`) differs from ours.
* Databases were partitioned by purpose. Baseline gates used `wasla_models` and `wasla_migrations`. Probes committed real rows into the migration-built `wasla_invariants`. The targeted suite ran on `wasla_dev` and `wasla_migrations` after the baseline finished. Mutations ran on `wasla_mut` in lane 2, which has its own Redis and its own worktree, so the authoritative baseline never shared a Redis database or a file tree with a mutated one.
* Probes and the baseline overlapped in time. The probes used Redis DB 7 only, and the suites use DBs 2, 9 and 11–15 (`grep` of every hard-coded URL). No `FLUSHALL` or `FLUSHDB` was run by the audit. No `git reset`, `clean` or `stash`.

**No run in this report is classified CONTAMINATED.**

---

## 4. Baseline Gates (frozen HEAD, unmodified)

| Gate | Result |
|---|---|
| `ruff check app tests` | All checks passed |
| `black --check app tests` | 580 files unchanged |
| `mypy app tests` | Success: no issues in 580 source files |
| `alembic heads` / `upgrade head` / `check` | `0067 (head)` / to 0067 / "No new upgrade operations detected" |
| Model-built whole `tests/` | **5081 passed, 17 skipped, 0 failed** (1077 s) |
| Migration-built `tests/integration` + `tests/e2e` | **2519 passed, 2 skipped, 0 failed** (1057 s) |

### CRM/Handoff targeted suite

35 files: 27 integration and 8 unit (`infra/crm_suite.txt`). Conversations, contacts, projection, leads (CRM and endpoints), follow-ups (endpoints, revalidation, worker, service), membership revocation, sentiment escalation and concurrency, AI lifecycle and empty response, tool authority/concurrency/semantics/deferred effects/invariants, tenant isolation, relational integrity, analytics events, opt-out, inbound concurrency, pagination, route authorization, workspace lifecycle; unit lead/follow-up models, lead validation, projection, sentiment service, registry, escalation config, opt-out.

| Strategy | Files | Passed | Skipped | Failed |
|---|---|---|---|---|
| Model-built | 35 | **548** | 1 | 0 |
| Migration-built (integration only) | 27 | **353** | 1 | 0 |

The skip is `test_tool_invariants.py:192`, which runs only when a prior run kept the tool suites' data. That is by design.

---

## 5. CRM / Handoff Inventory

| Entity | Table/model | API | Service | Repository | Worker/tool path | Tenant key | Ownership key | State machine | Actors | Audited? | Customer-visible? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Contact | `contacts` | `POST/DELETE /contacts/{id}/opt-out` only | `CampaignService`, inbound `ConversationProjectionService` | `ContactRepository` | webhook ingestion | `tenant_id`, UNIQUE(tenant, wa_id) | — | opt-out timestamp | customer, member, admin | no | no |
| Conversation | `conversations` | `GET list/one/messages`, `POST mode/assignment/priority/close/reopen/messages*` | `InboxService`, `SentimentService`, `MessagingService` | `ConversationRepository` | AI worker, handoff tool, sentiment, empty-response, quota-blocked | `tenant_id` + composite FKs to contact and account | `assigned_to_id` → `users.id` (**not** membership) | status, mode | member, agent, sentiment, system | **no** (analytics only for mode) | mode stops AI |
| Handoff | `conversations.mode/handoff_reason` + `analytics_events(handoff, handoff_resumed)` + `audit_logs(agent_handoff_requested)` | `POST /conversations/{id}/mode` | `InboxService.set_mode`, `SentimentService._persist` | — | `request_human_handoff`, sentiment, `_answer_emptiness`, `_quota_blocked` | tenant | analytics `actor_id` | AI↔HUMAN | user, agent, sentiment, system | agent path only | yes (AI stops) |
| Lead | `leads` | list, stats (admin), create, get, PATCH, status, assignment (admin), score | `LeadService` | `LeadRepository` | `record_lead_details` | `tenant_id`, partial UNIQUE(tenant, contact) open | `assigned_to_id` → `users.id` | lead status graph | member, admin, agent | `lead_activities` (+ `agent_lead_recorded`) | no |
| Note | `lead_notes` | GET/POST `/leads/{id}/notes` | `LeadService.add_note` | `LeadNoteRepository` | — | `tenant_id` (FK lead plain) | `author_id`/`author_kind` | append-only (no edit/delete route) | member, agent | `note_added` activity | no |
| Tags / custom fields | `leads.tags` ARRAY, `leads.custom_fields` JSONB | PATCH lead | `LeadService` | — | not agent-writable | row | — | — | member | `fields_updated` | no |
| Follow-up | `follow_ups` | list, create/reschedule, get, cancel | `FollowUpService` | `FollowUpRepository`, `DueFollowUpClaim` | `FollowUpWorker` (poll + lease), `schedule_follow_up` tool, inbound cancel, lifecycle cancel | `tenant_id` (FKs conversation, lead **plain**) | `created_by_id/kind` | PENDING→SENT/CANCELLED/SKIPPED/FAILED | member, agent, customer (cancel), system | `agent_follow_up_scheduled` only | yes |
| Priority/sentiment | `conversations.priority/sentiment`, `message_sentiments` | `POST /priority` | `SentimentService` | `SentimentRepository` | AI worker | tenant | — | raise-only automation | sentiment, member | no | no |
| Membership | `memberships` | `/workspace/members` list/remove/reinstate | `MembershipService` | `MembershipRepository` | — | tenant | — | ACTIVE/REVOKED | owner, admin, self | yes | no |

**Absent, verified at this HEAD, and marked not applicable:** contact read, edit, search, merge or delete APIs; conversation, message or lead deletion; note edit or delete; a tag entity; bulk endpoints; notifications, push, websocket or SSE; Redis anywhere in a CRM decision; an assignment history table; a CRM reconciler.

---

## 6. Authoritative State Model

| Fact | Authority | Precedence / notes |
|---|---|---|
| Customer identity | `contacts(tenant_id, wa_id)`, unique | `wa_id` exactly as Meta sends it; no normalisation (CRM-20) |
| AI vs HUMAN control | `conversations.mode` | The only authority. Re-read as a column by the orchestrator (`_taken_over`), `refusal_now`, tool executor, follow-up dispatch. No cache. |
| Assigned colleague | `conversations.assigned_to_id` | Independent of `mode`. A takeover does **not** assign the taker. Analytics `actor_id` is the only record of who took over. |
| Handoff reason | `conversations.handoff_reason` | Whoever writes last. Cleared on release. Copied (truncated) into `analytics_events.metadata.reason` per event. |
| Conversation status | `conversations.status` | OPEN/CLOSED in practice. `PENDING` exists in the enum and **nothing sets it**. Inbound reopens CLOSED. |
| Lead fields | `leads.*` | Human edit "sticky" via `leads.human_verified_fields` (ADR-021). Enforced only against a fresh read; see CRM-06. |
| Lead owner | `leads.assigned_to_id` | Admin-only writes |
| Follow-up owner | `follow_ups.created_by_id/kind` | Attribution only. Execution is the workspace's, regardless of creator. |
| Follow-up status | `follow_ups.status` + lease in `scheduled_at` | PENDING row with a pushed `scheduled_at` = leased |
| Priority | `conversations.priority` | Automation raises, only a person lowers |
| Tags / notes / custom | `leads.tags`, `lead_notes`, `leads.custom_fields` | Human only |

Redis holds no CRM state. The AI job queue carries ids only, and every decision re-reads PostgreSQL. **PostgreSQL is the single authority for every CRM fact.**

---

## 7. State Machines (as implemented)

**Conversation mode:** `AI ⇄ HUMAN`. Any member may take over or release. Taking over from AI cancels every pending follow-up and records `handoff(source)`. Releasing clears the reason and records `handoff_resumed(user)`. Setting HUMAN on HUMAN records nothing and **rewrites the reason, including to null** (CRM-03). There is no forbidden transition, no precondition and no lock.

**Conversation status:** `OPEN → CLOSED` (any member), `CLOSED → OPEN` (any member, or any inbound message). Idempotent. Close does not touch mode, assignment or follow-ups; dispatch skips closed. `PENDING` is unreachable (INFO).

**Lead status:** the graph in `app/db/models/lead.py`. `WON` is terminal; `LOST → NEW` only. A no-op is allowed. Forbidden moves give 422. The graph is checked against the row **as read**, not as committed (CRM-07). Reopening LOST while the customer has a newer open lead violates `uq_leads_active_contact` and gives 500 (CRM-13).

**Follow-up:** `PENDING → SENT | CANCELLED | SKIPPED | FAILED`. `PENDING` stays PENDING through `_fail` backoff until attempts run out. Reschedule rewrites a PENDING row in place. Cancel of a non-PENDING row is a no-op. Dispatch skips when unserved, closed, human, opted out or out of window without a template. The claim is `FOR UPDATE SKIP LOCKED` + a 300 s lease, then a per-row locked re-take. The API's cancel and reschedule take **no lock and no status precondition** (CRM-09, CRM-10).

**Membership:** `ACTIVE ⇄ REVOKED` under the tenant lock. Revocation touches nothing else (CRM-11).

---

## 8. Tenant Isolation

P1 used two workspaces with **the same customer phone** (`201099990000`) and presence first: the victim's owner read all four victim resources successfully. The attacker then held an owner token in workspace A.

```text
P1.presence: victim_rows_visible_to_owner=4
P1.attacks: count=22 statuses=[404] leaks=0
P1.listing: filters=7 foreign_rows=0
P1.victim_state: conversation_unchanged lead_unchanged notes=1 follow_up=pending opt_out=None
                 same_phone_contacts=2 distinct_tenants=2
P6.counts: a_total=1 b_total=4 cursor_replay_status=200 cursor_replay_foreign_rows=0
```

The 22 operations were read, mode, assign, priority and close on a conversation; read, PATCH, status, assign, score, notes and activity on a lead; read and cancel on a follow-up; create a follow-up on a foreign conversation; opt-out on a foreign contact; create a lead from a foreign contact or conversation; and assign own rows to a foreign member. **All answered 404 and nothing changed.** Listing and filtering by foreign `contact_id`, `conversation_id`, `assigned_to_id` and `lead_id` returned only own rows, as did search. Statistics were scoped, and a cursor minted in B and replayed in A returned no foreign rows. Mutations M01–M05 and M23 (tenant predicates and membership checks) were all killed.

**One path is not scoped:** `follow_ups.lead_id` (CRM-01).

```text
P1.follow_up_foreign_lead: foreign_lead_status=201 response_lead_id_is_victims=True
                           nonexistent_lead_status=409 ("A follow-up for this conversation could not be resolved.")
                           cross_tenant_rows=1
Invariant I05 follow_up.tenant != lead.tenant = 1
```

---

## 9. Contact Identity

* Identity is `UNIQUE(tenant_id, wa_id)`. The same phone in two workspaces yields two contacts (P1). Nothing merges across tenants.
* The contact creation race converges through a savepoint (MSG-09). Mutation M24, which removes that savepoint, is killed by `test_whatsapp_inbound_concurrency.py::test_distinct_first_messages_from_one_customer_build_one_conversation[2,4]`.
* Formatting variants (`+20…` vs `20…`) would be two contacts. Only the webhook creates contacts, and Meta sends the canonical digits-only `wa_id`, so this is INFO (CRM-20), not a defect.
* No contact merge exists; not applicable.

---

## 10. Conversation Ownership and Assignment Authorization

Roles as implemented (`P3g.role_matrix`), matching the documented line: *any member may triage conversations; lead assignment and statistics are administrative.*

| Action | MEMBER | TENANT_ADMIN | DB mutation | Audit row |
|---|---|---|---|---|
| Assign a conversation to anyone in the workspace | 200 | 200 | yes | **none** |
| Reassign a colleague's conversation | 200 | 200 | yes | **none** |
| Take over / release to AI | 200 | 200 | yes | **none** (analytics event only) |
| Close / reopen / set priority | 200 | 200 | yes | **none** |
| Lead assignment | **403** | 200 | admin only | `lead_activities` |
| Lead statistics | **403** | 200 | — | — |
| Lead note | 201 | 201 | yes | `note_added` activity |
| Cancel follow-up | 200 | 200 | yes | **none** |
| Any CRM write after removal (stale token) | 404 | — | no | — |
| Any CRM route, workspace suspended | 403 (read too) | 403 | no | — |

Assignment verifies an **active membership** in this workspace (M04 and M05 killed). The database does not: `conversations.assigned_to_id` and `leads.assigned_to_id` reference `users.id` only.

---

## 11. Assignment Concurrency and Stale Clients

Writes carry no version, no expected owner, no `If-Match` and no lock. `ConversationRead` exposes no version or `updated_at`.

```text
P3c.two_self_assigns: alice_told_she_owns=True bob_told_he_owns=True final_owner=bob assignment_trail_rows=0
P3c.stale_unassign:   alice_saw_owner=alice read_carries_version_field=False stale_status=200 after_stale=None
                      member_reassign_status=200
P3d.removal_races_assignment: bob_membership=revoked conversation_assigned_to_revoked=True
```

These are two real sessions on two pooled connections. Each ran the real `InboxService.assign`, then the commits were ordered. P3a separately confirmed `row_locks_held_before_commit=0`. There is one owner at a time, so the row is never contradictory. But the loser is told it won, a stale "unassign me" erases a newer reassignment, and nothing records what happened (CRM-04, CRM-05, CRM-11).

---

## 12. Human Takeover (human ownership must win)

This is audited with the real `AgentWorker`, real PostgreSQL and real Redis. Transports are faked (ai_harness), and every human action is the real service in its own committed session.

```text
P2b.takeover_during_inference: inference=1 sends=0 mode=human reason_is_colleagues=True
                               handoff_events=['user'] outcomes=['suppressed_human']
P2a.sentiment_vs_takeover:     sentiment_calls=1 inference=0 sends=0 mode=human
                               reason_is_colleagues=False
                               reason='Escalated automatically: the customer sounds angry about refund.'
                               handoff_events=['user','sentiment'] outcomes=['escalated']
P2c.tool_handoff_vs_takeover:  gate_fired=True sends=0 mode=human reason_is_colleagues=False
                               reason='Customer asked for a manager' handoff_events=['user']
                               agent_handoff_audits=1 outcomes=['handed_off']
P4a follow-up under a human-owned conversation: sends=0
```

**The customer-visible guarantee holds.** Once a takeover commits, no stale AI path sent anything. The guarantee is held by two redundant layers, the orchestrator's `_taken_over` and the worker's `refusal_now`. Each survives on its own (M21, M22), and the pair is killed by `test_a_conversation_taken_over_during_inference_sends_no_reply`.

**The ownership guarantee does not fully hold.** Mode stays HUMAN, but stale automation rewrites the colleague's reason, adds a second handoff event, and records turn outcomes that claim automation did the handoff (CRM-02).

The residual race at the Meta socket, where a takeover lands between `refusal_now` and the HTTP send, was accepted in the AI remediation and the Messaging audit. It is not reopened here (CRM-17, INFO).

---

## 13. AI ↔ Human Race Matrix

| Race | First commit | Last commit | Final DB truth | Customer-visible | Audit / analytics | Verdict |
|---|---|---|---|---|---|---|
| Inference vs takeover (P2b) | human | turn (suppressed) | HUMAN, colleague reason | 0 sends | 1 `handoff(user)` | ✔ |
| Sentiment read vs takeover (P2a) | human | sentiment | HUMAN, **automatic reason** | 0 | `user` + `sentiment` handoffs | ✘ CRM-02 |
| Handoff tool vs takeover (P2c) | human | tool | HUMAN, **AI reason** | 0 | `agent_handoff_requested`, outcome `handed_off` | ✘ CRM-02 |
| Lead tool vs human correction (P2d) | human | tool | **AI email**, `human_verified=['email']` | — | timeline lists the human edit last | ✘ CRM-06 |
| AI follow-up vs takeover | — | — | cancelled at takeover; dispatch skips | 0 | — | ✔ (closed in Tools; M07/M11/M12 killed) |
| Empty-response handoff vs takeover | human | worker | reason overwritten (code path identical to P2c: unconditional `set_mode` reason write) | fallback may send (accepted residual) | `handoff(system)` if stale | ✘ CRM-02 (code reading) |

---

## 14. Human/Human Race Matrix

| Race | Result | Finding |
|---|---|---|
| Two takeovers (P3a) | both commit; reason = last; `handoff_events=['user','user']`; 0 locks; 0 audit | CRM-03 |
| Re-take with empty reason (P3b) | 200; reason `None`; no event | CRM-03 |
| Two self-assigns (P3c) | both told "you own it"; last wins | CRM-04 |
| Stale unassign (P3c) | newer reassignment erased | CRM-04 |
| Assign vs member removal (P3d) | assigned to revoked member | CRM-11 |
| Two lead edits (P3e) | `human_verified_fields` loses `name`; later extraction overwrote the confirmed name | CRM-06 |
| WON vs PROPOSAL (P3f) | `final_status=proposal closed_at_set=True timeline=[(new,qualified),(qualified,won),(qualified,proposal)]` | CRM-07 |
| Reschedule vs dispatch (P4c) | sent at old time with new text | CRM-09 |
| Cancel vs dispatch (P4d) | API says cancelled; row `CANCELLED` + `sent_at` + `message_id`; customer received 1 | CRM-10 |

---

## 15. Handoff Idempotency

| Path | Repeat / replay | Concurrent |
|---|---|---|
| `POST /mode` same body | no second event ✔ (M06 killed) | two events (P3a) ✘ |
| `POST /mode` HUMAN again, other/empty reason | reason silently rewritten or cleared, no event | — |
| Handoff tool replay / already HUMAN | refused, no audit, no event ✔ (Tools; M10 killed) | audit + reason overwrite (P2c) ✘ |
| Sentiment replay | stored reading reused, no second escalation ✔ | escalation after a takeover (P2a) ✘ |
| Notification | none exists | — |

Semantics as built: **at-least-once, idempotent sequentially, last-writer-wins concurrently**. No guard exists at the storage level (a conditional `UPDATE … WHERE mode='ai'`).

---

## 16. Handoff Reason Authority

Nothing decides who may write the reason. Five writers exist: the API, the handoff tool, sentiment, empty-response and quota-blocked. Each writes unconditionally once it believes it is making the transition, and the API writes it even when no transition happens. Proven outcomes:

* **AI-generated reason overwriting a human's:** P2a and P2c.
* **Older request overwriting a newer one:** P3a, where the commit order decided.
* **Empty reason clearing a meaningful one:** P3b.
* Release clears the reason by design. That behavior has no test (M08 survived).

---

## 17. Lead / CRM Field Updates

The rules exist and are tested in sequence. A human edit is sticky, `M16` is killed, and extraction is limited to six fields (`M17` survives only because `ExtractedLead` carries no other field). Judgement fields are never agent-writable, the no-op write rule holds (M18 killed), and agent edits are attributed to the agent (M20 killed).

Under concurrency the rules do not hold:

```text
P2d.extraction_vs_human_correction: final_email=old.address@example.com human_verified=['email']
    human_value_survived=False
    activities=[('agent','Lead created…'),('agent','Agent updated email.'),('user','Updated email.')]
P3e.lead_verified_lost_update: verified_after_two_edits=['email'] name_after_ai='A. H.'
    human_name_overwritten_by_ai=True
```

The ORM emits `UPDATE … SET <changed columns>` computed against the row as read. `human_verified_fields` is an array rewritten whole, so two edits race on it.

Partial update semantics (omitted vs explicit null) work as documented. Blank strings give 422. Null clears the field and marks it verified.

---

## 18. Notes / Tags / Metadata

* **Notes:** append-only. No edit or delete route exists (P5 OpenAPI scan: no PUT, PATCH or DELETE on notes, activity or audit). They are tenant-scoped (P1), and attribution is `author_id` + `author_kind`. The service strips and bounds notes to 5000 characters: 5000 Arabic characters → 201, 5001 → 422. A NUL character → **500** (CRM-12). They are rendered only as JSON.
* **Tags:** a per-lead `ARRAY(String(50))`, trimmed, lowercased and de-duplicated, at most 25. There is no tag entity, so there are no cross-tenant tag ids and no tag deletion. The tag filter is containment within the tenant. Tag concurrency is last-writer-wins on the whole array, the same RMW as CRM-06.
* **Custom fields:** a JSONB value bounded by `check_json`. A NUL inside a value → 500 (CRM-12).

---

## 19. Follow-Up Ownership

* **Execution belongs to the workspace, not the creator.** A removed member's nudge still goes out (`P4e.removed_members_nudge: final=sent`). That is a product decision (PD-CRM-8).
* **A human-created follow-up on a human-owned conversation is accepted and never sent** (`P4a: schedule=201 created_by_kind=user final=skipped detail='A colleague has taken this conversation over.'`). `schedule()` explicitly allows this as "the ordinary way to use the feature", `dispatch()` skips every follow-up on a HUMAN conversation, and `test_a_follow_up_on_a_human_owned_conversation_sends_nothing` pins the skip for a USER-created row. CRM-08.
* **A takeover cancels a colleague's own earlier nudge** (`P4b: created_by_kind=user final=cancelled`). Suspension, by contrast, deliberately preserves colleague nudges (`cancel_agent_follow_ups`).
* **Suspension:** a colleague's nudge that falls due is terminally `SKIPPED` (`P4e`), per PD-TOOLS-06. That is consistent with the documented decision.

---

## 20. Follow-Up Races

| Race | Outcome | Verdict |
|---|---|---|
| Cancel during lease, before re-take | re-take sees CANCELLED, nothing sent | ✔ (M28 killed) |
| Cancel concurrent with dispatch (P4d) | final `CANCELLED`, message sent, `sent_at` set | ✘ CRM-10 |
| Reschedule during lease (P4c) | sent **now**, with the rescheduled body | ✘ CRM-09 |
| Takeover vs AI nudge | cancelled + dispatch guard | ✔ (M07, M11 killed) |
| Suspension vs dispatch | skipped | ✔ (M14 killed) |
| Member removal vs personal nudge | sent | product decision |
| Duplicate scheduling | reschedules the one pending row (partial unique index) | ✔ (M25 killed; I17 = 0) |
| Inbound reply vs pending | cancelled on the webhook path | ✔ (M29 killed) |

The window for CRM-09 is from `claim_due` commit to that row's `claim_by_id`. With `DEFAULT_CLAIM_LIMIT = 20` rows dispatched serially, each a Meta round trip with retries, it runs from seconds up to the 300 s lease.

---

## 21. Member Removal / Suspension

```text
P3d.removal_sequential: removal_status=200 conversation_still_assigned_to_removed=True
                        lead_still_assigned_to_removed=True removed_members_follow_up=pending
                        removed_member_write_status=404
Invariant I11 conversation assigned to revoked member = 2 · I12 lead assigned to revoked/non member = 1
```

The stale session is refused on the next request (ADR-038, re-proved at the CRM boundary). Nothing transfers or clears the removed member's ownership, and the database permits it because the FK is to `users`. Whether removal should unassign or transfer is a product decision (PD-CRM-2). The race that assigns to an already-revoked member is a defect either way (CRM-11). Member suspension is not a separate state: `MembershipStatus` is ACTIVE or REVOKED only. A user-level disable is the Auth subsystem's and is closed.

---

## 22. Workspace Lifecycle

* **Suspended:** every CRM route answers 403, reads included (`P3g.suspended_workspace`). Automation stops at the turn gate, the tool gate and the dispatch gate (closed in AI and Tools; M14 killed).
* **Soft-deleted / purged:** already closed in Authorization and Media (purge cascade). No CRM-specific path differs.
* **Inbound traffic to a suspended workspace** still projects contacts and messages. That is Messaging's closed contract. The CRM effect is that the backlog is visible on restore.

---

## 23. Audit / Attribution

| Operation | `audit_logs` | Other trail |
|---|---|---|
| Human takeover | **none** | `analytics_events(handoff, user, actor_id, reason)` |
| Release to AI | **none** | `analytics_events(handoff_resumed, actor_id)` |
| Assign / reassign / unassign | **none** | **none** |
| Close / reopen / priority | **none** | none |
| Follow-up create / cancel by a person | **none** | row columns only (`created_by_*`, `cancelled_reason`) |
| Agent handoff / lead / follow-up | `agent_*` actions | tool executions |
| Lead changes | — | `lead_activities` (append-only, actor + kind + before/after) |

`I16`: of 12 human takeovers in the population, the only audit rows naming those conversations were the 2 written by the agent path.

`analytics_events` is not an audit log. It is a metrics table, grouped and counted, and it cannot answer "who assigned this to whom, and what was it before". Attribution on the rows that exist is correct: USER, AGENT, SENTIMENT and SYSTEM are distinct, and M20 was killed.

History integrity: no route edits or deletes activity, notes or audit rows (P5). There is no cryptographic immutability, and none is claimed.

---

## 24. Notifications / Realtime

None exists. There is no push, websocket, SSE or Redis pub/sub in any CRM path. The durable handoff obligation is `conversations.mode = 'human'` in PostgreSQL, rediscoverable by listing. The inbox list has no mode, assignee or unassigned filter, though. `I14`: 12 human-owned conversations had no assignee in the population, and nothing but a full scan finds them (CRM-18, INFO / future feature).

---

## 25. Crash / Recovery Semantics

| Flow | Transaction shape | Crash window | Durable? |
|---|---|---|---|
| Human takeover | one tx: mode + reason + follow-up cancellation + analytics | none; all or nothing | ✔ |
| Assign / reassign | one UPDATE | none | ✔ (no trail to lose) |
| Follow-up create / reschedule | one tx (savepoint for the insert race) | none | ✔ |
| Follow-up cancel | one tx | none (but CRM-10 is a race, not a crash) | ✔ |
| Agent handoff | inside the turn tx; committed before the worker completes the turn | turn dies after commit → state durable, the turn is recovered by the AI worker (closed) | ✔ |
| Sentiment escalation | savepoint inside the turn | same | ✔ |

No CRM write holds a transaction across Redis, Meta, OpenAI or any notifier. Dispatch releases its lock before Meta (ADR-093). There is no CRM dead letter: follow-ups are polled, and an unexpected dispatch exception rolls back and is re-claimed after the lease. No stranded handoff or follow-up was producible, so no reconciler is required.

---

## 26. Database Constraints

| Invariant | DB-enforced? |
|---|---|
| conversation ↔ contact / account same tenant | ✔ composite FKs (ADR-100) |
| one conversation per (tenant, contact, account) | ✔ |
| one contact per (tenant, wa_id) | ✔ |
| one open lead per contact | ✔ partial unique |
| one pending follow-up per conversation | ✔ partial unique |
| **follow_up.lead same tenant** | ✘ plain FK, and reachable from the API (CRM-01) |
| lead.contact / lead.conversation same tenant | ✘ plain FK; the application checks it (M23 killed) |
| lead.contact == lead.conversation.contact | ✘ neither DB nor app (CRM-14; I08 = 1, I09 = 2) |
| assignee is an active member of the workspace | ✘ FK to users only; app check racy (CRM-11) |
| lead_notes / lead_activities tenant = lead tenant | ✘ plain FK; only internal writers (I06 = 0) |
| follow_up.conversation same tenant | ✘ plain FK; app checks it (I04 = 0) |

---

## 27. Logging / Privacy

Sentinel emails (`corrected@example.com`, `ahmed@corp.example`, `old.address@example.com`), handoff reasons ("VIP - call personally", "threatened legal action"), follow-up bodies and note text were written through the probes with `log_format=json, level=INFO`. **None appeared in any application log line.** The only matches were the probes' own `PROBE` prints, plus the sentiment `intent` label (`"refund"`), which the Sentiment design logs deliberately and which is model-generated classification text, not customer text.

Handoff reasons are stored in `analytics_events.metadata.reason`. That is a database copy of an internal note, not a log.

---

## 28. Runtime Probe Matrix

| # | Probe | Setup | Race / failure | Final DB | Customer-visible | Audit / analytics | Recovery |
|---|---|---|---|---|---|---|---|
| P1 | two tenants, same phone; 22 cross-tenant ops | real app + JWT | — | victim unchanged | 0 | 0 | n/a |
| P1f | foreign `lead_id` on follow-up | tenant A member | — | cross-tenant row stored | 0 | — | none |
| P2a | takeover during sentiment | real worker, gated classifier | human first | reason overwritten | 0 | 2 handoffs | none |
| P2b | takeover during inference | real worker | human first | HUMAN, colleague reason | **0** | 1 handoff | n/a |
| P2c | tool handoff vs takeover | gate between check and write | human first | reason overwritten | 0 | agent audit, `handed_off` | none |
| P2d | lead tool vs correction | gate between read and write | human first | AI value wins | — | timeline misordered | none |
| P3a | two takeovers | two sessions | ordered commits | last reason | — | 2 handoffs, 0 audit | none |
| P3b | re-take, empty reason | HTTP | — | reason `None` | — | none | none |
| P3c | self-assign ×2; stale unassign | sessions / HTTP | — | last writer; newer erased | — | 0 rows | none |
| P3d | removal vs assignment | sessions | revoke commits first | assigned to revoked | — | removal audited only | none |
| P3e | two lead edits, then AI | sessions | — | verified lost; AI overwrote | — | activities | none |
| P3f | WON vs PROPOSAL | sessions | — | PROPOSAL + closed_at | — | 2 exits from `qualified` | none |
| P3g | roles; suspension | HTTP | — | as documented | — | — | — |
| P3h | text bounds | HTTP | NUL, exact-max Arabic/emoji, max+1 | 9 paths 500 on NUL; bounds OK | — | — | rolled back |
| P4a | colleague nudge on own conversation | real worker | — | SKIPPED | 0 | — | none |
| P4b | takeover vs colleague nudge | HTTP | — | CANCELLED | 0 | — | none |
| P4c | reschedule during lease | real claim + re-take | human commits in lease | SENT | 1, **new text, old time** | — | none |
| P4d | cancel vs dispatch | real worker | cancel commits last | CANCELLED + sent_at | 1 | — | none |
| P4e | suspension; removed member | real worker | — | SKIPPED; SENT | 0; 1 | — | — |
| P5 | cross-customer linkage; LOST reopen | HTTP | — | 201/201; 500 | — | — | rolled back |
| P6 | naive local `scheduled_at`; counts; cursors | HTTP | — | +3 h drift; scoped; scoped | — | — | — |

Redis enqueue or notification failure after a committed handoff is **not applicable**: no CRM obligation is carried by Redis.

---

## 29. Invariant Sweep (`wasla_invariants`, migration-built, non-vacuous)

```text
POPULATION  tenants 28 · memberships 84 (revoked 3) · contacts 42 · conversations 42 (human 12, assigned 4)
            leads 24 · lead_activities 17 · follow_ups 30 · handoff events 15 · agent_turns 7
```

| Invariant | Count | Meaning |
|---|---|---|
| I01 conversation/contact tenant mismatch | 0 | composite FK |
| I02 / I03 lead/contact, lead/conversation tenant mismatch | 0 / 0 | app check held |
| I04 follow-up/conversation tenant mismatch | 0 | app check held |
| **I05 follow-up/lead tenant mismatch** | **1** | CRM-01 |
| I06 / I07 note/lead, analytics/conversation tenant mismatch | 0 / 0 | |
| **I08 lead contact ≠ its conversation's contact** | **1** | CRM-14 |
| **I09 follow-up lead's contact ≠ follow-up conversation's contact** | **2** | CRM-01 / CRM-14 |
| I10 assignee not a member at all | 0 | |
| **I11 conversation assigned to revoked member** | **2** | CRM-11 |
| **I12 lead assigned to revoked/non member** | **1** | CRM-11 |
| I13 AI mode carrying a handoff reason | 0 | |
| I14 human mode with no assignee | 12 | CRM-18 (INFO) |
| **I15 >1 handoff without a resume between** | **3** | CRM-02 / CRM-03 |
| I16 human takeovers with an own audit row | 0 of 12 (2 rows are agent rows) | CRM-05 |
| I17 >1 pending follow-up per conversation | 0 | index |
| **I18 cancelled follow-up naming a sent message** | **1** | CRM-10 |
| I19 / I21 pending follow-up on human conversation (any / agent) | 0 / 0 | |
| I20 pending follow-up in unserved workspace | 1 | a colleague nudge in a suspended workspace; will SKIP (PD-TOOLS-06) |
| I22 >1 open lead per contact | 0 | index |
| **I23 open lead with closed_at set** | **1** | CRM-07 |
| I24 lead status ≠ last recorded transition | 0 | |
| **I25 two transitions leaving the same state** | **1** | CRM-07 |
| I26 verified field last changed by agent | 0 | **false negative**: the P2d row escapes it because activities are ordered by transaction start (CRM-16) |

---

## 30. Mutation Matrix

Each mutation replaced text that occurs exactly once. The focused subset ran first; a survivor was then re-run against the full 35-file CRM suite. Original bytes were restored and SHA-256 verified every time (`restored=True` × 31). The control run, with nothing mutated, was 548 passed / 1 skipped.

| ID | Property removed | Result | Killer |
|---|---|---|---|
| M01 | tenant predicate, conversation | killed | `test_tenant_isolation::test_every_operation_against_another_workspace_answers_not_found` |
| M02 | tenant predicate, lead | killed | same |
| M03 | tenant predicate, contact | killed | same |
| M04 | conversation assignee active member | killed | `test_tenant_isolation::test_a_workspace_cannot_assign_its_work_to_an_outsider` |
| M05 | lead assignee active member | killed | `test_lead_crm::test_a_lead_cannot_be_assigned_to_someone_outside_the_workspace` |
| M06 | handoff event only on real change | killed | `test_analytics_events::test_setting_a_mode_it_already_has_is_not_a_second_handoff` |
| M07 | takeover cancels follow-ups | killed | `test_follow_up_revalidation::test_taking_a_conversation_over_cancels_the_nudge_waiting_on_it` |
| **M08** | **release clears the handoff reason** | **survived** | — (full suite) |
| M09 | sentiment skips human-owned | killed | `unit/test_sentiment_service::test_a_conversation_a_person_already_owns_is_not_analysed` |
| M10 | handoff tool leaves HUMAN alone | killed | `test_tool_authority::test_the_handoff_tool_refuses_a_conversation_a_colleague_already_owns` |
| M11 | dispatch refuses HUMAN | killed | `test_follow_up_revalidation::test_a_follow_up_on_a_human_owned_conversation_sends_nothing` |
| M12 | agent can't schedule on HUMAN | killed (full) | `test_tool_deferred_effects::test_an_agent_cannot_schedule_a_nudge_on_a_human_owned_conversation` |
| M13 | cancel leaves finished row | killed | `test_follow_ups::test_cancelling_an_already_sent_follow_up_changes_nothing` |
| M14 | dispatch refuses unserved workspace | killed (full) | `test_tool_deferred_effects::test_a_workspace_that_is_not_served_sends_no_automated_message[suspended…]` |
| M15 | lead capture refuses HUMAN | killed | `test_lead_crm::test_extraction_stops_once_a_colleague_takes_over` |
| M16 | extraction skips verified fields | killed | `test_lead_crm::test_extraction_does_not_overwrite_what_a_person_entered` |
| **M17** | **agent-writable filter** | **survived** | — (redundant: `ExtractedLead` has only the six fields) |
| M18 | no-op not a change | killed | `test_tool_semantics::test_a_lead_call_that_changed_nothing_writes_no_audit_row` |
| M19 | lead transition graph | killed | `test_lead_crm::test_an_illegal_transition_is_refused` |
| M20 | agent attribution | killed | `test_lead_crm::test_a_skipped_extraction_is_recorded_not_silent` |
| **M21** | orchestrator mid-turn takeover re-read | **survived alone** | — |
| **M22** | final pre-send human check | **survived alone** | — |
| M21+M22 | both takeover layers | killed | `test_ai_lifecycle::test_a_conversation_taken_over_during_inference_sends_no_reply` |
| M23 | manual lead contact scoping | killed | `test_tenant_isolation::…answers_not_found` |
| M24 | contact creation savepoint | killed | `test_whatsapp_inbound_concurrency::test_distinct_first_messages_from_one_customer_build_one_conversation[2]` |
| M25 | follow-up creation savepoint | killed | `test_tool_concurrency::test_a_follow_up_that_loses_its_race_costs_the_call_and_not_the_turn` |
| M26 | lead creation savepoint | killed | `test_tool_concurrency::test_a_lead_write_that_loses_its_race_costs_the_call_and_not_the_turn` |
| M27 | only active memberships authorise | killed | `test_membership_revocation::test_a_revoked_member_is_refused_by_every_workspace_route` |
| M28 | dispatch re-checks pending | killed | `test_follow_ups::test_dispatching_something_already_finished_does_nothing` |
| M29 | inbound reply cancels the nudge | killed | `test_follow_ups::test_an_inbound_webhook_cancels_the_waiting_nudge` |

**Inapplicable (the property does not exist to remove):** conversation assignment lock, stale-state precondition, follow-up `lead_id` tenant check, handoff reason authority, recovery of stranded handoff or follow-up. Each absence is a finding or a non-requirement above.

```text
applied 29 (+1 paired) · killed 25 (+pair) · survived 4 · inapplicable 5
```

---

## 31. Structural Test Gaps

* **TG-1 (M08):** "resuming AI clears the reason" is documented in `docs/CRM.md` and has no test.
* **TG-2 (M17):** the agent-writable filter is untested belt-and-braces. It is equivalent under today's callers; low value, recorded for completeness.
* **TG-3 (M21, M22):** each takeover send guard is unpinned on its own; only the pair is held. Removing one layer in a refactor passes every test.
* **TG-4:** no real-PostgreSQL concurrency test exists for any **human** CRM write: set_mode, assign, lead PATCH, lead status, follow-up cancel or reschedule versus dispatch. Every CRM race in §13–§14 was invisible to the suite for that reason.
* **TG-5:** `test_a_follow_up_on_a_human_owned_conversation_sends_nothing` seeds a USER-created row, and so pins the behaviour CRM-08 reports as contradictory.

No test covers a foreign `lead_id` on `POST /follow-ups` (CRM-01), member removal's effect on assignments (CRM-11), NUL on the human CRM paths (CRM-12), or LOST reopen conflicting with a newer lead (CRM-13).

---

## 32. Findings

### CRM-01 — `POST /follow-ups` stores another workspace's lead, and tells a caller which lead ids exist

```text
Severity HIGH · Status OPEN · Classification tenant-isolation defect, data-integrity defect
Component FollowUpService.schedule / follow_ups.lead_id
Files app/services/follow_up_service.py:181-273, app/api/v1/follow_ups.py:64-90, app/db/models/follow_up.py (lead FK)
```

* **Evidence:** `P1.follow_up_foreign_lead: foreign_lead_status=201 response_lead_id_is_victims=True nonexistent_lead_status=409 cross_tenant_rows=1`. I05 = 1, I09 = 2.
* **Reproduction:** as a member of A, `POST /api/v1/follow-ups {"conversation_id": <A conv>, "delay_minutes": 30, "body": "hi", "lead_id": <B lead>}` → 201, and the response echoes B's lead id. A random UUID → 409 "could not be resolved": the FK violation is caught as the creation race and falls into the `# pragma: no cover` branch.
* **Impact:** a cross-tenant reference written into A's data. It gives an existence oracle for B's lead ids and a same-tenant variant that ties a nudge to the wrong customer's lead. When B deletes the lead, A's row changes (`SET NULL`). Nothing of B's is read or modified, which is why this is not CRITICAL.
* **Current semantics:** `lead_id` is passed from the request body to the insert unvalidated. The reschedule branch ignores it.
* **Required semantics:** `lead_id` must name a lead in this workspace, and should belong to the conversation's contact. A foreign or nonexistent id must answer 404 or 422, identically for both.
* **Why tests missed it:** the isolation matrix attacks ids in the path, not ids in bodies. The follow-up endpoint test posts `lead_id: None`.
* **Required remediation:** resolve `lead_id` through `LeadRepository.require_by_id` (tenant-scoped) before scheduling, and optionally check contact agreement. Add a composite FK `(tenant_id, lead_id) → leads(tenant_id, id)`. ADR-100's premise ("no API path builds such a row") no longer holds for this relation.
* **Permanent regression test:** HTTP, two workspaces: a foreign `lead_id` and a nonexistent `lead_id` both give the same 404, with no row. Raw-SQL relational integrity: a cross-tenant `follow_ups.lead_id` insert is refused.
* **Deployment verification:** run I05 against production after the fix; it must be 0.

### CRM-02 — Automated handoffs overwrite a colleague's committed handoff and record a second one

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, data-integrity defect
Component SentimentService._persist; _request_human_handoff; AgentWorker._answer_emptiness; InboxService.set_mode
Files app/services/sentiment_service.py:128-275, app/agents/registry.py:398-455, app/workers/ai_worker.py:784-815, app/services/inbox_service.py:90-160
```

* **Evidence:** P2a `reason='Escalated automatically: …' handoff_events=['user','sentiment'] outcomes=['escalated']`. P2c `reason='Customer asked for a manager' agent_handoff_audits=1 outcomes=['handed_off']`. I15 = 3.
* **Reproduction:** a real `AgentWorker`, with a colleague's `set_mode(HUMAN, reason)` committed (a) inside the sentiment provider call, or (b) between the handoff tool's mode check and its `set_mode`.
* **Impact:** the colleague's note ("VIP – call personally") is replaced by the machine's. Handoff analytics count two handoffs for one, and attribute one of them to automation. The turn outcome and audit trail claim the agent handed over a conversation a person already owned. That is TOOL-07's outcome, still reachable concurrently. Sentiment's window is a full provider call, not milliseconds. No AI message is sent (§12).
* **Current semantics:** each path reads the mode, then makes an external call or runs more code, then writes `mode`/`handoff_reason` unconditionally. `set_mode` rewrites the reason even when `changed` is False.
* **Required semantics:** a transition to HUMAN happens once. Automation may move `AI → HUMAN` only if the row is still AI at write time: a conditional `UPDATE … WHERE mode='ai' RETURNING`, or a row lock held only for the write. It never touches the reason of a conversation already HUMAN. Events and audit rows are written only by the winner.
* **Why tests missed it:** only sequential takeover tests exist (TG-4). The Tools remediation pinned the check, not the write.
* **Required remediation:** make `set_mode` a compare-and-set, used by all five writers. Sentiment should apply its escalation through it rather than assigning ORM attributes.
* **Permanent regression test:** P2a and P2c as integration tests, asserting the colleague's reason verbatim, exactly one handoff event, no agent audit row, and outcome `suppressed_human`.
* **Deployment verification:** none; locally reproducible.

### CRM-03 — Human handoffs are not idempotent under concurrency, and an empty re-take erases the reason

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, data-integrity defect
Component InboxService.set_mode · Files app/services/inbox_service.py:109-160
```

* **Evidence:** P3a `row_locks_held_before_commit=0 handoff_events=['user','user'] reason='bob: pricing question'`. P3b `reason_after=None`, no event.
* **Impact:** handoff counts are inflated. The first colleague's reason is lost silently. A colleague clicking "take over" on an already-taken conversation wipes the note explaining why it was taken, with nothing recording that it happened.
* **Required semantics:** one event per logical transition. A HUMAN→HUMAN request either leaves the reason alone or is an explicit reason edit that is refused when empty or stale (PD-CRM-5).
* **Remediation:** the same compare-and-set as CRM-02. Do not write `handoff_reason` when unchanged or null on HUMAN→HUMAN.
* **Test:** two real sessions taking over at once → one event. Re-take with no reason → reason unchanged.

### CRM-04 — Conversation assignment is last-writer-wins with no precondition; the loser is told it won

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, product decision (stale-write model)
Component InboxService.assign / POST /conversations/{id}/assignment · Files app/services/inbox_service.py:162-178, app/schemas/conversation.py:86-91
```

* **Evidence:** P3c `alice_told_she_owns=True bob_told_he_owns=True final_owner=bob`. Stale unassign → `after_stale=None`, erasing the admin's newer reassignment. `read_carries_version_field=False`.
* **Impact:** two colleagues both believe they own a customer, and a stale browser undoes a manager's reassignment. The row itself is never contradictory: there is one owner at a time.
* **Required semantics:** writes carry an expected current owner (or version) and answer 409 when it differs. Self-assign on an owned conversation is refused or explicit (PD-CRM-4).
* **Remediation:** an `expected_assigned_to_id` (or `If-Match`) precondition enforced with `UPDATE … WHERE assigned_to_id IS NOT DISTINCT FROM :expected`. Expose a version on `ConversationRead`.
* **Test:** the P3c interleavings over HTTP; the second writer gets 409.
* **Deployment verification:** operator UI stale-state behaviour after the precondition exists.

### CRM-05 — No audit trail for conversation ownership

```text
Severity MEDIUM · Status OPEN · Classification observability gap (audit)
Component InboxService (set_mode, assign, close, reopen), SentimentService.set_priority, FollowUpService (human create/cancel)
```

* **Evidence:** P3a `audit_rows=0`, P3c `assignment_trail_rows=0`, I16: 0 of 12 human takeovers have their own audit row.
* **Impact:** "who took this customer from whom, and when" cannot be answered for assignment at all. For takeover and release it can be answered only from a metrics table that records no previous state. CLAUDE.md §22/§30 require "track who took ownership / who owns the conversation / who performed administrative actions".
* **Required semantics:** an append-only record per ownership change: actor, conversation, previous owner/mode, new owner/mode, reason category, time. No-op changes write nothing.
* **Remediation:** `AuditAction` members for takeover, release, assign, unassign, close and reopen, written by the transition winner only (after CRM-02/03/04).
* **Test:** each operation writes exactly one row with previous and new values; a no-op writes none.

### CRM-06 — Human-verified lead data is not protected under concurrency

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, data-integrity defect
Component LeadService.update_lead / capture_from_conversation · Files app/services/lead_service.py:303-359, 516-691
```

* **Evidence:** P2d `final_email=old.address@example.com human_verified=['email'] human_value_survived=False`. P3e `verified_after_two_edits=['email']`, then `human_name_overwritten_by_ai=True`.
* **Impact:** ADR-021's core rule ("the AI never overwrites what someone confirmed") fails in two ways. A correction made while the lead tool is running is lost. Two colleagues editing different fields together drop one field's protection, and the next extraction then overwrites a confirmed value. The record still claims the field is human-verified.
* **Required semantics:** a human-verified field is never written by extraction, and verification marks are never lost.
* **Remediation:** lock the lead row (`FOR UPDATE`) for the read-modify-write in both paths, which are short and involve no external calls. Alternatively, update `human_verified_fields` with array union in SQL and guard extraction writes with `WHERE NOT (:field = ANY(human_verified_fields))`.
* **Test:** P2d and P3e as integration tests on real PostgreSQL.

### CRM-07 — The lead state machine is not enforced under concurrency

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, data-integrity defect
Component LeadService.change_status · Files app/services/lead_service.py:361-407
```

* **Evidence:** P3f `final_status=proposal closed_at_set=True timeline=[(new,qualified),(qualified,won),(qualified,proposal)]`. I23 = 1, I25 = 1.
* **Impact:** a WON deal returns to the pipeline, violating the documented terminal state. `closed_at` contradicts the status, and the timeline records two exits from one state. Pipeline reporting counts the deal twice.
* **Required semantics:** a transition is validated against the committed status: `UPDATE … WHERE status = :from` or a row lock, and the loser gets 409.
* **Test:** two sessions, WON vs PROPOSAL from QUALIFIED. Exactly one succeeds, and there is one transition row.

### CRM-08 — A colleague's follow-up on a conversation they own is accepted and never sent

```text
Severity MEDIUM · Status OPEN · Classification reliability defect, product decision
Component FollowUpService.schedule vs dispatch; InboxService.set_mode cancellation
Files app/services/follow_up_service.py:212-224, 504-518; app/services/inbox_service.py:114-136
```

* **Evidence:** P4a `schedule=201 created_by_kind=user final=skipped detail='A colleague has taken this conversation over.'`. P4b: a takeover cancels a colleague's own earlier nudge.
* **Impact:** the documented "ordinary way to use the feature" is a 201 followed by a guaranteed `SKIPPED`. A colleague's reminder is discarded when another colleague takes over. Workspace suspension, by contrast, deliberately preserves colleague nudges. Visible in the row, so not silent, but the obligation the API accepted is never met.
* **Required semantics:** one rule, applied at both ends: either refuse USER follow-ups on HUMAN conversations at scheduling, or send them at dispatch and do not cancel them at takeover (PD-CRM-1).
* **Why tests missed it:** TG-5 pins the skip with a USER-created row.
* **Test:** a USER-created follow-up on a HUMAN conversation, end to end through the worker, asserting whichever outcome the decision chooses at both scheduling and dispatch.

### CRM-09 — A reschedule landing inside the sweep's lease is ignored, and the old nudge goes out with the new text

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect
Component DueFollowUpClaim.claim_by_id / FollowUpService._reschedule · Files app/repositories/follow_up_repository.py:173-230, app/services/follow_up_service.py:254-296
```

* **Evidence:** P4c `reschedule_status=201 same_row=True rescheduled_to_later=True final=sent sent_now=1 sent_body='new text for Thursday'`.
* **Impact:** a colleague moves a nudge to Thursday, gets a 201, and the customer receives Thursday's words now. The window is up to the 300 s lease across a batch of 20 serial sends.
* **Required semantics:** the per-row re-take sends only a row that is still due and still the row that was claimed. Rescheduling a leased row either waits for the dispatch or supersedes it.
* **Remediation:** a claim token or `claimed_until` column, where reschedule clears the claim and `claim_by_id` requires `claimed_by = :token AND scheduled_at <= now()`. Alternatively, have reschedule refuse or defer while leased.
* **Test:** claim, reschedule, re-take: nothing is sent; the row is pending at the new time.

### CRM-10 — Cancel racing dispatch: the colleague is told "cancelled" and the row ends CANCELLED although the message was sent

```text
Severity MEDIUM · Status OPEN · Classification concurrency defect, data-integrity defect
Component FollowUpService.cancel · Files app/services/follow_up_service.py:378-393, 427-431
```

* **Evidence:** P4d `api_told=cancelled after_dispatch=sent final=cancelled has_message=True has_sent_at=True customer_received=1`. I18 = 1.
* **Impact:** the record contradicts reality. Under the real messaging service's commit points, the opposite interleaving ends SENT while the API had already answered "cancelled". Either way the colleague's belief that the customer got nothing is false.
* **Required semantics:** cancel is a conditional transition (`UPDATE … SET status='cancelled' WHERE status='pending' RETURNING`) and reports what actually happened.
* **Test:** the P4d interleaving; final SENT, and the API reports the true status.

### CRM-11 — Removed members keep ownership, and assignment can land on an already-revoked member

```text
Severity MEDIUM · Status OPEN · Classification data-integrity defect, concurrency defect, product decision
Component MembershipService.revoke; InboxService.assign; LeadService.assign
```

* **Evidence:** P3d `conversation_still_assigned_to_removed=True lead_still_assigned_to_removed=True`. Race: `bob_membership=revoked conversation_assigned_to_revoked=True`. I11 = 2, I12 = 1.
* **Impact:** conversations and deals stay owned by someone who can no longer open them, and nothing surfaces them for reassignment. The membership check is read unlocked, while `revoke` takes the tenant lock, so the two do not serialise.
* **Required semantics:** assignment and revocation serialise, for example by assignment taking the membership row `FOR SHARE`. What removal does to existing ownership is a product decision (PD-CRM-2).
* **Test:** the race with two sessions → 404 for the assignment. Sequential removal → the chosen policy.

### CRM-12 — NUL in human CRM text is a 500

```text
Severity LOW · Classification reliability defect (input validation)
```

`P3h`: `handoff_reason`, note body, lead name, custom field value, tag, status reason, follow-up body and follow-up reason each gave **500** with a NUL. Exact-max Arabic (200 characters) and emoji (200) gave 200, and max+1 gave 422. The Tools remediation guarded the agent path (`storable_problem`); the human paths use no such guard. Nothing is corrupted, because the transaction rolls back. Remediation: apply `app.core.text_safety` in the request schemas. Test: each field with NUL → 422.

### CRM-13 — Reopening a LOST lead while the customer has a newer open lead is a 500

```text
Severity LOW · Classification reliability defect
```

`P5.reopen_lost_with_newer_open: lost=200 newer=201 reopen=500`. `uq_leads_active_contact` is the correct refusal, but the IntegrityError escapes. Remediation: check or catch the constraint and answer 409 with a message. Test: that sequence → 409.

### CRM-14 — A lead or follow-up can be tied to a different customer in the same workspace

```text
Severity LOW · Classification data-integrity defect
```

`P5.linkage: lead_contact_X_conversation_Y=201 follow_up_conv_Y_lead_X=201`. I08 = 1, I09 = 2. `create_lead` checks that each id exists in the workspace but not that they agree. Remediation: require `conversation.contact_id == contact_id` when both are supplied (and the CRM-01 check for follow-ups).

### CRM-15 — A naive `scheduled_at` is silently read as UTC

```text
Severity LOW · Classification data-integrity defect (time semantics)
```

`P6.scheduled_at: naive_local=201:drift_h=+3 explicit_offset=201:drift_h=+0`. A Cairo colleague entering a wall-clock time without an offset gets a nudge three hours late (two in winter). Storage is correct UTC. Remediation: refuse a naive datetime (require an offset), or resolve it with an explicit workspace time zone (PD-CRM-9).

### CRM-16 — The lead timeline is ordered by transaction start, not by what happened last

```text
Severity LOW · Classification observability gap
```

In P2d the agent's `fields_updated` row sorts **before** the human's although it committed last and decided the final value. I26 misses the P2d row for the same reason. `lead_activities.created_at` is `now()` (transaction start); the same lesson as AI-01 for messages. Remediation: order by a sequence or `clock_timestamp()` at insert.

### CRM-17 — Accepted residual at the send boundary (not reopened)

```text
Severity INFO · Classification observability / design note
```

AI remediation §18 and Messaging MSG accepted that a takeover committing between the final read and the Meta socket can still meet one reply. A cheaper fence than a lock held across Meta exists: make the `REQUESTED` transition (TX2) conditional on `conversations.mode = 'ai'`, which holds a row lock only for that statement. Offered for the remediation owner; not a finding against the accepted decision.

### CRM-18 — Handoffs have no owner and no server-side queue

```text
Severity INFO · Classification future feature, product decision
```

A takeover does not assign the taker, an agent or sentiment handoff assigns nobody, and the inbox has no mode, assignee or "unassigned" filter. I14 = 12. The durable obligation exists in PostgreSQL, but finding it requires paging the whole inbox. PD-CRM-3.

### CRM-19 — Releasing to AI does not answer what arrived while a person owned it

```text
Severity INFO · Classification product decision
```

Turns for inbound messages during HUMAN end `suppressed_human`, and release enqueues nothing, so the AI waits for the next customer message. PD-CRM-6.

### CRM-20 — Contact identity relies on Meta's canonical `wa_id`

```text
Severity INFO · Classification documentation
```

There is no normalisation, and today only the webhook creates contacts. If any future path creates contacts from typed phone numbers, it must normalise to the same form. `ConversationStatus.PENDING` is also unreachable (no writer); either document it or remove it.

---

## 33. Findings Ledger

| ID | Severity | Short title | Classification | Blocking? |
|---|---|---|---|---|
| CRM-01 | HIGH | Follow-up stores another workspace's lead; existence oracle | tenant-isolation, data-integrity | **BLOCKER BEFORE PRODUCTION** |
| CRM-02 | MEDIUM | Automation overwrites a colleague's handoff and double-counts | concurrency, data-integrity | REMEDIATION REQUIRED |
| CRM-03 | MEDIUM | Concurrent takeovers duplicate; empty re-take erases reason | concurrency, data-integrity | REMEDIATION REQUIRED |
| CRM-04 | MEDIUM | Assignment last-writer-wins, no stale protection | concurrency, product decision | REMEDIATION REQUIRED |
| CRM-05 | MEDIUM | No audit trail for conversation ownership | observability (audit) | REMEDIATION REQUIRED |
| CRM-06 | MEDIUM | Human-verified lead data lost under concurrency | concurrency, data-integrity | REMEDIATION REQUIRED |
| CRM-07 | MEDIUM | WON lead reverted by a concurrent move | concurrency, data-integrity | REMEDIATION REQUIRED |
| CRM-08 | MEDIUM | Colleague follow-up on own conversation never sends | reliability, product decision | REMEDIATION REQUIRED |
| CRM-09 | MEDIUM | Reschedule during lease ignored; old time, new text | concurrency | REMEDIATION REQUIRED |
| CRM-10 | MEDIUM | Cancel vs dispatch leaves CANCELLED on a sent message | concurrency, data-integrity | REMEDIATION REQUIRED |
| CRM-11 | MEDIUM | Removed members keep ownership; assign races revoke | data-integrity, concurrency | REMEDIATION REQUIRED |
| CRM-12 | LOW | NUL in human CRM text → 500 | reliability | REMEDIATION REQUIRED |
| CRM-13 | LOW | LOST reopen vs newer lead → 500 | reliability | REMEDIATION REQUIRED |
| CRM-14 | LOW | Same-tenant wrong-customer linkage | data-integrity | REMEDIATION REQUIRED |
| CRM-15 | LOW | Naive `scheduled_at` read as UTC | data-integrity (time) | REMEDIATION REQUIRED |
| CRM-16 | LOW | Lead timeline misorders concurrent edits | observability | REMEDIATION REQUIRED |
| CRM-17 | INFO | Accepted send-boundary residual; cheaper fence exists | design note | — |
| CRM-18 | INFO | Handoffs unowned; no human queue filter | future feature | FUTURE FEATURE |
| CRM-19 | INFO | Release does not answer the HUMAN-period backlog | product decision | PRODUCT DECISION |
| CRM-20 | INFO | Identity relies on Meta `wa_id`; `PENDING` unreachable | documentation | — |
| TG-1 | — | Release-clears-reason untested (M08) | test gap | TEST GAP |
| TG-2 | — | Agent-writable filter untested (M17) | test gap | TEST GAP |
| TG-3 | — | Each takeover send guard unpinned alone (M21/M22) | test gap | TEST GAP |
| TG-4 | — | No real-PG concurrency tests on human CRM writes | test gap | TEST GAP |
| TG-5 | — | Test pins CRM-08's skip for USER rows | test gap | TEST GAP |

---

## 34. Product Decisions

| ID | Question | Current behaviour | Risk | Decision required |
|---|---|---|---|---|
| PD-CRM-1 | Do colleague follow-ups run on a human-owned conversation? | accepted, then always skipped; cancelled on takeover | colleague reminders silently never go | refuse at scheduling, or send and survive takeover |
| PD-CRM-2 | What does member removal do to their conversations, leads and nudges? | nothing; nudges still send | orphaned ownership | unassign / transfer to remover / leave + surface |
| PD-CRM-3 | Does a takeover assign the taker? Does an automatic handoff assign anyone? | neither | unowned handoffs | auto-assign rule, or a queue view |
| PD-CRM-4 | Stale-write model for assignment and release | last-writer-wins | stale UI erases newer truth | expected-owner/version with 409, or documented LWW |
| PD-CRM-5 | Who may edit a handoff reason, and is its history kept? | anyone, any time, including to null | lost context | reason edits explicit; history in audit |
| PD-CRM-6 | Does release to AI answer messages received during HUMAN? | no | customer waits for their next message | answer the last unanswered message, or not |
| PD-CRM-7 | Is human verification permanent against AI? | yes (ADR-021), not enforced concurrently | — | confirm; enforce (CRM-06) |
| PD-CRM-8 | Should a removed member's pending nudges continue? | yes | a nudge from someone who left | continue / cancel / reassign |
| PD-CRM-9 | Time-zone contract for scheduled times | naive = UTC | 2–3 h drift | require offset, or a workspace time zone |
| PD-CRM-10 | May any member reassign a colleague's conversation? | yes (documented triage policy) | ownership churn | confirm |

---

## 35. Future Requirements

* An inbox "awaiting human", "mine" and "unassigned" view with counts (CRM-18).
* An ownership history, derivable from the CRM-05 audit rows. No new table is needed.
* Notifications to an assignee, if added, must commit the assignment first and treat notification as best-effort (§24).
* Contact edit, merge and delete, if ever added, need their own audit, since none exists today.

---

## 36. Final Deployment Verification (deployment-only)

* Operator UI stale-state behaviour against the CRM-04 precondition, once it exists.
* Run invariants I05, I08–I12, I15, I18 and I23 against production data after remediation; each must be 0.
* Unchanged from earlier audits: Real Meta inbound media DV-1…DV-8 and production alert delivery. **Not performed here.**

Nothing locally reproducible is deferred to this list.

---

## 37. Reliability / Security Score

Weights fixed before scoring.

| Dimension | Weight | Score /10 | Weighted | Reason |
|---|---|---|---|---|
| Tenant isolation | 0.12 | 7 | 0.84 | 22/22 path attacks refused, lists/counts/cursors scoped; one body-id cross-tenant write + oracle (CRM-01) |
| Authorization | 0.07 | 8 | 0.56 | role lines as documented; revoked and suspended refused immediately |
| Contact identity integrity | 0.05 | 8 | 0.40 | unique per tenant, race converges; cross-customer linkage (CRM-14) |
| Conversation ownership | 0.08 | 5 | 0.40 | single-row truth; no precondition, orphaned owners, unowned handoffs |
| Human-over-AI authority | 0.10 | 7 | 0.70 | sends = 0 under every race; reason and event overwritten by automation |
| Assignment concurrency | 0.07 | 4 | 0.28 | 0 locks, both told they own, stale erase, revoke race |
| Handoff idempotency | 0.06 | 5 | 0.30 | sequentially idempotent; concurrent duplicates |
| Lead / CRM update integrity | 0.09 | 4 | 0.36 | verified protection and terminal state both break concurrently |
| Follow-up correctness | 0.09 | 5 | 0.45 | send-safety holds; reschedule/cancel races; colleague nudges never sent |
| Member / workspace lifecycle | 0.06 | 6 | 0.36 | suspension exemplary; removal orphans |
| Recovery durability | 0.04 | 8 | 0.32 | PostgreSQL-only truth, single transactions, no external coupling |
| Auditability | 0.07 | 3 | 0.21 | lead timeline good; no ownership audit at all |
| Privacy / logging | 0.03 | 9 | 0.27 | no CRM PII in logs |
| Observability | 0.02 | 6 | 0.12 | structured events; timeline ordering; no ownership metrics |
| Testing | 0.03 | 5 | 0.15 | 25/29 mutations killed; zero human-path concurrency tests |
| Operational readiness | 0.02 | 6 | 0.12 | gates green; invariants runnable |
| **Total** | **1.00** | | **5.84** | |

---

## 38. Final Verdict

**CRM / Human Handoff: NOT CLOSED.**

Customers are protected. No AI or automated message reached a customer after a committed takeover, in any race this audit could build, and tenant isolation held on every path but one. The CRM record and ownership model are not yet trustworthy under concurrent use:

* one cross-tenant write must be fixed before production (CRM-01);
* ten medium defects share one root cause: human CRM writes are unlocked read-modify-writes with no state precondition and, for ownership, no audit trail;
* ten product decisions set the rules the remediation must implement.

Recommended remediation order:

1. CRM-01.
2. A compare-and-set primitive for mode, assignment, lead status, lead fields and follow-up cancel/reschedule (CRM-02, 03, 04, 06, 07, 09, 10, 11).
3. Ownership audit (CRM-05).
4. The PD-CRM-1 follow-up rule (CRM-08).
5. The LOW items and test gaps.

```text
Audit id              crm-c4h7
Frozen HEAD           4949135eb698b4143e1d7e66654bb5c1df78e260
Branch                worktree-billing-google-auth
Worktree              E:\wasla-crm-handoff-audit (+ E:\wasla-crm-mutation for mutations)
Alembic head          0067
Isolation             project wasla-crm-audit-c4h7 · PG system_identifier 7687082338105372717
                      Redis run_ids 3c385164… (lane 1), b5385fe1… (lane 2) · MinIO in-project
Baseline              ruff ✔ · black ✔ (580) · mypy ✔ (580) · alembic 0067, check clean
                      model-built whole 5081 passed / 17 skipped / 0 failed
                      migration-built integration+e2e 2519 passed / 2 skipped / 0 failed
CRM targeted          model-built 35 files 548 passed / 1 skipped · migration-built 27 files 353 passed / 1 skipped
Runtime probes        22 probe tests (P1-P6) · 12 concurrency cases on real PostgreSQL / real workers
Invariants            population 28 tenants, 42 conversations, 24 leads, 30 follow-ups, 15 handoff events
                      violations: I05 1 · I08 1 · I09 2 · I11 2 · I12 1 · I15 3 · I18 1 · I23 1 · I25 1
Mutations             applied 29 (+1 pair) · killed 25 (+pair) · survived 4 · inapplicable 5
Findings              CRITICAL 0 · HIGH 1 · MEDIUM 10 · LOW 5 · INFO 4 · test gaps 5
Product decisions     10
Deployment backlog    UI stale-state check (after CRM-04); production invariant run; carried DV-1…8
Score                 5.84 / 10
Verdict               NOT CLOSED - remediation required; CRM-01 blocks production
```
