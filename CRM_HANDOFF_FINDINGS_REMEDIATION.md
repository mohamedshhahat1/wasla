# Wasla — CRM / Human Handoff Findings Remediation

```text
Audit                crm-c4h7 (CRM_HANDOFF_AUDIT.md), score 5.84 / 10, NOT CLOSED
Audit baseline HEAD  4949135eb698b4143e1d7e66654bb5c1df78e260
Final code/test HEAD c5c7f7cd03e56a52efde5ea2004ba6a8d05299c4   (frozen for every authoritative gate)
Report/docs HEAD     the commit adding this file (docs only, child of c5c7f7c)
Branch               crm-handoff-findings-remediation
Worktree             E:\wasla-crm-handoff-remediation
Mutation lane        E:\wasla-crm-rem-mutation (detached; own Redis; restored clean after every mutation)
Alembic              0067 -> 0068 (single migration)
Evidence             E:\wasla-crm-rem-r5k8\{infra,logs,mutations}
```

---

## 1. Result

Every CRM/handoff transition is now applied against the state that exists at
commit time. A stale writer cannot overwrite newer human truth, every winning
ownership change leaves one audit row, and every accepted follow-up has one
honest ending.

```text
Findings    CRM-01 blocker CLOSED · 10/10 MEDIUM CLOSED · 5/5 LOW CLOSED
            INFO: CRM-17 accepted design note (unchanged) · CRM-18 future feature
                  CRM-19 closed by locked decision · CRM-20 documented
Test gaps   TG-1..TG-5 CLOSED
Mutations   64 defined · 63 killed · 0 survivors · 1 inapplicable (M10, code removed, property held by R29)
Invariants  16 invariants over a committed population built to break them · 0 violations
Score       5.84 -> 8.95
Verdict     CRM / HUMAN HANDOFF FINDINGS CLOSED
            FOR THE CURRENT CRM SURFACE
            WITH FINAL DEPLOYMENT VERIFICATION DEFERRED
```

Commits on the branch, all on top of `4949135`:

```text
d470d2c docs(crm): record independent CRM and handoff audit
3d0bdbc fix(crm): make ownership, lead and follow-up transitions authoritative
4d51071 test(crm): prove CRM transitions under real concurrency and sweep invariants
52db9d6 docs(crm): align CRM ownership and follow-up contracts
0779cb6 refactor(ai): let the handoff transition alone decide a human-owned conversation
c5c7f7c test(crm): pin the remaining transition layers individually
<this>  docs(crm): record the CRM and handoff findings remediation
```

`3d0bdbc` is one commit rather than the eleven the brief sketched. The
remediation is one mechanism, the compare-and-set on the committed row. It
threads through the same services (`InboxService`, `LeadService`,
`FollowUpService`), and splitting it by finding would have produced
intermediate commits that do not pass the suite. Each finding's part is
itemised in that commit's message and in §5.

---

## 2. Isolation

```text
Docker project     wasla-crm-rem-r5k8 (own network, volumes; loopback ports 57151-57153)
Final PostgreSQL   16.15, system_identifier 7687171825556209702  (fresh volumes, recreated at c5c7f7c)
Final Redis        lane 1 run_id 54b21d1665a0f3c1a3fdec9ff6b76dee5e28c537 (runner joins its netns)
Mutation Redis     lane 2 (redis2), separate container; lane-2 run_id 54bd2e88eb541f24eca1f98187be40333d67bbb9
                   before the final recreate
Code path          /work/app/__init__.py -> E:\wasla-crm-handoff-remediation (lane 1)
                   /work -> E:\wasla-crm-rem-mutation (lane 2)
Runner image       wasla-crm-rem-runner:r5k8 (re-tag of wasla-crm-runner:c4h7; pyproject unchanged)
```

* The developer stack (`wasla-api-1`, `wasla-postgres-1`, `wasla-redis-1`) and
  the other sessions' stacks, including the audit's `wasla-crm-audit-c4h7`, were
  not touched.
* Databases by purpose: `wasla_models` / `wasla_migrations` for whole suites,
  `wasla_dev` for targeted and boundary suites, `wasla_alembic` for migration
  proofs, and `wasla_mut` for mutations only.
* Before any change, the audit's CRM suite reproduced exactly on this
  infrastructure: 548 passed, 1 skipped.
* The authoritative evidence in §3 comes from one frozen HEAD, a clean
  worktree, and freshly recreated volumes. Nothing ran alongside it.

**No run cited as authoritative is CONTAMINATED.**

---

## 3. Authoritative Gates (frozen `c5c7f7c`, fresh volumes)

