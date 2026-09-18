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

All 21 numbered findings are closed. Three of them are closed by the locked product contracts of §3 of the brief rather than by code that could be different (TOOL-14, TOOL-17, TOOL-19); the ledger in §14 says which.

**Verdict: TOOLS / TOOL EXECUTION FINDINGS CLOSED FOR THE CURRENT TOOL SURFACE WITH FINAL DEPLOYMENT VERIFICATION DEFERRED.**

**HIGH-RISK EXTERNAL SIDE-EFFECTING TOOLS ARE NOT YET APPROVED FOR ADDITION UNTIL THE FUTURE IDEMPOTENCY / RECONCILIATION / RISK-APPROVAL REQUIREMENTS ARE IMPLEMENTED.** `tool_executions` is the durable foundation those requirements need; it is not the requirements themselves. §15 states exactly what is still missing.

---

## 2. Repository state

```text
audit baseline HEAD          6058c7456e6e16f29c98817378a549be92e77265
remediation branch           tools-findings-remediation
remediation worktree         E:\wasla-tools-remediation
final frozen HEAD            <FINAL_HEAD>
alembic head                 0064 (was 0063)
```

`E:\wasla` was not modified, reset, cleaned or stashed. The ten pre-existing untracked reports there are untouched; this remediation ran entirely in its own worktree, created with `git worktree add -b tools-findings-remediation E:/wasla-tools-remediation 6058c745…`. The audit report was copied in and committed unchanged as the first commit on the branch, before any production fix, because the repository already tracks its audits that way.

Isolation: a Docker Compose **project** of its own, `wasla-tools-rem-4b2a`, with its own PostgreSQL + pgvector, Redis and MinIO on `127.0.0.1:56841/56842/56843`, and databases partitioned by purpose (`wasla_models`, `wasla_migrations`, `wasla_invariants`, `wasla_dev`). The test runner joins Redis's network namespace, so the suites that hard-code `redis://localhost:6379/*` can only reach this project's Redis.

```text
wasla-tools-rem-4b2a redis run_id     91e6048409dde3e876a3dd04bbda4a23d94107bf
```

The four other stacks running on this host throughout (`wasla-tools-audit-9d3e`, `wasla-rag-rem-7c41`, `wasla-rag-base-7c41`, and the developer stack) were never stopped, flushed or touched. No `FLUSHALL`, no `git reset`, no `git clean`, no `git stash`.

---

## 3. Findings ledger

<!-- LEDGER -->

---

## 4. Blockers

<!-- BLOCKERS -->

---

## 5. Everything else

<!-- REST -->
