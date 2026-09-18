# Wasla — Tools / Tool Execution Findings Remediation

Remediation of `TOOLS_TOOL_EXECUTION_AUDIT.md` (audit id `tools-9d3e`) · Branch `tools-findings-remediation` · Date: 2026-09-18

---

## 1. What this closes

The audit's summary of the subsystem was that it is *safe against the model choosing the wrong thing and weak against the world changing underneath it*, and that nothing durable records what a tool did. Both halves are now closed.

Four tools exist, and none of them can name a tenant, a record, a URL, a recipient, an amount or a model — that was true before this work and is deliberately unchanged. What is new is everything the executor does *around* a call:

* **It looks again.** The workspace, the agent, the conversation, the number and the grant are re-read from the database immediately before every tool call, not once at turn start. A workspace suspended or deleted, an agent switched off, a capability revoked, or a colleague taking the conversation over now stops the tools of the round that follows — which is what `docs/AI_AGENTS.md` had been promising and the code was not doing.
* **It contains what it runs.** The handler executes inside a savepoint and *any* exception is caught, recorded and answered with a fixed sentence. Three classes of argument a model can plausibly emit — a NUL, a lone surrogate, an out-of-range delay — and one ordinary concurrency race each used to end the customer's turn in silence. They are now refused calls inside turns that still reply or hand over.
* **It bounds itself.** Eight calls per response, twelve per turn, one execution per provider call id, and no side-effecting call on the final round.
* **It writes down what happened.** Every call the provider asks for leaves one row in `tool_executions` — the ones that ran, the ones refused before they ran, the ones suppressed as duplicates — tied to the turn, the trigger message, the agent and the conversation.

And the one deferred, customer-visible effect the framework has — the follow-up a tool schedules — no longer leaves a workspace that has stopped being served.

All 21 numbered findings are closed. Three of them are closed by the locked product contracts of §3 of the brief rather than by code that could be different (TOOL-14, TOOL-17, TOOL-19); the ledger in §3 says which.

**Verdict: TOOLS / TOOL EXECUTION FINDINGS CLOSED FOR THE CURRENT TOOL SURFACE WITH FINAL DEPLOYMENT VERIFICATION DEFERRED.**

**HIGH-RISK EXTERNAL SIDE-EFFECTING TOOLS ARE NOT YET APPROVED FOR ADDITION UNTIL THE FUTURE IDEMPOTENCY / RECONCILIATION / RISK-APPROVAL REQUIREMENTS ARE IMPLEMENTED.** `tool_executions` is the durable foundation those requirements need; it is not the requirements themselves. §16 states exactly what is still missing.

---

## 2. Repository state

```text
audit baseline HEAD          6058c7456e6e16f29c98817378a549be92e77265
remediation branch           tools-findings-remediation
remediation worktree         E:\wasla-tools-remediation
final frozen HEAD            d4876557c2cb1f78168697ca2249f2eec0c60bf2   (all code and tests; the report is committed after it, docs only)
alembic head                 0064 (was 0063)
```

`E:\wasla` was not modified, reset, cleaned or stashed. The ten pre-existing untracked reports there are untouched; this remediation ran entirely in its own worktree, created with `git worktree add -b tools-findings-remediation E:/wasla-tools-remediation 6058c745…`. The audit report was copied in and committed unchanged as the first commit on the branch, before any production fix, because the repository already tracks its audits that way.

Isolation: a Docker Compose **project** of its own, `wasla-tools-rem-4b2a`, with its own PostgreSQL + pgvector, Redis and MinIO on `127.0.0.1:56841/56842/56843`, and databases partitioned by purpose (`wasla_models`, `wasla_migrations`, `wasla_invariants`, `wasla_dev`). The test runner joins Redis's network namespace, so the suites that hard-code `redis://localhost:6379/*` can only reach this project's Redis.

```text
wasla-tools-rem-4b2a redis run_id     8b18916b6a18e605aad8a14dea9e483e8ad6261a   (recreated with fresh volumes for the final gates)
```

The four other stacks running on this host throughout (`wasla-tools-audit-9d3e`, `wasla-rag-rem-7c41`, `wasla-rag-base-7c41`, and the developer stack) were never stopped, flushed or touched. No `FLUSHALL`, no `git reset`, no `git clean`, no `git stash`.

---

## 3. Findings ledger

| ID | Severity | Short title | Final status |
| --- | --- | --- | --- |
| TOOL-01 | HIGH | Model-supplied arguments destroy the customer's turn | **CLOSED** |
| TOOL-02 | HIGH | Two turns racing a tool kill one of them | **CLOSED** |
| TOOL-03 | HIGH | Tools run after the workspace or agent stopped being served | **CLOSED** |
| TOOL-04 | HIGH | A tool-scheduled follow-up is delivered after suspension | **CLOSED** |
| TOOL-05 | MEDIUM | Tool authorization is a turn-start snapshot | **CLOSED** |
| TOOL-06 | MEDIUM | `schedule_follow_up` has no human-mode guard | **CLOSED** |
| TOOL-07 | MEDIUM | The handoff tool overwrites a colleague's reason | **CLOSED** |
| TOOL-08 | MEDIUM | No bound on tool calls per response or per turn | **CLOSED** |
| TOOL-09 | MEDIUM | Locks held across a tool's embedding call | **CLOSED** |
| TOOL-10 | MEDIUM | A failing tool logs its SQL parameters | **CLOSED** |
| TOOL-11 | MEDIUM | No tool-level metrics | **CLOSED** |
| TOOL-12 | HIGH | No durable record of a tool execution | **CLOSED** |
| TOOL-13 | LOW | Tool grant/revoke is not audit-logged | **CLOSED** |
| TOOL-14 | LOW | `agent_tools.config` stored and read by nothing | **CLOSED BY LOCKED PRODUCT CONTRACT** (PD-TOOLS-08) |
| TOOL-15 | LOW | Denial log line reports the conversation as `agent_id` | **CLOSED** |
| TOOL-16 | LOW | Published schemas omit the bounds the server enforces | **CLOSED** |
| TOOL-17 | LOW | A trigger-less legacy job re-runs its tools | **CLOSED BY LOCKED LEGACY-JOB CONTRACT** (PD-TOOLS-09) |
| TOOL-18 | LOW | Repeat lead calls write an audit row for nothing | **CLOSED** |
| TOOL-19 | INFO | Final-round tool calls execute with results unread | **CLOSED BY LOCKED FINAL-ROUND CONTRACT** (PD-TOOLS-05) |
| TOOL-20 | LOW | Tool documentation drift | **CLOSED** |
| TOOL-21 | INFO | The takeover guard relies on the identity map | **CLOSED** |
| §31.1–§31.11 | — | Eleven structural test gaps | **CLOSED** (§6) |
| §34 | — | Framework requirements for high-risk tools | **REMAINS A FUTURE REQUIREMENT** (§16) |
| §35 | — | Deployment-only verification | **REMAINS DEPLOYMENT VERIFICATION**, 12 items (§17) |

21 of 21 numbered findings closed: 18 by code and evidence, three by the locked product contracts the brief fixed in advance. No finding is deferred, partially closed or carried forward.

---

## 4. The five blockers

### TOOL-01 — Model-supplied arguments the database or Python refuses destroy the customer's turn

```text
Severity   HIGH (blocker)          Status   CLOSED
```

**Before.** Three classes of value a model can legitimately emit were accepted by the registry, refused by PostgreSQL or by Python, and contained by nothing: a NUL character, a lone surrogate, and a `delay_minutes` large enough to overflow a `timedelta`. Each escaped `_execute` as a `DBAPIError` or an `OverflowError` — neither a `ToolArgumentError` nor a `WaslaError`, which were the only two the executor caught — and became an unhandled worker exception. Past engagement the worker's only honest policy is `NO_RETRY`, so the turn died `ENGAGED`: no reply, no handoff, no explanation, the conversation left in AI mode so nobody was asked to pick it up, the turn charged, and any earlier round's tool writes still committed.

**Root cause.** Two, and both had to be fixed. The registry's `ToolParameter` could express a type and an enum and nothing else, so every length, range and pattern lived downstream in a service — which meant `delay_minutes` was converted to a `timedelta` *before* the bound that would have refused it, and text safety (`app/core/text_safety.storable_problem`, which already existed for the knowledge-upload path) was never applied to a tool argument at all. And `_execute` was written for the two exception types the design anticipated, on the documented assumption that anything else "belongs to the worker" — an assumption that is only true if model output cannot cause one.

**Fix.**

* `ToolParameter` gained `minimum`, `maximum`, `min_length`, `max_length` and `pattern`. They are emitted into the provider schema (TOOL-16) *and* enforced by `validate_arguments`, so one declaration still does both jobs.
* Every string argument goes through `storable_problem` at the boundary. NUL and unencodable text are refused as a `ToolArgumentError` the model can correct; Arabic, RTL marks and emoji are storable text and pass untouched.
* Numeric bounds are checked **before** the value is used for anything, so `10**15` minutes is refused rather than converted.
* `ToolArgumentError` now carries a closed `reason` (`invalid_arguments`, `unsafe_text`, `range_violation`) so the refusal is countable as well as readable.
* `_invoke` runs the handler inside `session.begin_nested()` and catches `Exception` after the two specific classes: the savepoint is rolled back, the execution is recorded `failed` / `internal_error`, a metric is incremented, a fixed sentence goes to the model, and the loop continues. `CancelledError` is a `BaseException` and is deliberately not caught — a worker shutting down is not a tool that failed.
* Nothing from the exception reaches the model: not the message, not the SQL, not the customer's values.