| Gate | Result |
|---|---|
| `ruff check app tests` | All checks passed |
| `black --check app tests` | 589 files would be left unchanged |
| `mypy app tests` | Success: no issues found in 589 source files |
| `alembic heads` / `upgrade head` / `check` | `0068 (head)` / to 0068 / "No new upgrade operations detected" |
| Model-built whole `tests/` | **5150 passed, 17 skipped, 0 failed** |
| Migration-built `tests/integration` + `tests/e2e` | **2587 passed, 2 skipped, 0 failed** |
| CRM/Handoff targeted, model-built (40 files) | **617 passed, 1 skipped, 0 failed** |
| CRM/Handoff targeted, migration-built (32 files) | **421 passed, 1 skipped, 0 failed** |
| Boundary: Auth/Authorization touched by revocation (11 files) | **164 passed, 0 failed** |
| Boundary: AI/Tools touched by the handoff CAS (17 files) | **200 passed, 0 failed** |
| Boundary: Workers/Messaging touched by follow-up claims (12 files) | **142 passed, 0 failed** |
| CRM invariant sweep (`test_crm_invariants.py`, in both CRM runs) | passed: 16 invariants, 0 violations |
| Concurrency stability (4 race files, x5) | 27/27 every run |

The CRM targeted set is the audit's 35 files plus the five new ones
(`test_crm_races`, `test_crm_follow_up_claims`, `test_crm_handoff_authority`,
`test_crm_integrity`, `test_crm_invariants`). The skip is the same one the audit
recorded: `test_tool_invariants.py:192` runs only when kept tool data exists.

Baseline for comparison: model-built 5081 / 17 skipped. Migration-built
2519 / 2 skipped.

### Migration proof (`logs/migration_proof.txt`)

```text
A. populated 0067 (2 tenants: contacts, conversations, leads, notes, activities, follow-ups)
   0067 -> 0068        counts unchanged; alembic check clean
   0068 -> 0067        counts unchanged
   0067 -> 0068        counts unchanged
B. 0067 holding one cross-tenant follow_ups.lead_id
   upgrade refused:    RuntimeError ... "follow_ups.lead_id in another workspace: 1 row(s)" ... "Nothing has been changed"
   current             0067; no claim columns; no new constraint
C. fresh base -> 0068  alembic check clean
```

---

## 4. Product Decisions Implemented

| ID | Decision, as implemented |
|---|---|
| PD-CRM-1 | A USER follow-up is accepted on a HUMAN conversation, survives a takeover, and is **sent**. Opt-out, workspace, closed-conversation, window and template guards still apply. An AGENT follow-up is still refused, cancelled and skipped on HUMAN. |
| PD-CRM-2 | Revocation (removal, leaving, account closure) unassigns the member's conversations and leads in the same transaction. Nothing moves to the remover. History stays attributed. |
| PD-CRM-3 | A manual takeover from AI sets `mode=HUMAN`, the reason, and `assigned_to_id=actor` in one transition. HUMAN→HUMAN is a no-op. Automatic handoffs never assign anyone and keep an existing owner. |
| PD-CRM-4 | Conversation and lead assignment require `expected_assigned_to_id`. A stale write returns 409 `stale_assignment` and changes nothing. |
| PD-CRM-5 | Only the winner of AI→HUMAN writes the reason. While HUMAN, no takeover, empty takeover or stale automation changes it. Release clears it. Editing a reason is not implemented. |
| PD-CRM-6 | Release to AI does not answer the HUMAN-period backlog. |
| PD-CRM-7 | Human-verified fields outrank AI extraction permanently, including under concurrency. |
| PD-CRM-8 | A revoked member's pending USER follow-ups are cancelled with `cancelled_reason=member_revoked`. Dispatch re-checks the author. |
| PD-CRM-9 | `scheduled_at` must carry a UTC offset. A naive value is 422 at both schema and service. It is stored normalised to UTC. |
| PD-CRM-10 | Any active member may reassign a conversation. Lead assignment stays admin-only. |

---

## 5. Findings Ledger

### CRM-01 — Cross-tenant follow-up lead (HIGH) — **CLOSED**

* **Before:** `POST /follow-ups` stored another workspace's `lead_id` and returned 201. A nonexistent id returned 409, which was an existence oracle. The FK was plain.
* **Root cause:** `lead_id` went from the request body to the INSERT without being resolved. ADR-100 had assumed no API path could build such a row.
* **Fix:** `FollowUpService._require_lead_of` resolves the lead through the tenant-scoped `LeadRepository`. A foreign id and a nonexistent id both raise the same `NotFoundError`. A lead belonging to another customer is refused with 422. Migration 0068 adds `fk_follow_ups_tenant_lead (tenant_id, lead_id) → leads(tenant_id, id) ON DELETE SET NULL (lead_id)`. It also adds composite keys for follow-up→conversation, lead→contact, lead→conversation+contact, note→lead and activity→lead.
* **Files:** `app/services/follow_up_service.py`, `app/db/models/{follow_up,lead,conversation}.py`, `alembic/versions/20260919_0068_*.py`, `docs/AUTHORIZATION.md` §6.19, ADR-100 addendum.
* **Tests:** HTTP: `test_another_workspaces_lead_and_a_nonexistent_one_are_the_same_404` checks that both are 404 with the same code and message, that no id leaks and that no row is written. `test_a_lead_of_a_different_customer_is_refused` covers the service/HTTP path. Raw SQL: `test_the_database_refuses_a_cross_tenant_follow_up_lead` and `..._lead_contact`. Invariant `follow_up_lead_in_another_tenant` = 0.
* **Mutations:** R01, R02 and R03 killed.
* **Deployment verification:** run the RUNBOOK pre-deploy queries. The migration itself refuses crossed rows by name.

### CRM-02 — Automation overwriting a colleague's handoff (MEDIUM) — **CLOSED**

* **Before:** sentiment and the handoff tool read AI, then wrote mode and reason unconditionally. The colleague's reason was replaced, a second handoff was counted, an `agent_handoff_requested` audit row was written and the turn was marked `handed_off`.
* **Root cause:** there were five mode writers and no precondition at the write.
* **Fix:** every writer goes through `InboxService`. `hand_off` (agent, sentiment, empty-response, quota) locks the row (`FOR NO KEY UPDATE`) and moves it only if it is still AI. The loser writes nothing. The handoff tool raises its refusal, so there is no audit row and the execution is not recorded as succeeded. Sentiment records `superseded`, stores the reading as not escalated, and the turn ends `suppressed_human`. The orchestrator also stops after any tool round a takeover won, so no second inference is paid for. The tool's separate read-time check became equivalent to the CAS refusal and was removed (`0779cb6`).
* **Files:** `inbox_service.py`, `sentiment_service.py`, `agents/registry.py`, `agents/orchestrator.py`, `workers/ai_worker.py`.
* **Tests:** real `AgentWorker` + PostgreSQL + Redis: `test_a_takeover_during_the_sentiment_reading_is_not_overwritten` (P2a), `test_a_handoff_tool_that_loses_to_a_takeover_hands_nothing_over` (P2c), `test_an_empty_response_handoff_and_a_takeover_make_one_handoff`. Service race: `test_an_automated_handoff_that_loses_to_a_takeover_changes_nothing`. Unit: `test_a_takeover_during_the_reading_is_not_overwritten`.
* **Mutations:** R04, R06, R24, R27, R28 and R29 killed.

### CRM-03 — Concurrent takeovers and empty re-take (MEDIUM) — **CLOSED**

* **Fix:** the same CAS. HUMAN→HUMAN returns `changed=False` and writes no reason, owner, event or audit row.
* **Tests:** `test_two_takeovers_make_one_handoff_and_the_first_colleague_keeps_it` uses two sessions with the lock wait proven. `test_taking_over_an_owned_conversation_steals_nothing` covers the empty reason. `test_setting_a_mode_it_already_has_is_not_a_second_handoff` covers the reason being kept.
* **Mutations:** R04, R05 and R06 killed.

### CRM-04 — Assignment last-writer-wins (MEDIUM) — **CLOSED BY STALE-WRITE CONTRACT**

* **Fix:** `AssignmentRequest.expected_assigned_to_id` is required and nullable. The write happens under the row lock only if the current owner matches; otherwise 409 `stale_assignment`. No ownership-version column was added, because `assigned_to_id` is already on every read and is the smallest explicit contract. Lead assignment follows the same rule.
* **Tests:** `test_two_colleagues_self_assigning_at_once_one_wins_and_one_is_told` and `test_a_stale_unassign_cannot_erase_a_newer_reassignment` (both concurrent). `test_assignment_needs_the_expected_owner_and_a_stale_one_is_409` checks 422 when the field is missing, 409 when stale and 200 when fresh.
* **Mutation:** R07 killed.
* **Deployment verification:** operator UI handling of 409 `stale_assignment`.

### CRM-05 — No ownership audit (MEDIUM) — **CLOSED**