**Files.** `app/agents/registry.py`, `app/agents/orchestrator.py`, `app/db/models/tool_execution.py`.

**Permanent tests.** `tests/integration/test_tool_arguments.py` drives the real `AgentWorker` over ten classes of unusable argument — NUL in a handoff reason, NUL in a lead name, a lone surrogate in a lead interest, NUL in a follow-up body, a delay no `timedelta` can hold, a negative delay, a lead name longer than its column, a follow-up body longer than WhatsApp accepts, a currency code that is not one, and a passage count past the ceiling — and for each asserts a reply was sent, the turn completed `replied`, the execution is recorded `rejected` under the right reason, and no lead, follow-up, mode change or audit row exists. The Arabic/RTL/emoji control asserts the same values are stored exactly as written. `tests/unit/test_agent_registry.py` covers the same boundary directly.

**Runtime proof.** Every case goes through the real worker with providers faked at the transport; the offending argument is asserted present on the provider's wire and the refusal asserted present in the next request's input before any outcome is read.

**Mutation proof.** RM22 (remove text safety), RM23 (remove the numeric bound), RM24 (remove the length bound), TM06 (remove string typing) and TM09 (remove the handoff reason bound) were killed in the first run. RM04 (remove the final containment) **survived** it — no shipped tool can raise an unexpected exception any more, so nothing exercised the layer — and was killed only after `test_tool_failure_containment.py` was added. That test also exposed a real defect (§13).

---

### TOOL-02 — Two turns of one conversation racing a tool kill one of them

```text
Severity   HIGH (blocker)          Status   CLOSED
```

**Before.** A customer writing twice in a few seconds produces two turns, and this product deliberately does not coalesce them (AI-08). Both can reach `record_lead_details` or `schedule_follow_up`; `uq_leads_active_contact` and `uq_follow_ups_pending_conversation` let exactly one insert win, and the loser's `IntegrityError` escaped the tool, aborted the turn's whole transaction and stranded it `ENGAGED`. The data stayed correct. The second customer message was answered by silence.

**Root cause.** Both create paths did a read-then-insert with no savepoint, so the database's correct refusal became an exception with nowhere to go — the same missing containment layer as TOOL-01, meeting a constraint that is doing its job.

**Fix.** `LeadService._create_from_conversation` and `FollowUpService._create_pending` each attempt the insert inside `session.begin_nested()`, catch `IntegrityError`, roll back only that savepoint, and let the caller re-read the winner's row and apply its own intention to it. The unique indexes stay — they are what makes the outcome correct rather than merely uncrashed. For the follow-up the winner is then *rescheduled* with the later call's intention, which is the contract rescheduling has always had: the second call carries newer information.

**Files.** `app/services/lead_service.py`, `app/services/follow_up_service.py`, `app/repositories/follow_up_repository.py`.

**Permanent tests.** `tests/integration/test_tool_concurrency.py`, in two shapes because they prove different halves:

* Two real turns of one conversation, held at the classifier until both have arrived, then released together: both complete, both customers are answered, and exactly one lead / one pending nudge exists.
* The losing path itself, made deterministic. Two turns released together *usually* collide, and "usually" is not a property — so the competitor is a second connection holding an uncommitted conflicting insert, which the turn's own write blocks behind. A real lock wait is observed in `pg_stat_activity` before the competitor commits, so the turn provably lost.

**Runtime proof.** The lock wait is observed; the convergence path is additionally visible as `lead.create_raced` / `follow_up.create_raced` in the captured logs.

**Mutation proof.** RM26 and RM27 (narrow the `except IntegrityError` to an exception nothing raises) were killed by the deterministic losing-side tests. RM03 (remove the per-tool savepoint) survived the first run for the same reason as RM04 and was killed after the containment tests were added.

---

### TOOL-03 — Tools run after the workspace or agent has stopped being served

```text
Severity   HIGH (blocker)          Status   CLOSED
```

**Before.** A workspace suspended for abuse or non-payment, a workspace soft-deleted and inside its retention window, or an agent disabled during an inference did not stop the tools of the round that followed. A lead was written, a follow-up was scheduled, audit rows were recorded — and the *reply* was correctly suppressed, which is precisely what made it invisible. It also falsified `docs/AI_AGENTS.md:89`, which is the sentence an operator reads to decide whether suspension is sufficient during an incident.

**Root cause.** `refusal_now` existed and was asked twice — before the turn was charged and before the reply was sent — and never between. The authorization and lifecycle state a tool acted on was the state as of turn start.

**Fix.** `app/agents/lifecycle.py` was refactored so one indexed read answers two vocabularies: `outcome_for` returns the `TurnOutcome` the worker needs, `tool_refusal_for` returns the finer `ToolExecutionReason` the executor records — finer because an operator counting refused calls wants a suspended workspace told apart from a deleted one. The executor calls it immediately before every call, records `rejected` with the reason, and returns a fixed refusal the model can act on.

**Files.** `app/agents/lifecycle.py`, `app/agents/orchestrator.py`.

**Permanent tests.** `tests/integration/test_tool_authority.py` commits the change from another connection *inside the provider call* — the window in production where an administrator clicks suspend while a model is composing — and asserts the new state landed before reading what the tools did. Per lifecycle change: two `rejected` executions with the right reason, zero leads, zero follow-ups, zero audit rows, zero sends, and the documented turn outcome.

**Mutation proof.** RM01 (remove the per-call lifecycle re-read) and TM25 (serve a suspended workspace) — both killed. TM25 is worth noting: the audit killed it with tests asserting the *reply* was suppressed, which is why a killed mutation was not a covered property. It is now killed by tests asserting no *tool* ran.

---

### TOOL-04 — A tool-scheduled follow-up is delivered to a customer after the workspace is suspended

```text
Severity   HIGH (blocker)          Status   CLOSED
```

**Before.** `FollowUpService.dispatch` re-read the conversation's closure, its mode, the customer's opt-out, the template's standing and the service window — everything except the workspace's own lifecycle. So a nudge an agent scheduled was delivered to a real customer from a workspace Wasla had stopped serving. This is the one place a tool's effect was both customer-visible and detached from every check the AI path performs.

**Root cause.** The follow-up path does not go through the orchestrator, and the lifecycle predicate lived on the AI path only.

**Fix.** Two guards, in the order that makes the second necessary (PD-TOOLS-06):

1. `WorkspaceService.suspend` and the soft-delete path call `FollowUpService.cancel_agent_follow_ups`, so the pending agent nudges stop waiting and a colleague can see nothing is queued against their customers. Only agent-created nudges: a colleague's own follow-up is their work.
2. `dispatch` reads `tenants.status` and `tenants.deleted_at` as columns immediately before every send and skips with a recorded reason. This one is authoritative, because a transition landing after the sweep has claimed a row cannot be caught by the first.

Suppression is **terminal**, not postponed: a nudge stopped during a suspension must not arrive weeks later as a surprise if the workspace is restored.

**Files.** `app/services/follow_up_service.py`, `app/services/workspace_service.py`, `app/repositories/follow_up_repository.py`.

**Permanent tests.** `tests/integration/test_tool_deferred_effects.py`. The assertion that matters is `sends == 0`, counted at a stub standing where Meta stands — a guard that recorded the right status *after* asking Meta to deliver would satisfy a status assertion and fail the customer. Non-vacuity: the nudge is asserted pending and due first. The negative control (an active workspace still sends) is not optional, because every guard here is satisfiable by never sending anything.

**Mutation proof.** RM14 (remove the dispatch gate) and RM15 (remove the cancellation) — both killed.

---

### TOOL-12 — There is no durable record of a tool execution

```text
Severity   HIGH (blocker)          Status   CLOSED
```

**Before.** A tool left a trace only when it both succeeded *and* mutated something. A refusal, a rejected argument, a lifecycle denial, a duplicate suppression and a call whose turn then died were indistinguishable from one another and from a call that was never requested; the audit's sweep found three stranded turns holding committed agent effects with nothing tying the two together. `audit_logs` was keyed by conversation, so "which turn did this" was unanswerable.

**Fix.** `tool_executions` (migration 0064), one row per call the provider asks for. It carries the turn, the trigger message, the agent and the conversation; the provider's call id; the round and the position within the response; a closed `state` and `reason_code`; and `requested_at` / `authorized_at` / `started_at` / `finished_at`.

Three decisions worth stating:

* **Identity is server-generated.** `id` is this platform's own key and is the seed a future external tool would derive a provider idempotency key from. The provider's call id is recorded and is not trusted as an identity beyond the turn it arrived in — which is exactly what the partial unique index `(tenant_id, agent_turn_id, provider_call_id) WHERE state <> 'duplicate'` says. The exclusion matters: a suppressed repeat shares the identity by definition, and a constraint that refused to record it would make the table unable to hold the very thing it exists to show.
* **Never the arguments.** `argument_fields` carries the argument *names* and nothing else, the same rule `audit_logs.meta` follows (ADR-052).
* **Transactional honesty.** The record is staged in the turn's own session and flushed *before* the handler's savepoint opens, so a savepoint rollback cannot erase the evidence of the failure that caused it; a success is marked in the same transaction as the business mutation, so both commit together or neither does. The write itself sits in a savepoint of its own, so bookkeeping can never be what costs a customer their turn — and a record whose write failed is expunged and never rewritten, because the statement it would repeat is the one that just failed.