* **Fix:** seven `audit_action` labels were added: taken_over, released_to_ai, assigned, reassigned, unassigned, closed and reopened. Only the winning transition writes one. `meta` holds the conversation, previous/new mode, previous/new assignee, `source`, `reason_supplied`, and `cause` where relevant. The reason text is **not** stored, following the existing policy that audit records who and when, not what was said; the reason stays on the conversation row and in the analytics metadata. `actor_kind` is USER, AGENT or SYSTEM, and sentiment appears as `meta.source=sentiment`.
* **Tests:** `test_a_takeover_makes_the_colleague_the_owner_and_is_audited`, `test_release_to_ai_clears_the_reason_and_is_audited`, `test_close_and_reopen_are_audited_once_each`, `test_a_no_op_writes_no_audit_row`, and the race tests, which assert the loser writes 0 audit rows. Invariant `handoff_without_its_ownership_audit` = 0.
* **Mutations:** R22 and R23 killed.

### CRM-06 — Human-verified data under concurrency (MEDIUM) — **CLOSED**

* **Fix:** `update_lead` and extraction both take the lead row lock and re-read before applying. The model's values are composed without any lock. The verified set is a union with the committed list.
* **Tests:** `test_an_extraction_cannot_overwrite_a_correction_committed_before_it_applied` (P2d, with the agent transaction opened first) and `test_two_colleagues_editing_different_fields_both_stay_verified` (P3e, followed by an extraction that changes nothing).
* **Mutations:** R10, R11 and R11b killed.

### CRM-07 — Lead status not enforced concurrently (MEDIUM) — **CLOSED**

* **Fix:** `change_status` validates under the lead lock. The optional `expected_status` gives 409 `stale_lead_status`. Without it, a move that is illegal from the committed status is 422. WON stays terminal.
* **Tests:** `test_won_and_proposal_from_one_state_exactly_one_succeeds` (409, one exit from `qualified`, `closed_at` set) and `test_a_stale_move_without_a_precondition_cannot_leave_won`. Invariants `status_move_not_from_the_previous_status`, `lead_status_disagrees_with_its_last_move` and `open_lead_with_closed_at` are all 0.
* **Mutations:** R12 and R12b killed. The original M19 was killed.
* **Note:** the brief asks for a 409 for the loser. That holds when the client sends `expected_status`, which the docs tell it to do. A client that omits it gets the graph's 422, and the lead is still never corrupted.

### CRM-08 — Colleague follow-up never sent (MEDIUM) — **CLOSED BY LOCKED USER-FOLLOW-UP CONTRACT**

* **Fix:** the HUMAN dispatch refusal applies to AGENT rows only. A takeover cancels only AGENT pending rows.
* **Tests:** `test_a_colleagues_follow_up_on_a_human_owned_conversation_is_sent` (real worker, closes TG-5), `test_a_colleagues_follow_up_still_obeys_the_opt_out_on_a_human_conversation`, `test_taking_a_conversation_over_keeps_a_colleagues_own_reminder`, `test_an_agents_follow_up_on_a_human_owned_conversation_sends_nothing`, and `test_a_colleague_can_schedule_on_a_conversation_a_person_owns`.
* **Mutations:** R13, R14 and M11r killed.

### CRM-09 — Reschedule inside the lease (MEDIUM) — **CLOSED**

* **Fix:** a claim is now `claim_token` + `claimed_until`, and `scheduled_at` is never moved by the sweep. The re-take requires the exact token. A reschedule locks the row, clears the claim and applies the change. Once the intent is committed, a reschedule gets 409 `dispatch_in_progress`.
* **Tests:** `test_a_reschedule_inside_the_lease_takes_the_row_back_from_the_sweep` (0 sends, PENDING, Thursday, new text), `test_a_reschedule_holding_the_row_makes_the_retake_step_over_it`, `test_a_reschedule_of_a_nudge_already_being_sent_is_refused`, `test_a_worker_holding_a_superseded_claim_sends_nothing` and `test_the_claim_no_longer_moves_the_scheduled_time`.
* **Mutations:** R15, R15b and R17 killed.

### CRM-10 — Cancel racing dispatch (MEDIUM) — **CLOSED**

* **Fix:** cancel locks the row and waits for the re-take. A pending, not-yet-sent row is cancelled and its claim cleared. An in-flight row gets 409 `dispatch_in_progress`. A finished row is returned with its real status. The outcome after a send is written only under the same claim (`reacquire`). Bulk cancellations (inbound reply, takeover, lifecycle, revocation) skip in-flight rows.
* **Tests:** `test_a_cancel_racing_the_send_waits_and_is_told_the_truth` (lock wait proven, 409, final SENT with no cancelled fields), `test_a_cancel_inside_the_lease_stops_the_send` and `test_cancelling_after_the_send_reports_that_it_was_sent`. Invariant `cancelled_follow_up_that_was_sent` = 0.
* **Mutations:** R16 killed. The original M13 and M28 were killed.
* **Messaging boundary:** unchanged. The follow-up row now reports what the send protocol decided.

### CRM-11 — Removed members and the assignment race (MEDIUM) — **CLOSED BY LOCKED MEMBER-REMOVAL CONTRACT**

* **Fix:** `hold_active_for_user` takes a key-share lock on the workspace row, then `FOR SHARE` on the active membership. That is the same order revocation takes (tenant `FOR UPDATE`, then the membership UPDATE), so the two serialise. The first ordering tried was membership first; PostgreSQL detected a deadlock against revocation's tenant lock, which the race test caught. `release_departing_member` runs inside the revocation transaction, including account closure. It cancels the member's pending USER follow-ups, then unassigns their leads (with a lead activity), then their conversations (with an audit row).
* **Tests:** `test_an_assignment_that_reaches_the_member_first_is_undone_by_their_removal`, `test_an_assignment_that_reaches_the_member_second_is_refused` (404), `test_a_lead_assignment_racing_the_removal_lands_on_nobody`, `test_removing_a_member_releases_their_open_work_and_keeps_their_history` and `test_a_reminder_whose_author_has_left_is_cancelled_not_sent`. Invariants for conversation/lead assigned to a non-member and pending reminders of a departed member are all 0.
* **Mutations:** R08, R09, R30 and R32 killed.
* **Scope:** deleting a whole workspace revokes everyone and does not unassign anything. The workspace is no longer served, and the invariant queries skip deleted workspaces. This is documented in CRM.md.

### CRM-12 — NUL in human CRM text (LOW) — **CLOSED**

* **Fix:** `app/schemas/text.py` (`StorableText`) reuses `app.core.text_safety`. It covers the handoff reason, lead name/phone/email/interest/currency, tags, note body, status reason, and follow-up body, reason and template fields. `check_json` refuses NUL in keys and values of custom fields, which also covers template components and tool config.
* **Tests:** 11 parametrised HTTP cases return 422. Arabic and emoji at 200 characters are kept.
* **Mutations:** R18 and R18b killed.

### CRM-13 — LOST reopen collision (LOW) — **CLOSED**

* **Fix:** a fresh pre-check, plus the reopen flushed inside a savepoint that catches `uq_leads_active_contact`. Either way the result is 409 `open_lead_exists` with nothing changed. The row is then refreshed so the response does not lazy-load `updated_at`. That lazy load was a real 500 introduced during remediation and caught by the HTTP test.
* **Tests:** `test_reopening_a_lost_lead_behind_a_newer_open_one_is_a_409` and `test_the_reopen_collision_is_a_409_even_when_the_precheck_is_raced`.
* **Mutation:** R19 killed.

### CRM-14 — Same-tenant wrong-customer linkage (LOW) — **CLOSED**

* **Fix:** a manual lead naming both a contact and a conversation must agree (422). The DB enforces this with a three-column key. A follow-up's lead must belong to the conversation's customer (service check, 422).
* **Tests:** HTTP for the lead and the follow-up, plus raw SQL `test_the_database_refuses_a_lead_whose_conversation_is_another_customers`. Invariants `lead_conversation_with_another_customer` and `follow_up_lead_of_another_customer` are 0.
* **Mutation:** R02 killed.

### CRM-15 — Naive `scheduled_at` (LOW) — **CLOSED BY OFFSET-REQUIRED CONTRACT**

* **Tests:** `Z`, `+03:00` and `+02:00` are stored as the same instant. Naive is 422 over HTTP, at the service, and at the schema.
* **Mutations:** R20 was killed in round 2 by the schema test. R20b was killed.

### CRM-16 — Lead timeline ordering (LOW) — **CLOSED**

* **Fix:** `lead_activities.created_at` defaults to `clock_timestamp()` in both the model and migration 0068. Ordering is `(created_at, id)`, which is deterministic. Lead mutations are serialised by the row lock.
* **Test:** the P2d race opens the agent transaction first, so a transaction-start timestamp would sort its later write first. The test asserts the timeline is `[user, agent]`.
* **Mutation:** R21 killed.

### CRM-17 — Send-boundary residual (INFO) — **ACCEPTED DESIGN NOTE, UNCHANGED**

The window between the AI's final `refusal_now` read and Meta's socket stays as accepted by AI/Messaging. The cheap conditional `REQUESTED` fence was not adopted. It would change the Messaging send protocol, a closed subsystem, for a residual already accepted. One adjacent path did improve: the follow-up sweep now share-locks the conversation from its mode check to the send intent, so a takeover in that window waits and cannot land between "AI" and the send. The R26 test shows the same race deadlocks without that lock.