`ToolExecutionState.AMBIGUOUS` is unreachable from today's four tools and exists anyway: a state added at the moment it is first needed is a migration in the middle of an incident.

**Files.** `app/db/models/tool_execution.py`, `app/repositories/tool_execution_repository.py`, `alembic/versions/20260917_0064_tool_executions.py`, `app/agents/orchestrator.py`, `app/workers/ai_worker.py`, `app/repositories/agent_turn_repository.py`, `app/services/workspace_purge_service.py`.

**Permanent tests.** `tests/integration/test_tool_execution_records.py` drives one turn through four terminal states and asserts each row's linkage, timestamps and argument shape — including that a denied call was never authorised and that a terminal row always says when it finished. The invariant sweep (§12) adds fourteen more.

**Mutation proof.** RM05 (never create the record) and RM06 (drop the link to the turn) — both killed.

---

## 5. The other sixteen findings

### TOOL-05 — Tool authorization is a turn-start snapshot, so a revoked grant still executes

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** The grant set was read once, before the provider was called, and the execution-time check tested membership of that snapshot. So an administrator revoking a capability did not stop the calls of the turn already running — and an inference is long enough (up to three rounds of three attempts of sixty seconds) for that window to matter.

**Fix.** `_grant_refusal` reads the grant row for the tool being called, with `populate_existing=True` so the identity map's turn-start copy is overwritten by what the database holds now (PD-TOOLS-07). A missing grant is `not_granted`; a disabled one is `tool_disabled`; they are different rows in the record because they are different incidents. A turn with no agent at all is refused, which is the safe direction: a tool running for nobody is a tool nobody authorised. The `granted` snapshot is still checked first, because it catches a forged name without a database round trip.

**Files.** `app/agents/orchestrator.py`, `app/repositories/agent_repository.py`.

**Tests.** `test_a_grant_revoked_during_the_inference_stops_the_call` revokes the grant *inside* the provider call, asserts zero enabled grants remain before reading anything, and asserts the execution is `rejected` / `tool_disabled`, no lead exists, no audit row exists — and that the customer is still answered, because a refused tool is not a lost turn.

**Mutation proof.** RM02 (remove the fresh read) and TM03 (execute disabled grants — an audit survivor) were killed. TM01 (remove the turn-start snapshot check) survived the first run because the fresh read also refuses forged names; it was killed after `test_a_capability_granted_during_the_inference_is_not_usable_in_that_turn` made the two disagree.

---

### TOOL-06 — `schedule_follow_up` has no human-mode guard

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** The tool checked `CLOSED` and never `HUMAN`, so an AI nudge could be planted on a conversation a colleague had just taken over — including inside the same model response, immediately after the handoff that had cancelled the nudges that existed. `dispatch` would skip it while the conversation stayed human, but the row waited, and handing the conversation back to the AI released it.

**Fix.** Three layers, because they fail at different moments. `FollowUpService.schedule` refuses an `AGENT`-created follow-up on a `HUMAN` conversation (a colleague scheduling one on a conversation they own is the ordinary way to use the feature and is untouched). The executor's per-call lifecycle read refuses it earlier. And a successful handoff stops the rest of its own response (PD-TOOLS-02), so the `[handoff, schedule_follow_up]` shape cannot arise at all.

**Tests.** `test_a_colleagues_takeover_outranks_every_stale_tool_call` and `test_a_handoff_that_succeeds_stops_the_rest_of_its_own_response` in `test_tool_authority.py`; `test_an_agent_cannot_schedule_a_nudge_on_a_human_owned_conversation` and its colleague control in `test_tool_deferred_effects.py`.

**Mutation proof.** RM11 (let the response continue after a handoff) and RM12 (remove the human-mode guard) — both killed.

---

### TOOL-07 — The handoff tool overwrites a colleague's reason and duplicates handoff records

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** Called on a conversation a colleague had taken over during the inference, the tool set `HUMAN` again, **overwrote the colleague's own note about why they took it** with the model's sentence, wrote an `AGENT_HANDOFF_REQUESTED` row for a handover the agent did not perform, and reported the turn as `handed_off` rather than `suppressed_human`. Two concurrent turns produced two audit rows and two analytics events for one handover.

**Fix.** Human ownership outranks a stale AI decision (PD-TOOLS-01). The executor refuses the call with `conversation_human`, so nothing is written and the turn resolves `suppressed_human` through the existing takeover re-read. The tool itself keeps a second guard for any future caller that does not come through the executor, and raises rather than returning a string — a string would read as a successful handoff and file the turn as `handed_off`, claiming the agent did something a colleague had already done. Duplicate analytics were already guarded by `set_mode`'s `changed` check; what was missing was stopping the agent path from reaching it.

**Tests.** `test_a_colleagues_takeover_outranks_every_stale_tool_call` asserts the colleague's reason survives verbatim, no agent audit row exists, no handoff analytics event exists, and the turn ends `suppressed_human`. Two invariants in the sweep count duplicate handoff rows and duplicate handoff analytics events across the whole database.

**Mutation proof.** RM13 survived the first run — the executor refuses first, so the tool's own guard was never reached — and was killed after `test_the_handoff_tool_refuses_a_conversation_a_colleague_already_owns` drove the tool directly.

---

### TOOL-08 — No bound on tool calls per response or per turn

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** One response carrying eighty calls executed all eighty: forty embedding requests, forty audit rows, one lead rewritten forty times, and the next provider request grown from 2,452 to 30,522 bytes. The practical ceiling was the model's output-token budget and a 1 MiB body limit — accidents, not decisions. Two calls sharing one `call_id` both executed.

**Fix.** `MAX_TOOL_CALLS_PER_RESPONSE = 8` and `MAX_TOOL_CALLS_PER_TURN = 12` (PD-TOOLS-03), server constants that no provider output can override. **Every** call the provider asks for is charged against both, including the ones about to be refused for a bad argument or a missing grant, so a malformed call cannot buy a bypass. Past the limit the call is recorded with a bounded reason and a fixed refusal, and no handler runs.

A provider call id claimed once in a turn is claimed for that turn (PD-TOOLS-04). The claim is taken as soon as the record exists, not on success, so a call that was refused cannot come back under the same id and run. The partial unique index is the backstop behind the in-memory set.

**Tests.** `test_tool_execution_records.py`: forty calls in one response (eight run, the rest recorded against the two caps, one lead); eight calls in each of three rounds (twelve run, the rest against the turn cap); two calls sharing `call_id` (one succeeded, one `duplicate`, one follow-up, one audit row). Two invariants count over-budget turns and over-budget responses database-wide.

**Mutation proof.** RM07, RM08, RM09 — all killed.

---

### TOOL-09 — Connection and row locks held across a tool's embedding call

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** `search_knowledge` following a write in the same model response made its embeddings HTTP call with a checked-out connection, an open transaction and a `RowExclusiveLock` on `leads` — for up to three attempts with backoff. That is the ADR-080 bottleneck the project deliberately removed from the inference path, reappearing inside the tool path, and the held lock blocks the very concurrent turn TOOL-02 is about.

**Fix.** `RetrievalService.search` gained `release_session`, off by default because a request handler's unit of work must not be committed underneath it half way through — the release *is* a commit. The agent tool turns it on. What is staged at that point is a finished tool's work, which is exactly the condition `released` documents for its callers. The executor knows not to wrap a releasing tool in a savepoint, because a savepoint open across the release would defeat it; such a tool contains its own database failures in a nested transaction of its own, which `search_knowledge` always has.

**Tests.** `tests/integration/test_tool_transactions.py` measures from inside the fake embeddings provider, on a connection of its own: with a write first in the same response, `idle in transaction` and `leads RowExclusiveLock` are both zero while the HTTP call is outstanding. Non-vacuity: the embedding call is asserted to have been reached and the lead asserted written.

**Mutation proof.** RM16 — killed.

---

### TOOL-10 — A failing tool logs its SQL parameters, leaking customer content

```text
Severity   MEDIUM          Status   CLOSED
```

**Before.** `AgentWorker._attempt` logged `agent.job_failed` with `logger.exception`, the JSON formatter included the formatted exception, and the engine was built without `hide_parameters` — so SQLAlchemy's `[parameters: (…)]` block landed in the log store carrying a handoff reason, a customer's name on a lead, and a follow-up body. That is the content the audit trail is explicitly built never to copy (ADR-052), reaching the log store by a different route, with a different retention story, on precisely the failures an operator greps.

**Fix.** `hide_parameters=True` on the engine, and the worker's job-failure line now carries the exception class rather than the formatted exception. What is lost is the values in a debugging session; what identifies the bug — the statement, the constraint name, the SQLSTATE — is still there. The executor's own `agent.tool_crashed` line carries safe structured metadata only.

**Tests.** `tests/integration/test_tool_observability.py` uses sentinels and proves they *reached the database as bound parameters* before asserting they did not reach the logs — a test that only asserted absence would pass against a tool that never ran. A second test covers the failure path specifically and asserts no `parameters:` block anywhere in the captured stream.

**Mutation proof.** RM17 survived the first run: with argument text refused at the boundary and the worker no longer formatting exceptions, no current path puts a database error into a log, so turning the setting off changed nothing observable. `test_a_database_error_from_this_application_quotes_no_parameters` asserts the setting itself on a real unique violation, and killed it.