### CRM-18 — Awaiting-human queue (INFO) — **FUTURE FEATURE**

No inbox filter was built. A manual takeover now assigns the taker (PD-CRM-3). Automatic handoffs may remain unassigned by design.

### CRM-19 — No backlog replay on release (INFO) — **CLOSED BY LOCKED PRODUCT DECISION**

PD-CRM-6 is documented in CRM.md, the endpoint docstring and `release_to_ai`. No code change.

### CRM-20 — `wa_id` identity; `PENDING` status (INFO) — **DOCUMENTED**

CRM.md states that identity is Meta's canonical `wa_id` and that any future typed or imported contact path must normalise first. `ConversationStatus.PENDING` is documented as reserved and unreachable. It was not removed, because removing an enum label would require a table rewrite.

### Test gaps

| Gap | Closed by | Mutation |
|---|---|---|
| TG-1 release clears reason | `test_release_to_ai_clears_the_reason_and_is_audited` | M08 killed alone |
| TG-2 agent-writable filter | `test_extraction_cannot_reach_a_field_outside_the_agent_allowlist` (an extraction naming `source`, `score` and `status`) | M17 killed alone |
| TG-3 each send guard alone | `test_the_orchestrator_withholds_a_reply_composed_across_a_takeover` (orchestrator only, no worker gate in path); `test_the_final_check_refuses_a_takeover_after_the_orchestrator_looked` (takeover after the orchestrator returned) | M21 killed alone; M22 killed alone |
| TG-4 real-PG human concurrency | `test_crm_races.py`, `test_crm_follow_up_claims.py`, `test_crm_handoff_authority.py`: 2 takeovers, 2 self-assigns, stale unassign, extraction vs correction, 2 human edits, WON vs PROPOSAL, revoke vs assign (both orders, conversation and lead), reschedule and cancel vs dispatch, sentiment and tool vs takeover | R04–R12, R15–R17, R24–R29 |
| TG-5 USER on HUMAN pinned as skipped | replaced by `test_a_colleagues_follow_up_on_a_human_owned_conversation_is_sent` (real worker; the AGENT variant is kept) | R14 killed |

---

## 6. Runtime Race / Fault Matrix

Each row is a permanent test on real PostgreSQL. Races use two sessions on two
connections, with the second proven blocked on the first's lock
(`pg_stat_activity.wait_event_type='Lock'`) before the first commits.

| Case | HTTP / call outcome | Final DB | Customer-visible | Audit / history |
|---|---|---|---|---|
| Foreign `lead_id` follow-up | 404, same body as nonexistent | no row | none | none |
| Sentiment vs takeover | turn `suppressed_human` | HUMAN, colleague's reason, owner = colleague | 0 sends, 0 inference | 1 `handoff(user)`, 1 `taken_over` |
| Handoff tool vs takeover | tool refused | same | 0 sends, 1 inference | no `agent_handoff_requested` |
| Empty response vs takeover | colleague no-op | HUMAN by system | fallback (accepted residual) | 1 handoff, 1 `taken_over` |
| Two takeovers | loser `changed=False` | first colleague's reason and ownership | — | 1 event, 1 audit |
| Two self-assigns | loser 409 `stale_assignment` | first owner | — | 1 `assigned` |
| Stale unassign | 409 | reassigned owner kept | — | 1 `reassigned`, 0 `unassigned` |
| Revocation vs assignment (assign first) | both succeed, serialised | unassigned; membership revoked | — | `unassigned`, cause `member_revoked` |
| Revocation vs assignment (revoke first) | assignment 404 | unassigned | — | — |
| AI extraction vs human correction | both succeed, serialised | human email, `email` verified | — | timeline `[user email, agent interest]` |
| Two human field edits | both succeed, serialised | `verified=[email,name]`; later AI writes nothing | — | 2 user activities |
| WON vs PROPOSAL | loser 409 `stale_lead_status` (422 without precondition) | WON, `closed_at` set | — | one exit from `qualified` |
| USER follow-up on HUMAN | 201; worker sends | SENT, claim cleared | 1 message | — |
| Reschedule vs dispatch (in lease) | 201 | PENDING, Thursday, new text | 0 now | — |
| Reschedule after intent | 409 `dispatch_in_progress` | SENT, original text | 1 (original) | — |
| Cancel vs dispatch (waits on re-take) | 409 `dispatch_in_progress` | SENT, no cancel fields | 1 | — |
| Cancel in lease | 200 cancelled | CANCELLED, no message | 0 | — |
| LOST reopen collision | 409 `open_lead_exists` | stays LOST | — | — |
| Wrong-customer linkage | 422 (lead, follow-up); DB refuses raw insert | no row | — | — |
| Naive `scheduled_at` | 422 | no row | — | — |
| NUL in 11 human fields | 422 each | unchanged | — | — |
| Takeover vs sweep's decided send | takeover waits for the intent | SENT, then HUMAN | 1 (decided before takeover) | 1 `taken_over` |