---

### TOOL-11 — No tool-level metrics

```text
Severity   MEDIUM          Status   CLOSED
```

**Fix.** `wasla_agent_tool_executions_total{tool,outcome}` and `wasla_agent_tool_execution_duration_seconds{tool}`, both in the Redis catalogue the scrape reads — a counter written to a hash nobody reads is a metric that does not exist. Both label domains are closed: `tool` comes from the deployment's own registry and a name the model invented is counted as `unknown`, because a label domain a stranger's message can extend is a cardinality leak. Six outcomes: `succeeded`, `rejected`, `denied`, `failed`, `duplicate`, `ambiguous`. No tenant, conversation, turn or call id appears anywhere near either metric.

Two alerts, both ratios so a quiet workspace and a busy one alert at the same badness, both with `for: 30m`. `AgentToolFailures` counts calls that *broke* and deliberately excludes refusals — a model asking for a tool it was not granted is the system working. `AgentToolDenials` catches a capability revoked while agents still expect it, or a conversation talking a model into naming tools it does not have. `docs/RUNBOOK.md` gains the procedure and the queries.

**Tests.** Catalogue membership; emission by tool and outcome against real Redis, including the label-hygiene assertion that no identifier appears in any field; and the `unknown` fallback for an invented name. `promtool check rules` and `promtool test rules` cover the alerts.

**Mutation proof.** RM18 — killed.

---

### TOOL-13 — Granting or revoking a tool is not audit-logged

```text
Severity   LOW          Status   CLOSED
```

**Fix.** `AGENT_TOOL_GRANTED` / `AGENT_TOOL_REVOKED`, written against the authenticated administrator, carrying the agent, the tool and the state the grant was left in — and never the grant's stored settings, which are a payload somebody typed rather than a decision. A grant written with `enabled=False` is recorded as a withdrawal: the row on the table is what matters, not which endpoint reached it.

**Tests.** `tests/integration/test_tool_grant_administration.py`, including that a row never lands in another workspace.

**Mutation proof.** RM19 — killed.

---

### TOOL-14 — `agent_tools.config` is stored, documented as policy, and read by nothing

```text
Severity   LOW          Status   CLOSED BY LOCKED PRODUCT CONTRACT (PD-TOOLS-08)
```

The column stays — it is the right shape for the first tool that needs a per-grant knob, and dropping it would be migration churn for nothing — but the pretence goes. A non-empty value is refused at the boundary with a message saying the field is reserved, rather than accepted and ignored; an empty object is still accepted so a client that always sends the field is not broken. The model docstring and `docs/AI_AGENTS.md` now say "reserved and not applied".

**Tests.** `test_per_grant_settings_are_refused_rather_than_silently_ignored`, plus the empty-object control.

---

### TOOL-15 — The tool-denial log line reports the conversation id as `agent_id`

```text
Severity   LOW          Status   CLOSED
```

The one signal that a model reached for a capability it does not have pointed an investigator at an agent that does not exist. It now logs `conversation_id` under its own name and the real agent id beside it, because the executor knows which agent is answering.

**Tests.** `test_the_denial_log_line_names_the_conversation_it_is_about` asserts both fields on the captured record, and asserts the denial genuinely happened first.

---

### TOOL-16 — Published tool schemas omit the bounds the server enforces

```text
Severity   LOW          Status   CLOSED
```

`ToolParameter` carries the bounds and `json_schema()` emits them, so the model is told in schema what it was previously told in prose. The aligned bounds are exactly those the brief names: `search_knowledge.max_results` 1..10, `schedule_follow_up.delay_minutes` 1..43,200, `request_human_handoff.reason` ≤ 200, `schedule_follow_up.message` ≤ 4,096, `.reason` ≤ 300, `budget_currency` `^[A-Z]{3}$`, and the lead text fields at their storage-safe column widths. `query` is bounded at the 2,000 characters the retrieval service was already trimming to.

`strict` remains unset, which is the existing documented decision and is untouched: it would require every property to be required, which would force the deliberately optional lead fields to be sent as nulls and lose the distinction between "not mentioned" and "empty".

**Tests.** `test_the_published_schema_carries_the_bounds_the_server_enforces` asserts the *serialized* schema, because that is the artefact the provider reads, and `test_the_schema_still_refuses_arguments_nobody_declared` asserts `additionalProperties: false` survived the change. Eight parametrised cases assert the server refuses what the schema declares, so the schema is not the only thing holding.

**Mutation proof.** RM25 (stop publishing the bounds), RM23/RM24 (stop enforcing them), TM08 (drop the top-k clamp), TM09 (drop the handoff bound) — all killed.

---

### TOOL-17 — A trigger-less legacy job re-runs its tools on redelivery

```text
Severity   LOW          Status   CLOSED BY LOCKED LEGACY-JOB CONTRACT (PD-TOOLS-09)
```

A turn's identity is the inbound message it answers. A job naming none has no way to tell a redelivery from a second customer message, so the same job delivered twice ran two inferences, scheduled the same nudge twice, wrote two audit rows and sent two replies. It now fails closed: the refusal happens before anything is claimed, charged or called, and the envelope is dead-lettered with the queue's existing `MALFORMED` / `NO_RETRY` route — retrying would refuse identically for ever. No synthetic identity is invented, because one derived from the envelope changes on redelivery and would prove nothing.

Every enqueue site in this build sets a trigger id, so the only jobs this can affect are ones an older build left in the queue across a deploy. **Draining that queue is a deployment item** (§17).

**Tests.** `tests/integration/test_tool_semantics.py`: a trigger-less job runs no inference, no tool, sends nothing and leaves no execution record; delivered twice it still does nothing twice; and a keyed job is the control that still answers, because the refusal above is otherwise satisfiable by answering nobody.

**Mutation proof.** RM20 — killed.

---

### TOOL-18 — Repeat lead calls write an audit row even when nothing changed

```text
Severity   LOW          Status   CLOSED
```

`LeadService.capture_from_conversation` now returns a `LeadCapture` — the row, whether it was created, and which fields actually moved — and the tool writes `AGENT_LEAD_RECORDED` only when something moved. The service already computed the changes; it simply did not tell the caller.

**Tests.** Five identical calls in one response: five successful executions, one audit row, one lead. The control asserts three calls that each change something still write three rows.

**Mutation proof.** RM21 — killed.

---

### TOOL-19 — Tool calls on the final round execute with their results unread

```text
Severity   INFO          Status   CLOSED BY LOCKED FINAL-ROUND CONTRACT (PD-TOOLS-05)
```

On the final round only a terminal tool runs, and `request_human_handoff` is the only terminal tool: handing over *is* the ending, so it is the one thing still worth doing when nothing can read a result. Everything else is recorded `rejected` / `round_limit`. No embedding is paid for, no CRM row is written, and no customer message is planned on grounds the model never got to reason about.

**Tests.** A three-round turn whose last response asks for a lead write and a follow-up: both recorded `round_limit`, no follow-up exists, and the round count is asserted first. The control asserts a handoff on the final round still hands over.

**Mutation proof.** RM10 — killed.

---

### TOOL-20 — Tool documentation drift

```text
Severity   LOW          Status   CLOSED
```

Three statements in `docs/AI_AGENTS.md` were false, and they are the reference an operator reads to decide what suspension guarantees. The code is fixed, so the prose is now true, and it says what else now bounds a tool call, what `tool_executions` records, that `agent_tools.config` is reserved rather than applied, that `schedule_follow_up` ships, and that no high-risk external tool may be added yet. `docs/OBSERVABILITY.md` gains the two metrics; `docs/RUNBOOK.md` gains the alert procedure, the queries and the new log lines.

---

### TOOL-21 — The lead tool's takeover guard depends on the identity map dropping its snapshot

```text
Severity   INFO          Status   CLOSED
```

The guard worked, and it worked because nothing held a strong reference to the conversation across the inference, so the identity map had usually dropped it and the re-`select` genuinely re-read. That is a property of garbage collection, not of the code. `LeadService._fresh_conversation` now reads with `populate_existing=True` and says why; `FollowUpService.schedule` does the same; and the executor's own lifecycle read is column-based, which is the technique `app/agents/lifecycle.py` already documented.

**Tests.** `test_the_lead_tool_re_reads_a_conversation_it_is_already_holding` loads the conversation into a session and deliberately keeps the instance alive — exactly what a refactor might do — commits a takeover from another connection, and asserts the service raises and that the very instance the session was holding was refreshed to `HUMAN`.

**Mutation proof.** RM28 (`populate_existing=False`) survived the first run, because the executor's per-call read catches the takeover before the service is reached; the test above was added and killed it.

---

## 6. The eleven structural test gaps (§31 of the audit)

| # | Gap | Closed by |
| --- | --- | --- |
| 1 | No test drives a tool against state that changed *during* the inference | `test_tool_authority.py` — every lifecycle change, the grant revocation and the takeover are committed from another connection inside the provider call, and asserted landed before any outcome is read |
| 2 | No test drives two turns of one conversation concurrently through a tool | `test_tool_concurrency.py` — two real turns held at the classifier and released together, plus a deterministic losing-side pair with an observed lock wait |
| 3 | No test feeds a tool an argument the database will refuse | `test_tool_arguments.py` — NUL, lone surrogate, over-range integer, over-long string, bad pattern, over-count, each through the real worker; `test_agent_registry.py` at the boundary |
| 4 | Argument bounds are untested through the tool | `test_agent_registry.py` — the serialized schema, eight refusal cases, and the top-k clamp as the tool uses it |
| 5 | "Granted" vs "enabled" is untested at execution | `test_a_grant_revoked_during_the_inference_stops_the_call`; TM03 now killed |
| 6 | No test bounds tool calls per response, asserts a tool metric, or asserts a durable per-execution record | `test_tool_execution_records.py`, `test_tool_observability.py` |
| 7 | No test asserts a failing tool does not log its parameters | `test_a_tools_values_never_reach_the_log_stream`, `test_a_failing_tool_publishes_no_parameters` |
| 8 | No test covers a deferred tool effect crossing a lifecycle change | `test_tool_deferred_effects.py` |
| 9 | Tool attribution is untested | `test_a_nudge_an_agent_scheduled_is_recorded_as_the_agents`; TM23 now killed |
| 10 | Blank-means-absent is untested | `test_a_blank_lead_field_leaves_the_stored_value_alone`; TM27 now killed |
| 11 | A killed mutation is not a covered property (TM25) | TM25 is now killed by tests asserting no *tool* ran, not only that the reply was suppressed |

---

## 7. Product decisions applied (§33 of the audit, §3 of the brief)

| Decision | Applied as |
| --- | --- |
| PD-TOOLS-01 human ownership wins | Per-call lifecycle read refuses every tool on a `HUMAN` conversation; the handoff tool is a no-op that leaves the colleague's reason; the follow-up service refuses an agent-created nudge |
| PD-TOOLS-02 stop after a successful handoff | The response's remaining calls are recorded `handoff_completed` and not run |
| PD-TOOLS-03 call bounds | `MAX_TOOL_CALLS_PER_RESPONSE = 8`, `MAX_TOOL_CALLS_PER_TURN = 12`, server constants, every requested call counted |
| PD-TOOLS-04 duplicate provider call ids | Claimed when the record is created, suppressed as `duplicate`, backed by a partial unique index |
| PD-TOOLS-05 final round | Only a terminal tool runs; the rest are `round_limit` |
| PD-TOOLS-06 suspension and pending automation | Cancelled at the transition, refused again at dispatch, terminally |
| PD-TOOLS-07 current grant is authoritative | `populate_existing` grant read per call |
| PD-TOOLS-08 `agent_tools.config` reserved | Non-empty value refused; docs corrected; column retained |
| PD-TOOLS-09 trigger-less legacy jobs | Fail closed and dead-letter; drain listed as a deployment item |
| PD-TOOLS-10 future quotas / approvals | Not implemented, deliberately; §16 states what they must be |

---

## 8. What was explicitly *not* changed

The properties the audit found sound were left alone, and the mutation matrix re-proves them:

* No tool declares, accepts or infers a tenant, workspace, conversation, lead, agent or user id.
* Dispatch is an exact-name dictionary lookup; no dynamic import, `eval`, reflection, generic HTTP or SQL tool.
* Tool output reaches the model only inside `function_call_output`, and application decisions are taken from `ToolExecution.succeeded` rather than from the output text.
* `additionalProperties: false` on every tool schema; `true` is still not an integer.
* The three-round loop cap, the retrieval bounds, the human-verified lead field protection, the two uniqueness constraints, turn identity and message idempotency.
* `strict` remains unset, for the documented reason.

No new tool was added, and no existing tool gained an argument.

---

## 9. Runtime reproductions of the blockers, after the fix

Each of these is the audit's own probe, rebuilt as a permanent test against the real `AgentWorker` with the two outbound hosts faked at the transport.

| Audit probe | What it did then | What it does now | Test |
| --- | --- | --- | --- |
| P06 — NUL, lone surrogate, over-range delay | turn `ENGAGED`, 0 outbound, envelope dead-lettered, no redelivery | tool call `rejected` with a closed reason; the customer is answered; the turn completes `replied` | `test_an_argument_the_server_cannot_use_costs_the_call_and_not_the_turn` (10 cases) |
| P06 control — Arabic, RTL mark, emoji | stored correctly | stored correctly, byte for byte | `test_arabic_rtl_and_emoji_are_stored_exactly_as_the_model_wrote_them` |
| P07 — two turns racing the lead tool | 1 lead, 1 reply, **the losing turn died `ENGAGED`** | 1 lead, 2 replies, both turns `completed` / `replied` | `test_two_turns_racing_the_lead_tool_both_answer_the_customer` |
| P07 — two turns racing the follow-up tool | 1 pending nudge, 1 reply, losing turn died | 1 pending nudge, 2 replies, both turns completed | `test_two_turns_racing_the_follow_up_tool_both_answer_the_customer` |
| P07 — the losing side, deterministically | — | the turn's write blocks behind an uncommitted competitor (lock wait observed), converges when it commits, and answers its customer | `test_a_lead_write_that_loses_its_race_costs_the_call_and_not_the_turn`, and the follow-up twin |
| P02 — suspended / soft-deleted / agent disabled mid-inference | lead written, follow-up scheduled, 2 audit rows | 2 `rejected` executions, 0 leads, 0 follow-ups, 0 audit rows, 0 sends | `test_a_workspace_that_stopped_being_served_mid_inference_runs_no_tool` (3 cases) |
| P01 — grant revoked mid-inference | tool executed, lead written, audit row written, customer replied | `rejected` / `tool_disabled`, 0 leads, 0 audit rows, customer still replied | `test_a_grant_revoked_during_the_inference_stops_the_call` |
| P03 — colleague takes over mid-inference | lead refused; follow-up scheduled anyway; handoff overwrote the colleague's reason | all three `rejected` / `conversation_human`; the colleague's reason intact; turn `suppressed_human` | `test_a_colleagues_takeover_outranks_every_stale_tool_call` |
| P05 — `[handoff, schedule_follow_up]` in one response | a fresh nudge planted on the conversation just handed over | the follow-up recorded `handoff_completed` and not run; no nudge | `test_a_handoff_that_succeeds_stops_the_rest_of_its_own_response` |
| P08 — 40+ calls in one response | all executed | eight run, the rest recorded against the two caps | `test_a_response_asking_for_far_too_many_tools_runs_only_the_allowance` |
| P09 — two calls sharing one `call_id` | both executed | one `succeeded`, one `duplicate`; one follow-up; one audit row | `test_one_provider_call_id_is_one_execution` |
| P10 — search after a write in one response | `checked_out=1`, `idle in transaction=1`, `leads RowExclusiveLock=1` across the embedding call | 0, 0 and 0, measured from inside the provider | `test_a_search_after_a_write_holds_no_connection_across_the_embedding_call` |
| P11b — trigger-less job delivered twice | 2 inferences, 2 audit rows, 2 replies | 0 inferences, 0 sends, dead-lettered both times | `test_a_trigger_less_job_redelivered_still_does_nothing_twice` |
| P12 — follow-up due after suspension | **message sent to the customer** | 0 sends, terminally skipped; and cancelled at the transition before it is ever due | `test_a_workspace_that_is_not_served_sends_no_automated_message` |
| TOOL-12 — every execution has a record tied to its turn | no record existed | four terminal states in one turn, each carrying turn, trigger message, agent, conversation and timestamps | `test_every_call_leaves_a_record_tied_to_the_turn_that_asked` |
| (new) a handler that stages a row and raises | not reachable before: nothing contained it | the row is rolled back to the call's savepoint, the earlier tool keeps its work, the customer is answered | `test_a_tool_that_raises_after_writing_loses_its_row_and_not_the_turn` |

---

## 10. Non-vacuity

Every absence asserted above is paired with a presence, because the failure mode of a test asserting "zero" is passing against a path that was never taken.

| Claim | The presence that makes it evidence |
| --- | --- |
| a lifecycle change stopped the tools | the new workspace / agent state is read back and asserted **inside** the provider call, before any outcome is read |
| a revoked grant stopped the call | zero enabled grants asserted, from a second connection, before the tool ran |
| a takeover stopped the tools | the conversation is read back as `HUMAN` with the colleague's reason before any outcome is read |
| both turns raced | both are held at the classifier until both arrive; the count is asserted |
| a write provably lost its race | a real lock wait is observed in `pg_stat_activity` while the competitor's insert is still uncommitted |
| a duplicate `call_id` was suppressed | both rows' `provider_call_id` are asserted equal, so the id genuinely arrived twice |
| the call budget was exceeded | the number of recorded executions is asserted greater than the cap |
| the final round was reached | the inference count is asserted to be three |
| the embedding call held nothing | the provider is asserted to have been entered, and the preceding write asserted to have produced a lead |
| the sentinels never reached the logs | they are first asserted present on the lead row and the follow-up row, so they genuinely were bound parameters |
| a careless tool ran | the handler's own call counter is asserted |
| the trigger-less job was delivered | the number of envelopes the worker consumed is asserted |
| a follow-up was due | its status is asserted `pending` and its `scheduled_at` asserted in the past |
| the invariant sweep had something to sweep | eleven populations are asserted non-empty, and the failure message names whichever is not |

---

## 11. Authoritative gates

Run from the frozen HEAD, on recreated resources, with a clean tree. Counts from different revisions are never mixed: every number below comes from this one.