Human-over-AI send safety is preserved in all three windows the brief names.
During inference (existing paired test plus M21 alone), 0 sends. After the
orchestrator's check (M22 alone), 0 sends. During the handoff and sentiment
paths (P2a, P2c), 0 sends.

Tenant isolation is preserved. The 22-operation attack matrix
(`test_every_operation_against_another_workspace_answers_not_found`) passes with
the new required bodies, and CRM-01 is now part of the closed set. M01–M05 and
M23 are killed.

---

## 7. Invariant Sweep

`tests/integration/test_crm_invariants.py` builds its population through the
real services in committed transactions: 3 workspaces × (manual takeover racing
a sentiment handoff, release, agent handoff, assignment, extraction racing a
correction, QUALIFIED→WON racing PROPOSAL, LOST reopen blocked by a newer lead,
USER reminder rescheduled inside the lease, AGENT nudge cancelled while in
flight, member removal with owned work).

```text
population   handoffs 6 · won leads 3 · member_revoked reminders 3 · sent follow-ups 3 · rescheduled pending 3
violations   follow_up_lead_in_another_tenant 0 · lead_conversation_with_another_customer 0
             follow_up_lead_of_another_customer 0 · conversation_assigned_to_non_member 0
             lead_assigned_to_non_member 0 · pending_reminder_of_a_departed_member 0
             second_handoff_without_a_resume 0 · handoff_without_its_ownership_audit 0
             ai_conversation_with_a_handoff_reason 0 · cancelled_follow_up_that_was_sent 0
             finished_follow_up_still_claimed 0 · open_lead_with_closed_at 0
             closed_lead_without_closed_at 0 · status_move_not_from_the_previous_status 0
             lead_status_disagrees_with_its_last_move 0 · more_than_one_open_lead_per_customer 0
```

The population count is asserted exactly, so an empty sweep fails. "HUMAN with no assignee" is deliberately not an invariant (PD-CRM-3). The same queries are in `docs/RUNBOOK.md` for production.

---

## 8. Mutation Testing

`mutations/run_mutations.py` applies each mutation as exact-once replacements
in the lane-2 worktree. It runs a focused subset first, and a survivor is re-run
against the full 40-file CRM suite. Original bytes were restored and SHA-256
verified after every mutation, and the lane-2 worktree was `git status` clean
afterwards.

Controls: 614 passed / 1 skipped at `52db9d6` (round 1), and 617 passed / 1
skipped at `c5c7f7c` (round 2).

**Round 1 (`52db9d6`):** 64 applied, 60 killed, 4 survived:

* **R20.** The schema offset rule was redundant with the service's for any HTTP test.
* **R25.** The flush before the lead lock was masked by the membership lock's flush.
* **R26.** The dispatch share lock was masked by the test stub's message insert, which also locks the row.
* **M10.** The tool's read-time HUMAN check had become equivalent to the CAS refusal.

**Response (`0779cb6`, `c5c7f7c`):** subject-matching tests were added for R20, R25 and R26. The redundant read behind M10 was removed; its property, "the tool refuses a human-owned conversation", is held by the CAS and killed as R29.

**Round 2 (`c5c7f7c`):** R20, R25, R26 and R29 killed. M10 is inapplicable (0 matches).

| Result | Mutations |
|---|---|
| Original survivors, now killed alone | M08, M17, M21, M22 |
| Remediation, killed | R01–R19 (incl. R11b, R12b, R15b, R18b), R20, R20b, R21–R32, M11r |
| Original properties re-run, killed | M01, M02, M03, M04, M05, M07, M09, M12, M13, M14, M15, M18, M19, M20, M23, M24, M25, M26, M27, M28, M29 |
| Inapplicable | M10 (code removed; property = R29, killed). M06 and M16 from the audit exist only as R04/R06 and R11 now, because the `changed` flag and allow-list line were rewritten; they are counted once, under their R ids |

```text
defined 64 · killed 63 · survived 0 · inapplicable 1   (meaningful survivors = 0)
```

For R16, R18, R26, M02, M03, M23 and M24, the runner captured the first
`ERROR` log line rather than the test name. The verdict comes from pytest's
exit status. The failing test for R26 was confirmed separately:
`test_a_takeover_waits_for_a_nudge_the_sweep_has_decided_to_send`, which fails
because PostgreSQL detects a deadlock between the takeover and the unlocked
dispatch. Full rows are in `mutations/results.tsv`.