| Gate | Result |
| --- | --- |
| `ruff check app tests` | All checks passed (exit 0) |
| `black --check app tests` | 549 files would be left unchanged (exit 0) |
| `mypy app tests` | Success: no issues found in 549 source files |
| `alembic heads` | `0064 (head)` — one head |
| `alembic upgrade head` from an empty database | clean, `0063 -> 0064` last |
| `alembic check` | `No new upgrade operations detected.` |
| `alembic downgrade 0063` then `upgrade head` | `0064 -> 0063`, `current` = `0063`; `0063 -> 0064`, `current` = `0064 (head)` |
| **Model-built, whole `tests/`** | **4,815 passed, 16 skipped, 0 failed** in 959 s (audit baseline 4,695 / 15) |
| **Migration-built `tests/integration` + `tests/e2e`** | **2,406 passed, 2 skipped, 0 failed** in 935 s (audit baseline 2,313 / 1) |
| Tool-targeted suite (the mutation matrix's positive control) | **365 passed, 1 skipped, 0 failed** in 166 s — 22 files (the audit's positive control was 436 over a different 23-file set) |
| Kept-data run + tool invariant sweep | **95 passed, 0 skipped** — the non-vacuity test ran; 25 invariants, 0 violations |
| `promtool check rules` | `SUCCESS: 34 rules found` |
| `promtool test rules` | `SUCCESS` |

**Skips, all intended.** Model-built: 11 × `tests/real_provider/test_openai_contract.py` (no `OPENAI_API_KEY`), 3 × `test_schema_parity.py` (migration-built only), and the two kept-data sweeps (`test_ai_invariants.py`, `test_tool_invariants.py`) which only run when a session keeps its data. Migration-built: the same two sweeps. The one skip more than the audit's baseline in each run is the new tool sweep's own presence check.

**Earlier runs are discarded, not counted.** Two full gate runs preceded this one and are not reported as results: one at `96874a7`, superseded when three tests were added after its mutation matrix (§13), and, before it, a migration-built run that reported 2,342 passed and **64 skipped** - the skips were the object-store suites: the runner passed MinIO's settings under the wrong variable names (`WASLA_TEST_S3_*` rather than `TEST_S3_ENDPOINT_URL` and friends), so every test that needs a real object store skipped itself. That is an infrastructure mistake of this remediation, not a result, and both suites were re-run from scratch with the corrected runner; the numbers above are those re-runs.

**The kept-data sweep excludes suites that cannot share a database.** `test_ai_security.py`'s `_audit` helper reads the whole `audit_logs` table, unchanged since the audit baseline, so it fails when earlier suites deliberately leave agent audit rows behind. It passes in both authoritative runs and in the targeted run; the sweep is fed by the worker-driven suites, which are the ones that commit data.

**Targeted suite.** 22 files: the two agent unit files, the eleven new `test_tool_*.py` files, and the AI, lead, follow-up, authorization-hardening and RAG-turn suites that exercise the tool path.

### Migration 0064

| Check | Result |
| --- | --- |
| fresh `base` → `head` | clean |
| `alembic check` at head | `No new upgrade operations detected.` |
| `downgrade 0064` → `0063` | clean; `current` = `0063` |
| `upgrade` again → `0064` | clean; `current` = `0064 (head)` |
| single head | `0064 (head)` |
| upgrade of a populated `0063` database (agent turns, audit logs, agents, tool grants, leads, follow-ups) | seeded one row in each of `agent_turns`, `audit_logs`, `agents`, `agent_tools`, `leads`, `follow_ups`; after the upgrade every count is unchanged and `tool_executions` holds **0** rows |

The migration invents no history: turns that ran before it keep no execution rows, because there is no evidence to reconstruct them from.

---

## 12. Database invariant sweep

`tests/integration/test_tool_invariants.py`, 25 invariants over the whole database, run against the migration-built schema after a kept-data run of the tool suites.

**Presence first.** The non-vacuity test asserts eleven populations are non-empty before any violation is counted, and names whichever is not:

```text
executions             138   (rejected 79 · failed 4 · duplicate 1 · succeeded 54, derived from the total)
denied (grant)         7
lifecycle refusals     9
bounded refusals       46
distinct reasons       17
distinct tools         8
agent leads            15
agent follow-ups       6
agent handoffs         4
conversations with two turns   2
```

**Violations.**

```text
25 invariants          25 × 0 violations
```

The sweep covers: tenant scope on every execution record and its links to turn, conversation and agent; the record's own lifecycle (a success that never finished, a terminal state with no finish time, a call that ran without being authorised, a refusal that was nonetheless started, a closed row with no reason); the argument-shape rule; one logical execution per provider call of a turn, and that a duplicate did nothing; both call budgets; what a tool may leave behind (a pending AI nudge on a human conversation or in an unserved workspace, more than one pending nudge, duplicate handoff rows, duplicate handoff analytics, a handoff row on a conversation still answered by the AI, an agent audit row pointing outside its workspace); and that no turn is stranded holding a tool's committed effect.

---

## 13. Mutation testing

Each mutation is an exact single-occurrence replacement in one file, compiled, run against a **subject-matching** selection of tests (named per mutation, so a failure elsewhere cannot be mistaken for a kill), then restored and SHA-256-verified. The runner is `E:\wasla-tools-rem-4b2a\mutations\mutate.py`; it is outside the repository.

**56 mutations**: 28 new ones for the remediation (RM01–RM28) and **all 28 of the audit's own** (TM01–TM28), including its six survivors.

### How the result was reached — reported as it happened

This was **not** a clean kill in one run, and the history is the useful part.

| Run | Revision | Applied | Killed | Survived | What happened next |
| --- | --- | --- | --- | --- | --- |
| 1 | `0f68a23` | 41 (28 RM + 13 TM) | 32 | **9** — RM03, RM04, RM13, RM17, RM28, TM01, TM08, TM23, TM28 | a test added per survivor; **one exposed a real defect** (below) |
| 1b | after the fix and tests | 9 (the survivors) | 9 | 0 | — |
| 2 | `96874a7` | 56 (all 28 TM) | 53 | **3** — TM14, TM20, TM22 | a test added per survivor |
| 2b | after the tests | 3 | 3 | 0 | — |
| **3 (final)** | **`d487655`** | **56** | **56** | **0** | **full matrix kill**, every file restored and SHA-256-verified |

Why the survivors survived is the same story twelve times: **the remediation added a layer in front of an older one, and the older one stopped being reached.** The registry now refuses bad text and out-of-range numbers before a handler runs, and the services contain their own integrity errors — so no shipped tool can raise an unexpected exception, and nothing exercised the executor's savepoint (RM03) or its catch-all (RM04). The executor re-reads ownership before the handoff tool runs, so the tool's own guard (RM13) and the lead service's fresh read (RM28) were never reached. With the argument refused at the boundary, the search tool's own clamp (TM08) and the snapshot grant check (TM01) became second lines. Convergence on a unique-index collision makes the lead and follow-up pre-reads (TM20, TM22) an optimisation rather than a correctness condition. None of those is a reason to delete the older layer — each is what holds when the newer one is wrong — so each got a test that reaches it directly.

**The defect RM03/RM04's tests found.** The test registry's careless tool stages a row and raises. The executor contained the exception, and then the *turn died anyway*: the execution record had been marked `started` inside the savepoint, the rollback expired it, and the next attribute read was a lazy load with no greenlet to run in (`MissingGreenlet`). A containment layer that loses the turn on its own bookkeeping would have shipped. Fixed in `ab52255` by marking and flushing the record before the savepoint opens.

**One mistake of the runner's, and its effect.** The container writes the source files back with LF endings while the checkout uses CRLF, so after a run `git status` lists the mutated files as modified. `git diff` is empty for every one of them — the content is byte-identical in normalised form, which is what the runner's SHA-256 check compares — and a `git checkout -- app/` restores the checkout form. The tree was clean before the final gates and is recorded clean after them.

### Final run, per mutation

| ID | Origin | Property removed | Result | Killer (subject-matching) |
| --- | --- | --- | --- | --- |
| RM01 | remediation | the per-call lifecycle re-read | killed | `test_a_workspace_that_stopped_being_served_mid_inference_runs_no_tool[workspace` |
| RM02 | remediation | the per-call grant re-read | killed | `test_a_grant_revoked_during_the_inference_stops_the_call` |
| RM03 | remediation | the per-tool savepoint | killed | `test_a_tool_that_raises_after_writing_loses_its_row_and_not_the_turn` |
| RM04 | remediation | the final unexpected-exception containment | killed | `test_a_tool_that_raises_after_writing_loses_its_row_and_not_the_turn` |
| RM05 | remediation | creating the durable execution record | killed | `test_a_response_asking_for_far_too_many_tools_runs_only_the_allowance` |
| RM06 | remediation | the execution record's link to its turn | killed | `test_every_call_leaves_a_record_tied_to_the_turn_that_asked` |
| RM07 | remediation | duplicate provider call ids are suppressed | killed | `test_one_provider_call_id_is_one_execution` |
| RM08 | remediation | the per-response call cap | killed | `test_a_response_asking_for_far_too_many_tools_runs_only_the_allowance` |
| RM09 | remediation | the per-turn call cap | killed | `test_a_response_asking_for_far_too_many_tools_runs_only_the_allowance` |
| RM10 | remediation | the final-round rule | killed | `test_the_final_round_runs_no_tool_whose_answer_nothing_can_read` |
| RM11 | remediation | a handoff stops the rest of its response | killed | `test_a_handoff_that_succeeds_stops_the_rest_of_its_own_response` |
| RM12 | remediation | the follow-up tool's human-mode guard | killed | `test_an_agent_cannot_schedule_a_nudge_on_a_human_owned_conversation` |
| RM13 | remediation | the handoff tool's own-ownership guard | killed | `test_the_handoff_tool_refuses_a_conversation_a_colleague_already_owns` |
| RM14 | remediation | the follow-up dispatch lifecycle gate | killed | `test_a_workspace_that_is_not_served_sends_no_automated_message[suspended-suspended-False]` |
| RM15 | remediation | cancelling AI nudges when a workspace stops being served | killed | `test_suspending_a_workspace_cancels_the_ai_nudges_it_is_holding` |
| RM16 | remediation | the session release around a tool's embedding call | killed | `test_a_search_after_a_write_holds_no_connection_across_the_embedding_call` |
| RM17 | remediation | hiding database parameters | killed | `test_a_database_error_from_this_application_quotes_no_parameters` |
| RM18 | remediation | the tool execution metric | killed | `test_every_call_is_counted_by_tool_and_by_outcome` |
| RM19 | remediation | the grant/revoke audit row | killed | `test_granting_a_tool_is_recorded_against_the_person_who_granted_it` |
| RM20 | remediation | refusing a trigger-less legacy job | killed | `test_a_job_with_no_trigger_message_runs_nothing_at_all` |
| RM21 | remediation | recording a lead audit row only when something changed | killed | `test_a_lead_call_that_changed_nothing_writes_no_audit_row` |
| RM22 | remediation | text safety at the tool boundary | killed | `test_text_the_database_cannot_hold_is_refused_at_the_boundary[request_human_handoff-arguments0]` |
| RM23 | remediation | the numeric bound check | killed | `test_a_value_outside_a_published_bound_is_refused[search_knowledge-arguments1]` |
| RM24 | remediation | the string length bound | killed | `test_a_value_outside_a_published_bound_is_refused[request_human_handoff-arguments5]` |
| RM25 | remediation | publishing the bounds in the provider schema | killed | `test_the_published_schema_carries_the_bounds_the_server_enforces` |
| RM26 | remediation | converging on a raced lead insert | killed | `test_a_lead_write_that_loses_its_race_costs_the_call_and_not_the_turn` |
| RM27 | remediation | converging on a raced follow-up insert | killed | `test_a_follow_up_that_loses_its_race_costs_the_call_and_not_the_turn` |
| RM28 | remediation | the freshness of the lead tool's takeover read | killed | `test_the_lead_tool_re_reads_a_conversation_it_is_already_holding` |
| TM03 | audit survivor | only enabled grants authorise execution | killed | `test_a_grant_revoked_during_the_inference_stops_the_call` |
| TM06 | audit survivor | string type validation | killed | `test_a_text_argument_that_is_not_text_is_refused[request_human_handoff-arguments0]` |
| TM08 | audit survivor | the max_results clamp inside the tool | killed | `test_the_tool_clamps_a_count_even_when_nothing_validated_it` |
| TM09 | audit survivor | the handoff reason bound | killed | `test_the_published_schema_carries_the_bounds_the_server_enforces` |
| TM23 | audit survivor | a follow-up an agent scheduled is attributed to the agent | killed | `test_a_nudge_the_tool_scheduled_is_recorded_as_the_agents` |
| TM27 | audit survivor | blank tool text means absent, not a clear | killed | `test_a_blank_lead_field_leaves_the_stored_value_alone[]` |
| TM01 | audit killed | the execution-time grant check | killed | `test_a_capability_granted_during_the_inference_is_not_usable_in_that_turn` |
| TM10 | audit killed | a handoff is taken from the execution, not the name | killed | `test_a_model_naming_an_ungranted_handoff_is_still_answered` |
| TM11 | audit killed | a handoff ends the loop | killed | `test_a_granted_handoff_hands_over_and_sends_nothing` |
| TM19 | audit killed | the lead tool's human-mode guard | killed | `test_the_lead_tool_re_reads_a_conversation_it_is_already_holding` |
| TM21 | audit killed | human-verified fields are protected from the agent | killed | `test_extraction_does_not_overwrite_what_a_person_entered` |
| TM25 | audit killed | a suspended workspace is refused | killed | `test_a_suspended_workspace_makes_no_provider_call_and_sends_nothing` |
| TM28 | audit killed | an unknown name never falls back to another tool | killed | `test_a_grant_naming_a_tool_this_build_does_not_implement_runs_nothing` |
| TM02 | audit killed | offer only granted tools | killed | `test_an_agent_without_the_grant_is_never_offered_it` |
| TM04 | audit killed | unknown-argument refusal | killed | `test_an_invented_argument_is_refused` |
| TM05 | audit killed | required-argument check | killed | `test_a_missing_required_argument_is_refused` |
| TM07 | audit killed | bool rejected where an integer is wanted | killed | `test_true_is_not_accepted_as_a_whole_number` |
| TM12 | audit killed | 3-round loop cap | killed | `test_a_model_that_only_ever_asks_for_tools_is_not_silent` |
| TM13 | audit killed | tool output kept out of instructions | killed | `test_retrieved_text_reaches_the_model_only_as_tool_output` |
| TM14 | audit killed | domain tool failure contained as a domain failure | killed | `test_a_domain_refusal_reaches_the_model_in_its_own_words` |
| TM15 | audit killed | argument refusal contained | killed | `test_a_rejected_argument_becomes_output_the_model_can_read` |
| TM16 | audit killed | lead audit row written | killed | `test_recording_a_lead_names_the_fields_but_never_their_values` |
| TM17 | audit killed | audit meta carries shapes, not values | killed | `test_recording_a_lead_names_the_fields_but_never_their_values` |
| TM18 | audit killed | handoff audit written only after the handoff | killed | `test_the_handoff_tool_refuses_a_conversation_a_colleague_already_owns` |
| TM20 | audit killed | lead resolved from the contact, not created blind | killed | `test_an_ordinary_repeat_takes_the_update_path_not_the_race_path` |
| TM22 | audit killed | a follow-up is rescheduled rather than queued twice | killed | `test_an_ordinary_repeat_takes_the_update_path_not_the_race_path` |
| TM24 | audit killed | mid-turn takeover re-read before replying | killed | `test_a_conversation_taken_over_during_the_turn_is_not_answered` |
| TM26 | audit killed | lead repository tenant predicate | killed | `test_one_workspace_cannot_read_another_workspaces_lead` |

Meaningful survivors at the frozen HEAD: **0**. Inapplicable: **0**. `git diff` after the run: empty.

---

## 14. Reliability & safety score

The audit's own dimensions and weights, recomputed. Its published figure was **6.3**; adding up its own table gives 6.42, so the "before" column below sums to 6.4 and the difference is rounding in the original, not a disagreement about any dimension.

| Dimension | Weight | Before | After | What the remaining deduction is |
| --- | --- | --- | --- | --- |
| Execution authorization | 0.10 | 7 | **9** | Grant and lifecycle are re-read from the database before every call, and forged or spoofed names are still refused. Authorization is binary: there is no risk classification and no approval path, which the first high-risk tool will need (§16). |
| Tenant isolation | 0.10 | 10 | **10** | Unchanged, and re-proved. The new table is tenant-scoped, carries the composite foreign key to its conversation, and is swept for cross-tenant links. |
| Argument authority | 0.09 | 6 | **9** | Length, range and pattern declared once, published to the provider and enforced server-side; unstorable text refused at the boundary. No cross-field validation and no per-grant policy — the second is deliberate (TOOL-14). |
| Idempotency | 0.09 | 6 | **8** | Turn identity, one execution per provider call id, a durable server-generated execution identity, and a capture that changes nothing writes nothing. No provider-side idempotency key and no reconciliation, because no tool yet calls a provider that accepts one. |
| Crash / replay safety | 0.08 | 6 | **8** | A crashing tool no longer loses the turn; a trigger-less replay is refused. Committed-then-lost remains the shape when a *provider* fails on a later round — an accepted, documented trade of ADR-080, not a defect. |
| Transaction correctness | 0.07 | 6 | **9** | Per-tool savepoint; the execution record flushed outside it so a failure cannot erase its own evidence; no connection and no row lock held across any provider call on the tool path. The turn is still deliberately not one transaction. |
| External side-effect safety | 0.07 | 5 | **8** | The one deferred customer-visible effect is gated at the transition and again at the send, terminally. There is still no framework for an irreversible external effect. |
| Retry semantics | 0.05 | 8 | **8** | Unchanged and still correct. No per-tool retry budget, which nothing needs while no tool is retryable. |
| Tool-loop control | 0.05 | 5 | **9** | Rounds capped, calls capped per response and per turn, duplicates suppressed, the final round restricted to a terminal tool. The caps are global constants rather than per-plan or per-tool. |
| Prompt authority separation | 0.05 | 10 | **10** | Unchanged; results still reach the model only as `function_call_output` and decisions are still taken from what the server did. |
| Failure containment | 0.06 | 4 | **9** | Every exception a handler can raise is contained and the turn converges to a reply or a handoff. A record whose own write fails is reported and lost rather than guaranteed — bounded, logged and alertable. |
| Auditability | 0.05 | 6 | **9** | An execution record for every requested call, linked to its turn; capability changes audited; no-op rows and the false handoff row gone. `audit_logs` rows are not linked to `tool_executions` by a column; the sweep joins them through the conversation. |
| Observability | 0.04 | 3 | **9** | Two metrics with closed label domains, two tested alerts, runbook procedures and queries. Alert *delivery* is deployment verification. |
| Secret handling | 0.04 | 7 | **9** | The proved leak is closed at the engine and at the call site. A driver's own logger could still be configured to echo statements in a deployment; that is configuration, checked at deployment. |
| Testing | 0.04 | 5 | **9** | Eleven structural gaps closed, all six audit survivors killed, an invariant sweep with presence proofs, and a deterministic proof of the losing side of the race. Multi-host and real-provider properties remain untestable here. |
| Operational readiness | 0.02 | 5 | **8** | Runbook procedures for every new failure mode. The legacy-queue drain is an open deployment item. |