---

## 9. Defects the Remediation Itself Surfaced

Recorded because the tests caught them before they shipped:

1. **Autoflush-off sessions and `populate_existing`.** The application's sessions do not autoflush, so a locking re-read would have silently thrown away changes staged earlier in the same unit of work. The invariant sweep caught a QUALIFIED move being lost. Every lock primitive now flushes first. R25 pins this.
2. **`FOR UPDATE` against FK key-shares.** Every child insert takes `FOR KEY SHARE` on its parent, so plain `FOR UPDATE` queued takeovers behind message and tool-execution inserts. The locks are now `FOR NO KEY UPDATE`.
3. **Deadlock between assignment and revocation.** Taking the membership lock before the workspace key-share inverted revocation's order. The fix takes the workspace key-share first.
4. **`updated_at` lazy load after a savepoint flush** in `change_status`: a 500 on status changes, now avoided by flushing only on reopen and refreshing afterwards.

---

## 10. Privacy

The new log lines carry ids, states and reason codes only:
`conversation.handoff_superseded`, `conversation.assignment_stale`,
`follow_up.claim_lost`, `membership.work_released`,
`agent.taken_over_during_tools`. The ownership audit meta carries no reason
text; the test asserts the reason string is absent. No lead email, name, note,
handoff reason or follow-up body was added to any log or audit field.

---

## 11. Score

Weights are the audit's, unchanged.

| Dimension | Weight | Before | After | Weighted | Remaining deduction |
|---|---|---|---|---|---|
| Tenant isolation | 0.12 | 7 | 9.5 | 1.140 | follow-up↔lead same-customer is service-enforced, not a DB key |
| Authorization | 0.07 | 8 | 9 | 0.630 | active-member rule is transactional by design; UI 409 handling unverified |
| Contact identity integrity | 0.05 | 8 | 9 | 0.450 | `wa_id` normalisation is a documented future requirement |
| Conversation ownership | 0.08 | 5 | 8.5 | 0.680 | automatic handoffs unowned with no queue view (CRM-18) |
| Human-over-AI authority | 0.10 | 7 | 9 | 0.900 | accepted Meta-socket residual (CRM-17) |
| Assignment concurrency | 0.07 | 4 | 9 | 0.630 | client behaviour on 409 is deployment-only |
| Handoff idempotency | 0.06 | 5 | 9.5 | 0.570 | — |
| Lead / CRM update integrity | 0.09 | 4 | 9 | 0.810 | `expected_status` optional; tags/custom fields remain whole-value edits (serialised) |
| Follow-up correctness | 0.09 | 5 | 9 | 0.810 | a lease stolen mid-send yields "claim lost" and leaves the outcome to the new holder |
| Member / workspace lifecycle | 0.06 | 6 | 9 | 0.540 | workspace deletion revokes without unassigning (not served) |
| Recovery durability | 0.04 | 8 | 8.5 | 0.340 | no CRM reconciler (none needed) |
| Auditability | 0.07 | 3 | 8.5 | 0.595 | priority changes and human follow-up create/cancel not in `audit_logs` |
| Privacy / logging | 0.03 | 9 | 9 | 0.270 | — |
| Observability | 0.02 | 6 | 7 | 0.140 | no metric for stale-write 409s or lost claims |
| Testing | 0.03 | 5 | 9.5 | 0.285 | — |
| Operational readiness | 0.02 | 6 | 8 | 0.160 | production invariant run pending |
| **Total** | **1.00** | **5.84** | | **8.95** | |

---

## 12. Deployment Verification Backlog (deployment-only)

* Operator UI handling of 409 `stale_assignment`, `stale_lead_status`, `dispatch_in_progress` and `open_lead_exists`, including sending `expected_assigned_to_id`.
* Before 0068: the RUNBOOK pre-deploy CRM queries. The migration refuses crossed rows by name.
* After rollout: the production CRM invariant sweep (RUNBOOK "CRM relational invariants"). Rows from before the remediation (revoked-member assignments, contradictory lead dates) need a person's decision, not an automatic repair.
* Carried unchanged from earlier audits: real Meta inbound media DV-1…DV-8 and production alert delivery.

Nothing locally reproducible is deferred here.

## 13. Future Requirements (not built)

Awaiting-human, mine and unassigned inbox views with counts (CRM-18) ·
assignee notifications · contact edit/merge/delete and their audit · automatic
routing/distribution · workspace time-zone infrastructure · handoff-reason edit
as an explicit audited action · a follow-up↔lead customer key if follow-ups
ever carry the contact.