**Weighted total: 6.4 → 8.8 / 10.**

Every remaining deduction is either a future-capability requirement (§16) or a deployment-verification item (§17). None of them is an open finding.

---

## 15. Final verdict

Every condition the brief sets for the verdict holds at the frozen HEAD:

| Condition | State |
| --- | --- |
| all current findings closed | 21 of 21 (§3) |
| all blocker reproductions pass | TOOL-01, -02, -03, -04, -12 (§9) |
| meaningful mutation survivors | 0 of 56 (§13) |
| database invariant violations | 0 of 25, against non-empty populations (§12) |
| authoritative gates | green (§11) |

**TOOLS / TOOL EXECUTION FINDINGS CLOSED FOR THE CURRENT TOOL SURFACE WITH FINAL DEPLOYMENT VERIFICATION DEFERRED.**

**HIGH-RISK EXTERNAL SIDE-EFFECTING TOOLS ARE NOT YET APPROVED FOR ADDITION UNTIL THE FUTURE IDEMPOTENCY / RECONCILIATION / RISK-APPROVAL REQUIREMENTS ARE IMPLEMENTED.**

The framework is not payment- or refund-safe because today's four tools are closed. What closed them is a durable execution identity, a per-call authority read, containment and bounds — the foundation a financial tool needs, not the controls it needs (§16).

---

## 16. Future high-risk tools — what is still missing

None of `create_payment_link`, `charge`, `refund`, `create_order`, `update_order`, `create_booking`, `cancel_booking`, generic CRM mutation, campaign or outreach sends, outbound webhooks, email or calendar actions exists, and none was added here. The audit's framework table, restated against the code as it now stands:

| Requirement | State after this remediation |
| --- | --- |
| Execution-time authorization against current grants and lifecycle | **present** — re-read per call (TOOL-03, TOOL-05) |
| Tenant binding with every sensitive identifier server-derived | **present** — unchanged and re-proved |
| Typed, strictly bounded input schema published to the provider | **present** — length, range and pattern declared, published and enforced (TOOL-01, TOOL-16) |
| Business-rule validation in a service, not in the tool | **present** |
| Durable logical execution identity, distinct from the turn | **present** — `tool_executions.id`, server-generated (TOOL-12) |
| Idempotency key propagated to the external provider | **absent** — the seed exists; nothing derives, sends or stores a provider-side key, because no tool calls a provider that accepts one |
| Reconciliation for an ambiguous outcome | **absent** — `AMBIGUOUS` can be *expressed* and nothing produces, sweeps, ages or resolves it. There is no reconciler, no backlog metric, no alert on the age of the oldest unresolved execution |
| Per-tool savepoint so one failed call cannot lose the turn's other work | **present** (TOOL-01, TOOL-02) |
| Bounded retry with an explicit ambiguous class never auto-retried | partial — at the turn level; there is still no per-tool retry budget, and there would need to be one for a tool that can be retried at all |
| Bounded calls per response and per turn | **present** (TOOL-08) |
| Result serialization bounds per tool | **present** |
| Audit trail covering request, authorization, outcome and external effect | **present** for the first three; nothing records an external effect, because none exists |
| Metrics and alerts per tool | **present** (TOOL-11) |
| Risk classification with confirmation or approval for financial and irreversible actions | **absent** — no policy engine, no approval state, no amount limits, no recipient allow-listing. `agent_tools.config` is the reserved shape for the first of these and is deliberately inert |
| SSRF controls for any URL-bearing argument | not applicable — no tool takes a URL, a host, a scheme, a method or a header; `build_guarded_client` is the hook when one does |
| Secrets never reachable from a tool's arguments, results or errors | **present**, and the log leak is closed (TOOL-10) |

**The framework could now host a reversible, internal, side-effecting tool.** It could not host a financial one. What a payment or refund tool needs and does not have is the *second half* of idempotency: a key the provider honours, a state in which "we asked and do not know" is recorded before the request goes out, a sweep that resolves it afterwards, and a limit and an approval on the amount. `tool_executions` gives the durable identity those are built on and is not a substitute for them.

**HIGH-RISK EXTERNAL SIDE-EFFECTING TOOLS ARE NOT YET APPROVED FOR ADDITION UNTIL THE FUTURE IDEMPOTENCY / RECONCILIATION / RISK-APPROVAL REQUIREMENTS ARE IMPLEMENTED.**

---

## 17. Final deployment verification backlog

Items that need a deployed environment, a sandbox credential or more than one host. None of them is a locally reproducible defect: every defect this audit found is closed above.

| # | Item | Why it cannot be settled here |
| --- | --- | --- |
| 1 | Real Responses API behaviour with many parallel tool calls, and whether `parallel_tool_calls` changes the per-response count | provider-side behaviour |
| 2 | Real provider behaviour when a `call_id` is repeated within one response, and when a duplicate result is echoed back | provider-side acceptance |
| 3 | Whether declaring `minimum`/`maximum`/`maxLength`/`pattern` measurably reduces violations, and whether the provider honours them | provider behaviour; the server bound is what holds either way |
| 4 | Real provider adherence to `additionalProperties: false` under injection pressure, re-confirmed per model version | provider behaviour; recorded as an observation, never relied upon |
| 5 | A tool call whose `arguments` string is invalid JSON, echoed verbatim in the next request | provider tolerance |
| 6 | Real WhatsApp suppression of a tool-scheduled follow-up after suspension | needs the Meta sandbox |
| 7 | Multi-host races: two workers on different machines, one conversation, TOOL-02's shape | single-host probes cannot prove the distributed case |
| 8 | Distributed worker crash with a partially committed tool round | needs real process kills across hosts |
| 9 | Alertmanager delivery for `AgentToolFailures` and `AgentToolDenials` | needs the production route |
| 10 | Secret injection: confirming no tool path sees credentials in the deployed environment | environment-specific |
| 11 | Production egress restrictions for the embeddings call made inside a tool | network policy |
| 12 | **Drain, or prove the absence of, trigger-less agent jobs in the queue before or during the rollout** | a live queue; these are now dead-lettered rather than run (TOOL-17), which is correct and is still work an operator must see |

Item 12 is the one that is new, and it is the deliberate cost of PD-TOOLS-09. The check is `LLEN agent:jobs:pending` plus an inspection for envelopes carrying no `trigger_message_id`; any found should be drained or re-derived from durable state by `InboundRecoveryWorker` rather than replayed.

---

## 18. Commits

```text
d487655 test(tools): hold the layers the remediation made partly redundant
96874a7 test(tools): kill the mutations the first matrix left standing
ab52255 fix(tools): mark a call started before its savepoint, not inside it
0f68a23 fix(tools): record a call that carried no arguments as an absence
7b926f0 test(tools): read the audit trail's own column names in the invariant sweep
22be360 docs(tools): say what the tool layer actually does now
d813937 test(tools): sweep the database for what a tool must never leave behind
137a824 fix(audit): record who changed what an agent is able to do
c2c0483 feat(observability): count agent tool calls, and alert on failures and denials
52dd415 fix(logging): keep database parameters out of the log stream
84e455e fix(followups): stop the AI's messages when a workspace stops being served
78a6fd5 fix(tools): look again before every call, and contain what the call does
ca4a81f fix(tools): bound what a model may send, and converge when two turns race
3d81788 feat(tools): record every tool call in a durable execution table
36aa357 docs(tools): record the independent tool-execution audit
<report commit>  docs(tools): record the findings remediation and how each was proved
```

Each is a coherent unit rather than a file. `app/agents/registry.py` carries several findings at once — an argument's bounds, a handler's ownership rule and a handler's change reporting are one contract about what the four tools do — so it lands in one commit whose message names each finding, rather than being split into commits that would each leave the tree inconsistent. The executor's commit is the same shape for the same reason: a per-call authority read, a savepoint, a call budget and an execution record are one method's worth of one decision.

Three things in this history are mistakes of the remediation's own, recorded rather than rewritten. `0f68a23` also swept in a placeholder skeleton of this report through a `git add -A`; the report commit supersedes it, and nothing depends on the skeleton. And two commits fix mistakes this remediation made rather than ones the audit found, and they are kept separate and honest rather than squashed away: the invariant sweep first read the ORM's column names instead of the database's, and the execution record first stored "no arguments" as the JSON literal `null` rather than as an absence. The second was found by the kept-data sweep, which is the point of running one.

---

## 19. Stop

This remediation is complete and **not merged**. `tools-findings-remediation` is a branch in its own worktree at `E:\wasla-tools-remediation`; `worktree-billing-google-auth` is untouched and awaits an explicit merge instruction.

Nothing was started beyond it: no Media audit, no second Tools audit.
