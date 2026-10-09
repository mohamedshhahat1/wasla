# Wasla Canonical Merge Verification — Omnichannel, Entitlements, Platform Operations, Kept-Data Fix

Integration gate for four locally committed stages into the canonical branch
`worktree-billing-google-auth`:

1. the omnichannel final findings remediation;
2. entitlements and channel capacity (ADR-131);
3. migration 0091 and platform entitlement operations (ADR-132);
4. the kept-data sweep and AI invariants fix for F-1 (ADR-133).

This is the second attempt. The first (2026-10-09, report `b1431bf` on branch
`canonical-merge-verification-20261009`) stopped on F-1 and merged nothing.

Run 2026-10-09 on fresh, disposable infrastructure. No result, container or
database of the implementation stages or of the first attempt was reused. Nothing
was deployed, `main` was not touched, no real provider was contacted, nothing was
force-pushed or rewritten.

## 1. Executive Summary

Every gate passed on `KD_HEAD` (`e50a275`) and again on the merged canonical
commit `ccfe9e8`, whose tree is byte-identical to `e50a275`.

| Gate | Result |
|---|---|
| Billing step | every billing branch already in canonical; nothing unreviewed |
| Ancestry | `origin ⊂ canonical ⊂ 2c58c35 ⊂ a7fc8b5 ⊂ fe12a37 ⊂ e50a275`; `b1431bf` not an ancestor |
| Existing migrations | `downgrade()` only, in 0075, 0078, 0079, 0081 (vs canonical) and 0091 (vs the omnichannel head); every `upgrade()` byte-identical |
| Static | ruff, black (901), mypy app tests (789), mypy scripts (11): clean |
| Alembic | empty → 0100 in 100 steps; check clean; catalogue 0/0/0; preflight ok; 44 invariants at 0 |
| Refused downgrades | 0090, 0069, 0099 each refused; stamp 0100; 309 indexes identical and valid |
| Preflight probes | missing index → exit 1, named; invalid index → exit 1, "index invalid" |
| Populated upgrade from origin | 0081 → 0100, 19 steps, 7.5 s; 49 tables, 373 rows, **0 rows lost**, **0 reductions** |
| Model-built lane | 6,819 / 6,803 / 0 failed / 16 skipped (known set) |
| Migration-built lane | 6,819 / 6,807 / 0 failed / 12 skipped (known set), after two recorded infrastructure runs |
| Kept-data sweep (F-1) | 3 / 3 runs 300 / 300 / 0 / 0 with `ci.yml`'s exact selection; **F-1 closed** |
| Every CI job | 5 / 5 run locally and green; GitHub-only items listed |
| Races | 7 suites × 3 runs, all green |
| Invariants / oracle | E01..E18 at 0 everywhere; oracle 168 comparisons in the suite, 0 mismatches |
| Mutation spot-check | 21 / 21 killed, 21 restored |
| APP-E2E | V-1..V-9 pass; 0 secrets found |
| Merges | four `--no-ff` merges, no conflict, each tree equal to its incoming head |
| Final canonical tree | `ccfe9e8`: static, Alembic, families, full migration-built lane (6,807 / 0 / 12) and kept sweep (300 / 300) green again |

**Verdict: CANONICAL MERGE VERIFIED AND PUSHED**

## 2. Source Branches and Exact Heads

| Item | Value |
|---|---|
| `CANONICAL_START_HEAD` | `3473068` (`worktree-billing-google-auth`, `E:\wasla`) |
| `ORIGIN_CANONICAL_START_HEAD` | `354db53` (canonical 12 ahead, 0 behind) |
| `OMNICHANNEL_HEAD` | `2c58c35` (`omnichannel-final-remediation-20261002`), Alembic 0091 |
| `ENTITLEMENTS_HEAD` | `a7fc8b5` (`entitlements-channel-capacity-20261002`), Alembic 0098; `43751f1` is not on the branch |
| `PLATFORM_OPS_HEAD` | `fe12a37` (`platform-entitlement-ops-20261008`), Alembic 0100 |
| `KD_HEAD` | `e50a275` (`kept-data-invariants-20261009`), Alembic 0100 |
| First attempt | `b1431bf` on `canonical-merge-verification-20261009`: evidence of method only; never merged |
| `ORIGIN_MIGRATION_HEAD` | `0081` |
| Stage branches on origin | none of the four; each 0 behind its predecessor |
| Working trees | `E:\wasla`: 11 untracked user documents, no tracked change, no stash. Stage worktrees clean. |
| Python / Docker / Compose | 3.12.7 / Engine 29.8.0 / Compose v5.5.1 |
| PostgreSQL / Redis | 16.15 (`pgvector/pgvector:pg16`) / 7.4.11 |
| `app.__file__` | asserted in every run: `E:\wasla-merge-verify\…`, `E:\wasla-merge-lane\…`, `E:\wasla-merge-mut\…`, `E:\wasla-merge-origin\…`, `E:\wasla-merge-final\…` |

Every stage head was the newest commit of its branch. None had commits beyond
those recorded by the stage reports.

Infrastructure, this stage only:

| Container | Port | Use |
|---|---|---|
| `wasla-mv-pg` | 56461 | lanes, kept sweeps, races, families, final gates |
| `wasla-mv-pg2` | 56462 | Alembic gates, populated upgrade, APP-E2E, mutations |
| `wasla-mv-redis` / `-redis2` | 56471 / 56472 | lanes (`WASLA_TEST_REDIS_HOST` / `TEST_REDIS_URL`) |
| `wasla-mv-redis3` | 56473 | helpers (populate, E2E, mutations, races) |
| `wasla-mv-minio` | 59461 | `wasla-media` bucket created before the first suite ran |

The `wasla-kd-*` containers (56361-56373, 59361) were left running and never
used. Ports were chosen after listing every listener.

## 3. The Billing Step

| Branch | Head | In canonical | In `KD_HEAD` | Status |
|---|---|---|---|---|
| `annual-billing` | `68e9afb` | yes | yes | ALREADY IN CANONICAL |
| `billing-findings-remediation` | `c8f85bd` | yes | yes | ALREADY IN CANONICAL |
| `custom-plans-topups` | `39836ef` | yes | yes | ALREADY IN CANONICAL |
| `final-merge-annual` | `b2ad821` | yes | yes | ALREADY IN CANONICAL |

The billing step is the 12 local canonical commits origin lacks
(`75fbe51`..`3473068`, `aa11098` among them). They reach origin with this push.
No branch is NOT INCLUDED.

## 4. Ancestry and Commits Landing

| Link | Holds | merge-base |
|---|---|---|
| `origin/worktree-billing-google-auth` ⊂ canonical | yes | `354db53` |
| canonical ⊂ `2c58c35` | yes | `3473068` |
| `2c58c35` ⊂ `a7fc8b5` | yes | `2c58c35` |
| `a7fc8b5` ⊂ `fe12a37` | yes | `a7fc8b5` |
| `fe12a37` ⊂ `e50a275` | yes | `fe12a37` |
| `canonical-merge-verification-20261009` ⊂ `e50a275` | **no** (correct) | — |

Commits landing (`3473068..e50a275`): **83**. By stage: omnichannel 29,
entitlements 37, platform operations 9, kept-data fix 8. Plus 4 merge commits.
Diff: 282 files changed, +36,907 / −1,221.

## 5. Migration Changes

- Canonical head 0081 → 0100. 16 files added: 0085..0100.
- `downgrade()`-only edits, proven by AST comparison of every top-level function:

| File | Compared | `upgrade()` | Changed functions |
|---|---|---|---|
| 0075 | `3473068` → `e50a275` | identical | `downgrade` |
| 0078 | `3473068` → `e50a275` | identical | `downgrade` |
| 0079 | `3473068` → `e50a275` | identical | `downgrade` |
| 0081 | `3473068` → `e50a275` | identical | `downgrade` |
| 0091 | `2c58c35` → `e50a275` | identical | `downgrade` |

0091 is new relative to canonical; its edit happened between the omnichannel
head and the platform stage (`b83df88`), so it is compared from `2c58c35`.
0085..0090 and 0092..0098 are unchanged after their own stage.

Stage 4: `git diff fe12a37 e50a275 -- alembic/` and `-- app/` are both empty.
It adds scripts, tests and documents only, as its report states.

## 6. Independent Static and Alembic Gates

On `E:\wasla-merge-verify` (`e50a275`), on `wasla-mv-pg2`:

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `black --check .` | 901 files unchanged |
| `mypy app tests` / `mypy scripts` | no issues in 789 / 11 files |
| `alembic heads` | `0100 (head)`, single |
| empty → head | 100 steps, 39.9 s (beside the migration lane; 19 s alone, §13) |
| `alembic check` | No new upgrade operations detected |
| catalogue | 0 NOT VALID, 0 invalid indexes, 0 disabled triggers, stamp 0100 |
| `db_preflight verify` | ok, exit 0 |
| `omnichannel_invariants verify` | ok; 44 checks, all 0 (E01..E18 among them) |
| head → 0091 → head | 9 down (0091's index present at 0091), 9 up; check clean; catalogue 0/0/0; 309 indexes identical to the fresh head |

Refused downgrades. Each seed was written by SQL in this stage, and each downgrade
ran through the Alembic CLI.

| Seed | Command | Refused by | Stamp after | Indexes after | Preflight |
|---|---|---|---|---|---|
| Instagram connection with `sends_per_minute = 10` | `downgrade 0089` | 0090, after 10 steps ran | 0100 | 309 identical, valid; 0091's valid | ok |
| stored card (`payment_methods` row) | `downgrade 0068` | 0069, after 32 steps ran | 0100 | 309 identical, valid; `uq_users_email_lower` and all 14 indexes named by 0075/0078/0079/0081/0091 valid | ok |
| withdrawn platform grant (`topup_purchases`) | `downgrade 0098` | 0099 ("topup_purchases withdrawn by staff: 1") | 0100 | 309 identical; 0100's `ix_audit_logs_target_type_target_id_occurred_at` valid | ok |

Preflight probes:

- `ix_conversations_tenant_id_channel_last_message_at` dropped by hand (0 rows in
  `pg_indexes`) → exit 1, `index missing:
  conversations.ix_conversations_tenant_id_channel_last_message_at`. Recreated → ok.
- Marked `indisvalid = false` → exit 1, `index invalid: …`. `REINDEX` → ok,
  catalogue 0/0/0.

Documentation truth: `test_documentation_truth` passed in both full lanes and in
the final families. README states `0001`–`0100`.

## 7. Populated Upgrade from Origin

`ORIGIN_MIGRATION_HEAD = 0081`. `wasla_mv_upgrade` was built by `alembic upgrade
head` in `E:\wasla-merge-origin` (`354db53`): 81 steps, 20 s.

It was populated through the origin revision's own API under uvicorn and its own
workers. Meta, OpenAI and Paymob were faked at the httpx transport. The populate
harness is the first attempt's, recovered from its scratch directory, read in
full and reused unchanged apart from ports.

| Data | How (origin code) |
|---|---|
| 3 workspaces (staff, Alpha, Beta); owners, 1 admin, 2 members | `POST /auth/register`, `/invitations`, `/invitations/accept` |
| Alpha on `pro` with a stored card; Beta on free `starter` at its 1-number limit (2nd number → 402) | `/billing/checkout` + signed TRANSACTION and TOKEN callbacks |
| recurring renewal on the stored card | real `BillingWorker` → MOTO intention + pay; signed callback → 4 payments succeeded |
| 5 WhatsApp numbers with sealed tokens, 1 disabled | `POST /whatsapp/accounts`, `/disable` |
| grants (`whatsapp_numbers` +1, `period_ai_turns` +50); paid top-ups (number +1, 1,000 messages) | staff grant route; staff product creation; `/billing/topups/{id}/checkout` + callback |
| agents, 2 knowledge bases, 4 documents and their chunks | API + real `IngestionWorker` (4 jobs) |
| 8 contacts and conversations; inbound text and image, outbound text, template, media, AI replies | signed webhook; real `MediaWorker` (8) and `AgentWorker` (16 jobs) |
| usage events, sentiments, media rows | side effects of the above |
| opt-outs (team, customer); one campaign through the real `CampaignWorker` | `/contacts/{id}/opt-out`, `/campaigns` |
| audit log | side effect |

Before the upgrade, Alpha's owner read `whatsapp_numbers`: base 3, top-up 1,
grant 1, effective 5, used 3. Beta's: 1 / 0 / 0, effective 1, used 1.

No origin process was alive at upgrade time; the process list was recorded. The
only Python process was this stage's migration lane, on the other PostgreSQL. The
upgrade ran from `E:\wasla-merge-verify`.

| Measure | Result |
|---|---|
| Upgrade | one run, **19 steps (0081 → 0100), 7.5 s**, exit 0 |
| `alembic check` / catalogue / preflight / invariants | clean / 0/0/0, stamp 0100 / ok / 44 checks all 0 |
| Checked | 49 tables (34 populated), **373 rows**, 710 columns; hashed per row and per column, keyed by primary key |
| Rows lost | **0** |
| New tables | `channel_connections` (5), `contact_identities` (8), `contact_channel_consents` (2), 3 empty |
| Reductions opened | **0**, also after one head `BillingWorker` sweep (`handled 0`) |

Every difference, with the proof that it is the documented one:

| Difference | Migration | Proof |
|---|---|---|
| `contacts.marketing_opt_out_at`, `opt_out_source` dropped | 0097 | both opted-out contacts have one `whatsapp` consent row each; the timestamp and source hashes equal the dropped columns' per-row hashes (ENT-19) |
| `plans.limits` changed on 3 of 4; `plans.revision` on 4 of 4 | 0093 / 0098 | renaming `channel_connections` back to `whatsapp_numbers` reproduces each row's original hash exactly (starter 1, pro 3, business 10, enterprise unlimited) |
| `plan_versions` +4, `plan_prices` +2 | 0098 | new versions only; pinned v1 rows unchanged |
| `topup_products.entitlement_key` on 1 of 2 | 0093 | `mv-extra-number` became `channel_connections` typed `whatsapp`; the messages product is unchanged |
| `topup_products` +6 | 0098 | the six inactive placeholder channel top-ups |

Purchases keep `whatsapp_numbers`. No other existing row changed in any column.

Head application (uvicorn, real login) against the upgraded database:

| Check | Result |
|---|---|
| Alpha (`GET /billing/channel-capacity`) | effective 5 = base 3 + top-up 1 + grant 1; active 3; remaining 2; not over |
| Beta | effective 1 = 1 + 0 + 0; active 1; not over |
| connections | Alpha 4 (3 active, the disabled one still disabled), Beta 1 active: the same numbers and states as before |
| staff `…/tenants/{id}/channel-capacity` and `…/channel-connections` | 200; equal field by field to the owner's; 0 plaintext tokens, 0 ciphertexts, no secret-named keys |
| inbound fake-Meta webhook on an upgraded number | 200; 1 inbound stored |
| AI turn | 1 job, 1 provider call, exactly 1 `ai_turn` charge, 1 reply sent |
| sealed token | the reply's `Authorization` header was the original plaintext token (decrypted with the configured key) |
| oracle on this database | 3 comparisons, 0 mismatches |
| secrets | 12 secrets over 502,328 bytes (`pg_dump --data-only`, every log and evidence file): **0 found** |

## 8. Model-Built Test Results

MODEL-BUILT (`WASLA_TEST_SCHEMA` unset), `E:\wasla-merge-lane` at `e50a275`,
`wasla_mv_models`, alone on the machine, 13:49:03–14:27:28 UTC (38 m 08 s):

| Collected | Passed | Failed | Errors | Skipped | xfailed | xpassed |
|---|---|---|---|---|---|---|
| 6,819 | **6,803** | **0** | 0 | 16 | 0 | 0 |

The 16 skips are the known set: 11 `real_provider/test_openai_contract.py` (no
key), 4 `test_schema_parity.py` (model lane), 1 `test_local_storage_cleanup.py`
(`WASLA_TEST_TMPFS`).

The two kept-only non-vacuity checks are deselected exactly as `ci.yml` does.
That is why the prompt's two "kept-data sweep" skips do not appear here.

Count against the first attempt (6,800 / 6,782 / 18): stage 4 added 18 invariant
tests and 3 runner tests, and 2 tests moved from skipped to deselected.
6,800 + 21 − 2 = 6,819; 6,782 + 21 = 6,803. This equals stage 4's report.

## 9. Migration-Built Test Results

MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`),
`E:\wasla-merge-lane` at `e50a275`, `wasla_mv_migrations`. All three runs are
recorded; only run 3 counts.

| Run | Window (UTC) | Collected | Passed | Failed | Errors | Skipped | Cause |
|---|---|---|---|---|---|---|---|
| 1 | 11:35:24–12:29:21 | 6,819 | 6,798 | 8 | 1 | 12 | load + this stage's env (below) |
| 2 | 12:30:07–13:09:19 | 6,819 | 6,800 | 7 | 0 | 12 | this stage's env (below) |
| **3** | **13:10:12–13:48:24 (37 m 55 s)** | **6,819** | **6,807** | **0** | **0** | **12** | — |

- **7 × `tests/unit/test_readiness_gate.py` (runs 1 and 2): a defect in this
  stage's environment.** My `env.sh` exported `MSYS_NO_PATHCONV=1`, meant only for
  `docker run -v` in Git Bash. Under it, Git's `sh` running `check_readiness.sh`
  could not reach the test's local server. Proof: with that file sourced, the
  module gives 7 failed / 4 passed; with the variable unset, 11 passed. The
  variable was removed from `env.sh` and the whole lane re-run.
- **1 error at setup of `test_webhook_media_metadata.py::…[ascii_300-…]` (run 1):**
  `OSError: [WinError 121]`, Docker Desktop's localhost proxy. The mutation run,
  populate and APP-E2E were running beside it on the other PostgreSQL.
- **`test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven`
  (run 1):** `gate.decided 0 == 10` under the same load. It is the known
  load-sensitive test. It passed in runs 2 and 3, and 5 / 5 alone (§11).

Run 3's skips are 11 real-provider and 1 `WASLA_TEST_TMPFS`. Schema parity ran
and passed, and also ran alone first, as the job does: 4 passed, 0 skipped.
6,786 + 21 = 6,807; 14 − 2 deselected = 12, matching stage 4.

POPULATED UPGRADE (0081 → 0100 on data written by the origin revision): 19 steps,
7.5 s, 49 tables, 373 rows, 0 undocumented differences (§7).

## 10. Kept-Data Sweep and CI Job Inventory

**Kept-data sweep (F-1).** This is `ci.yml`'s command verbatim:
`SUITES=$(grep -l "ai_harness" tests/integration/test_*.py | grep -v _invariants)`,
then pytest on the suites plus the two invariant files, with
`WASLA_TEST_KEEP_AI_DATA=1` and `WASLA_TEST_SCHEMA=migrations`. Each run used a
fresh `wasla_mv_kept` with nothing else on that PostgreSQL.

The selection was 28 suites + 2 sweeps. `diff` against `python -m
scripts.kept_data_sweep --list` shows identical files; the runner adds only a
count line.

| Run | Window (UTC) | Collected | Passed | Failed | Skipped | Skip gate |
|---|---|---|---|---|---|---|
| 1 | 14:27:29–14:34:55 | 300 | 300 | 0 | 0 | ok |
| 2 | 14:34:56–14:42:19 | 300 | 300 | 0 | 0 | ok |
| 3 | 14:42:20–14:49:32 | 300 | 300 | 0 | 0 | ok |
| 4 (observed) | 14:49:34–14:56:45 | 300 | 300 | 0 | 0 | ok |

Run 4 added a read-only plugin, run before the last test's teardown drops the
schema, to count what the invariants saw: **222 turns**, 200 with an outcome, 155
`ai_turn` charges, 0 open holds, 0 charged without an outcome. Charge shapes:
`charged` 153, `charged/hold_expired` 2, `released/generation_failed` 18,
`released/not_chargeable` 13, `released/hold_expired` 4, none 32. Runs 1–3 are
verbatim, without the plugin.

**CI job inventory.** `.github/workflows/ci.yml` at `e50a275` has five jobs,
triggered on push to any branch, on pull requests and on `workflow_dispatch`.

| Job | Steps | Local equivalent | Result |
|---|---|---|---|
| `quality` | ruff; black; `mypy app tests` | same (plus `mypy scripts`) | clean; 901 unchanged; 0 errors in 789 (scripts 0 in 11) — **green** |
| `tests` | full pytest (2 deselects, `--cov`); skip gate; object store not skipped; application factory; `alembic upgrade / downgrade base / upgrade`; `db_preflight verify`; `alembic check` | model lane §8 (no `--cov`); `test_object_store.py` 53 passed, 0 "No object store"; `create_app()` ok; up 100 / down 100 / up 100, exit 0; preflight ok; check clean; invariants 44 at 0 | **green** |
| `migration-parity` | parity alone; parity-not-skipped; integration + e2e under migrations (2 deselects); kept-data sweep | parity 4 passed, 0 skipped; full suite under migrations (a superset) §9; sweep ×3 above | **green** |
| `monitoring` | `promtool check config`; `promtool test rules`; `amtool check-config` | same images and commands | config valid, 70 rules, rule tests SUCCESS, Alertmanager valid (1 inhibit rule, 2 receivers) — **green** |
| `docker` | build `--target runtime`; run as `staging` with a per-run JWT secret; `/health/live` within 30 × 2 s | `docker build --target runtime` (1,044 s); same run and probe, 3 runs | run 1: empty reply for all 30 attempts; runs 2, 3: alive at attempts 13 and 7 — **green, with the cause found (below)** |

**Docker's empty reply, found.** The probe succeeds in the same second uvicorn logs
`Uvicorn running`. Before that, Docker Desktop's port proxy accepts the connection
and returns an empty reply.

What delays uvicorn is the Python import of the app inside Docker Desktop's VM:

- Warm: 5.6–5.8 s import, 9–13 s to live.
- Cold: after dropping the VM's page cache on purpose, uvicorn started at
  **147 s**, after **47 empty replies**.

Run 1 followed a 17-minute build that left that cache cold.

This is this machine's Docker Desktop VM and port proxy, not the application. It
is recorded as an observation, not a finding. GitHub's runner is the proof for
the `docker` job.

Deviations, each recorded:

- **`--cov` dropped.** Stage 4 measured about 4× slowdown here, and the repository
  sets no `fail_under`, so coverage cannot change the result.
- **`WASLA_TEST_TMPFS`.** Windows has no tmpfs, so `test_local_storage_cleanup.py`
  skips locally. CI mounts one.
- **GitHub-only:** the tmpfs test, `setup-python`/buildx `type=gha` caching and the
  `concurrency` group. These change speed, not results; the first GitHub run after
  the push is their proof.

This inventory agrees with stage 4's KD-07.

## 11. Targeted Families, Races, Invariants and Oracle

`E:\wasla-merge-verify` (`e50a275`), alone on `wasla-mv-pg`:

| Family | Schema | Result |
|---|---|---|
| migrations (downgrade atomicity, preflight declared, entitlement, plan price, omnichannel, recovery, billing, payment token, schema parity) | migrations | 22 passed |
| entitlements + kept-data fix (reductions, channel top-ups, entitlement invariants, AI turn charging, capacity, oracle, API, channel not in plan, entitlement observability, `test_ai_invariants` with the 18 injection tests, `tests/unit/test_kept_data_sweep.py` selection equality) | models | 149 passed, 1 skipped (the kept-only non-vacuity check, outside kept mode) |
| platform (withdrawal, withdrawal concurrency, reads, filters, hierarchy, access audit) | models | 64 passed |
| omnichannel (webhooks, body cap, retention, media metadata, opt-outs + recovery, consent, provider signals, echoes, pause/inbox, anchors, reply actions, status, send context, follow-ups, invariants, oracles) | models | 212 passed |
| billing (recurring/MOTO, Paymob checkout, webhook, refunds, reconciliation, top-ups, custom plans/offers, annual) | models | 208 passed |
| isolation (tenant isolation, authorization, billing authorization, route authorization, platform hierarchy) | models | 77 passed |

Races. Each suite ran 3 consecutive times on a fresh database, alone. The
attempts, decided, created, refused and max-active figures are each suite's own
assertions; every one held in every run.

| Suite | Tests | Run 1 / 2 / 3 | What the passing assertions establish |
|---|---|---|---|
| `test_channel_capacity_concurrency` | 8 | 8 / 8 / 8 | 10 connects vs 1 free slot → exactly 1 created, 9 refused; the two-channel variant the same; disable vs connects, two enables vs one slot, typed + general, expiry while connecting: active never above capacity at any commit; two billing workers disable once |
| `test_entitlement_races` | 4 | 4 / 4 / 4 | two creations at a limit of one → one; the locked guard on every creating route |
| `test_grant_withdrawal_concurrency` | 3 | 3 / 3 / 3 | connect-first: 1 created, max active 2 ≤ 2, then a reduction; withdraw-first: 1 refused; two staff: 1 withdrawn, 1 conflict, 1 audit entry |
| `test_settlement_concurrency` | 10 | 10 / 10 / 10 | two settlements of one invoice apply once; the database refuses a lockless settlement |
| `test_topup_concurrency` | 9 | 9 / 9 / 9 | one callback ×4 grants once; two workers grant once; grant vs AI consumption never oversells |
| `test_omnichannel_concurrency` | 7 | 7 / 7 / 7 | omnichannel ingestion and sends under contention |
| `test_whatsapp_inbound_concurrency` | 6 | 6 / 6 / 6 | duplicate inbound deliveries processed once |

Time-sensitive test, alone, 5 runs: pass ×5, at 12.73, 13.41, 8.87, 9.79 and
9.86 s per session.

Invariants:

- **E01..E18:** every one 0 on the fresh head, the populated upgrade, the `tests`
  job database and the E2E database.
- **The 18 checks by name:** E01 `connections_over_capacity_unexplained`, E02
  `connection_of_a_type_not_allowed_unexplained`, E03
  `typed_slot_for_a_type_not_allowed`, E04
  `channel_topup_sold_to_an_ineligible_plan`, E05 `turn_charged_twice`, E06
  `charge_for_a_turn_not_chargeable`, E07 `hold_outliving_its_ttl_and_the_sweep`,
  E08 `charged_turn_without_its_charge`, E09
  `reduction_disable_without_its_audit`, E10
  `reduction_released_or_deleted_a_connection`, E11
  `more_than_one_open_reduction`, E12 `retired_whatsapp_numbers_key_in_use`, E13
  `opt_out_without_channel_or_source`, E14
  `campaign_copy_sent_after_an_opt_out_on_its_channel`, E15
  `pinned_version_with_unreadable_channel_types`, E16
  `withdrawn_grant_still_counting`, E17 `withdrawn_grant_without_its_audit`, E18
  `grant_withdrawn_reduction_without_its_grant`.
- **The migration-built suite's own database** cannot be read after its run: the
  suite drops its public schema at session end. The four databases above stand in
  for it.

Oracle:

- **In the suite:** `test_entitlements_oracle.py -s` reported
  `entitlement oracle comparisons: 168, over limit: {True: 125, False: 43}`, with
  0 mismatches. It also passed in both lanes.
- **E2E database:** 3 comparisons, 0 mismatches.
- **Populated upgrade:** 3 comparisons, 0 mismatches.

## 12. Mutation Spot-Check

The first attempt's runner and 18 mutants were recovered from its scratch
directory, read and reused. M-K01, M-K04 and M-K06 were written for this stage;
the M-K01 and M-K04 edits differ from stage 4's own.

Protocol:

- run in `E:\wasla-merge-mut` (detached `e50a275`), model-built `wasla_mv_mut`;
- control first;
- one textual change per mutant;
- KILLED only when a named killer test fails;
- sha256 checked after each restore;
- bytecode off.

Control: 20 killer tests passed. After all mutants, `git status` of the worktree
was empty.

| ID | Mutation | Verdict | Killed by — first assertion |
|---|---|---|---|
| M-P01 | 0091 downgrade `CONCURRENTLY` in autocommit | KILLED | `…refused_below_0091…` — `'0091' == '0100'` |
| M-P01b | 0075 likewise | KILLED | `…refused_at_0069…` — `'0075' == '0100'` |
| M-P02 | preflight ignores missing indexes | KILLED | `…index_the_model_declares…` — `0 == 1` |
| M-P04 | withdrawn grant keeps counting | KILLED | `…stops_counting_at_once` — `(2, 2) == (2, 1)` |
| M-P05 | withdrawal skips `boundary()` | KILLED | `…opens_a_reduction_with_a_grace` — `None is not None` |
| M-P07 | paid purchase withdrawable | KILLED | `test_a_paid_purchase_cannot_be_withdrawn` — 500 (CHECK) instead of 409 |
| M-P13 | platform connections read exposes the sealed token | KILLED | `…never_expose_credentials` — `['v2.f3df…'] == []` |
| M-P15 | another workspace's grant accepted | KILLED | `…another_workspaces_grant_is_404` — `(200, 404)` |
| M-P16 | audit cursor compares `occurred_at` only | KILLED | `…stable_cursor` — "no entry skipped" |
| M-E01 | hold without charge → charge at engagement | KILLED | `test_a_provider_failure_is_not_charged_and_gives_its_hold_back` — CHARGED is RELEASED |
| M-E02 | charge on an empty reply | KILLED | `test_an_empty_answer_is_not_charged` — CHARGED is RELEASED |
| M-E11 | capacity guard reserves without the advisory lock | KILLED | `test_ten_whatsapp_connects_against_one_free_slot_leave_exactly_one` — many `created` |
| M-E15 | typed slot usable by any type | KILLED | `test_a_typed_instagram_slot_never_seats_a_whatsapp_number` — DID NOT RAISE |
| M-E22 | fallback keeps the newest (oldest-kept reversed) | KILLED | `test_with_no_choice_the_fallback_disables_disallowed_types_then_the_newest` |
| M-E28 | opt-out applied person-wide (per-channel consent lost) | KILLED | `test_a_stop_on_whatsapp_covers_every_number_and_not_instagram` — `set() == {…}` |
| M-O07 | `last_inbound_at = at` without GREATEST | KILLED | `test_a_late_older_message_leaves_the_anchors_at_the_newer_one` |
| M-O08 | 1 MiB webhook cap restored | KILLED | `test_a_meta_sized_signed_delivery_reaches_ingestion[1468122]` — 413 |
| M-O11 | paused channel allows outbound | KILLED | `test_a_paused_channel_is_listed_and_refuses_every_send` — 201 instead of 422 |
| M-K01 | B1 checks only a missing charge (`<> 1` → `< 1`): charged twice not checked | KILLED | `test_a_turn_charged_twice_is_counted` — `{} == {'customer…usage': 1}` |
| M-K04 | a `held` hold no longer counted: every open hold explained | KILLED | `test_a_hold_nothing_settled_released_or_expired_is_counted` — `{} == {'AI turn hol…': 1}` |
| M-K06 | the application charges an empty answer | KILLED | `test_an_empty_answer_is_not_charged` — CHARGED is RELEASED; **and the kept sweep at the mutant: 300 / 281 / 19 failed, F-1b counted 7 rows, all B6** |

**Total 21, killed 21, survived 0, restored 21.**

M-K06's sweep was not run in its first attempt. My runner launched `bash` from
Python, and Windows resolved it to WSL's `bash.exe` (exit 1, "WSL Relay ERROR").
That was a defect in this stage's runner, not a survival. The runner was pointed
at Git Bash and M-K06 re-run alone; the result above is that run.

M-P08 is not in the spot-check, as instructed.

## 13. Runtime Verification (APP-E2E)

Setup:

- the real API under uvicorn, real registration and login;
- real `AgentWorker`, `BillingWorker` and `CampaignWorker`;
- `wasla_mv_e2e` built by `alembic upgrade head` from empty (100 steps, 19 s);
- Meta, OpenAI and Paymob faked at the httpx transport.

The scenario script is the first attempt's, read and reused. Run 2026-10-09
11:46:12–11:46:29 UTC at `e50a275`.

| # | Result | Evidence |
|---|---|---|
| V-1 | PASS | staff plan `mv-tiny` (1 connection, 1 AI turn); owner registers, verifies, buys it (fake Paymob callback); number connected 201 |
| V-2 | PASS | inbound → 1 reply, 1 charge, 1 provider call; second inbound → conversation `human`, `AI_QUOTA_EXHAUSTED`, still 1 provider call, 1 charge; turns `replied/charged`, `quota_blocked/—` |
| V-3 | PASS | grant +1 slot 201; second number 201; third 409 `channel_capacity_exceeded` |
| V-4 | PASS | withdraw → limit 2 → 1; reduction `grant_withdrawn`, `pending_selection`, grace exactly 7 days; 0 disabled |
| V-5 | PASS | grace moved past (this DB only); `BillingWorker` → oldest active, newest disabled; 0 released, 0 deleted; `resolved_automatically` |
| V-6 | PASS | `/capacity-reductions` ordered by grace end (11:46:24 resolved, then 10-16 pending); owner and staff capacity/connections equal; 0 secrets in bodies |
| V-7 | PASS | opt-out on WhatsApp → one `whatsapp` consent row; campaign sent to the other contact only |
| V-8 | PASS | invariants exit 0, 44 checks all 0 (E01..E18 listed in §11); oracle 3 comparisons, 0 mismatches |
| V-9 | PASS | 9 secrets over 117,965 bytes (`pg_dump --data-only`, run log, evidence, migrate log, invariants output): 0 found |

## 14. Untracked Files

`E:\wasla` held 11 untracked documents:

- `AI_FINDINGS_REMEDIATION.md`
- `AI_SUBSYSTEM_AUDIT.md`
- `API_ROUTES_AND_ADMIN_GAPS.md`
- `AUTHORIZATION_MULTI_TENANCY_AUDIT.md`
- `FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md`
- `FRONTEND_MASTER_PLAN.md`
- `PLATFORM_ADMIN_API_COVERAGE.md`
- `RAG_SUBSYSTEM_AUDIT.md`
- `REAL_META_WHATSAPP_E2E_VERIFICATION.md`
- `WORKERS_QUEUES_AUDIT.md`
- `WORKERS_QUEUES_FINDINGS_REMEDIATION.md`

- **Collisions:** none of these paths exists in `2c58c35`, `a7fc8b5`, `fe12a37` or
  `e50a275`.
- **Unchanged:** the list and every blob hash (`git hash-object`) were compared
  after each merge and after the report commit, and stayed the same.
- **Untouched:** none was modified, staged, committed or moved.

## 15. Merges and Tree Equality

In `E:\wasla` on `worktree-billing-google-auth`, after `git fetch` confirmed origin
still at `354db53`:

| # | Merge commit | Parents | Incoming | `git diff --exit-code <incoming> HEAD` | Alembic head | Untracked |
|---|---|---|---|---|---|---|
| 1 | `dd6503e` | `3473068` `2c58c35` | omnichannel | 0 | 0091 | unchanged |
| 2 | `7038585` | `dd6503e` `a7fc8b5` | entitlements | 0 | 0098 | unchanged |
| 3 | `1634160` | `7038585` `fe12a37` | platform operations | 0 | 0100 | unchanged |
| 4 | `ccfe9e8` | `1634160` `e50a275` | kept-data fix | 0 | 0100 | unchanged |

- All four are `--no-ff` merges, with no conflict.
- `CANONICAL_MERGED_HEAD = ccfe9e8`; `git diff --exit-code e50a275 ccfe9e8` → exit 0.
- `canonical-merge-verification-20261009` was not merged.

## 16. Gates on the Final Canonical Tree

`E:\wasla-merge-final` (detached `ccfe9e8`), asserting its own `app.__file__`,
alone on `wasla-mv-pg`:

| Gate | Result on `ccfe9e8` | Same as `e50a275` |
|---|---|---|
| ruff / black / mypy app tests / mypy scripts | clean / 901 unchanged / 789 files 0 issues / 11 files 0 issues | yes |
| fresh `wasla_mv_final` → head | 100 steps (51.2 s); check clean; catalogue 0/0/0, stamp 0100; preflight ok; 44 invariants at 0 | yes |
| migrations family (migrations) | 22 passed | yes |
| platform family (models) | 64 passed | yes |
| `test_documentation_truth` | 27 passed | yes |
| entitlements + kept-data family (models), run 1 | 148 passed, **1 failed**, 1 skipped in 311.9 s: `test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven`, `gate.decided 0 == 10` | — |
| entitlements + kept-data family (models), run 2 (recorded) | **149 passed, 1 skipped** in 111.8 s | yes |
| the overlap test alone on `ccfe9e8`, 5 runs | 5 / 5 pass (5.57, 5.76, 6.08, 5.84, 5.69 s per session; call 2.90–3.26 s) | yes |
| full MIGRATION-BUILT lane (`wasla_mv_final_lane`, 16:10:06–16:46:35) | **6,819 / 6,807 / 0 failed / 12 skipped** | yes |
| kept-data sweep, ci.yml selection, fresh DB (16:46:37–16:53:49) | **300 / 300 / 0 / 0**, skip gate ok | yes |

The family's run 1 failure is the known load-sensitive test. It has the same
signature as lane run 1 (§9): the harness's arrival poll ran out before any turn
reached the decision. That whole run took 2.8× its normal time on an identical
tree. The test passed in the full lane on this commit, in the family's run 2,
5 / 5 alone here and 5 / 5 alone on `e50a275`. It never failed alone. Both runs
are recorded; it is not counted as a finding.

## 17. Push

Preconditions, checked immediately before the push, after this report's commit:

- `git fetch origin --prune`;
- origin still `354db53`;
- `merge-base --is-ancestor origin HEAD` true, so the push is a fast-forward.

The push itself is `git push origin worktree-billing-google-auth`, without force.
It happens after this commit, so its result (origin equal to local, `aa11098` on
origin) is reported alongside this commit, not inside it.

No stage branch, helper branch or tag is pushed. `main` is not touched.

## 18. Findings

**None.**

**F-1 is CLOSED.** CI's kept-data sweep is green 3 / 3 at `e50a275` and on the
merged tree, with `ci.yml`'s exact selection. A mutant that charges an empty
answer turns it red with 7 B6 rows.

Observations (not findings):

| Item | Cause | Status |
|---|---|---|
| Docker `/health/live` empty reply right after the build | Docker Desktop's port proxy answers before uvicorn listens; a cold VM page cache makes the import take up to 147 s (reproduced) | this machine; GitHub's runner proves the job |
| `test_readiness_gate.py` 7 failures (lane runs 1–2) | this stage's `MSYS_NO_PATHCONV=1` export | fixed in this stage's environment; lane re-run green |
| `WinError 121`, overlap test under load (lane run 1) | parallel load on Docker Desktop | lane re-run alone green; overlap 5 / 5 alone |
| M-K06 sweep first not run | this stage's runner resolved `bash` to WSL | fixed; re-run alone, KILLED with sweep counts |
| merge commit messages | the four merge commits carry the mandated `-m` messages without a co-author trailer; not amended (no rewriting) | none |

## 19. Cleanup

After the push:

- the `wasla-mv-*` containers and their volumes are removed;
- the image `wasla:mv-ci` is removed;
- `E:\wasla-merge-verify`, `-lane`, `-mut`, `-origin` and `-final` are removed with
  `git worktree remove`.

Left in place, for the operator:

- worktrees `E:\wasla`, `E:\wasla-entitlements`, `E:\wasla-platform-ops`,
  `E:\wasla-kept-data`, `E:\wasla-plat-mut`, `E:\wasla-plat-lane`,
  `E:\wasla-kd-base-omni`, `E:\wasla-kd-base-ent`, `E:\wasla-kd-bisect`;
- the `wasla-ent-*`, `wasla-plat-*` and `wasla-kd-*` containers;
- every stage branch;
- `canonical-merge-verification-20261009`.

## 20. Remaining External Actions

Not performed by this stage:

1. **The deploy itself.** Follow the RUNBOOK rule: stop old processes before
   `alembic upgrade` (0093 adds an enum label; 0097 drops four `contacts`
   columns).
2. **Downgrades below 0099.** Once the first platform grant is withdrawn in
   production, these refuse by design.
3. **Open entitlement items:**
   - real prices on the six channel top-ups;
   - the placeholder plan values;
   - the capacity-reduction email wording;
   - Paymob Live;
   - real-provider verification per adapter.
4. **`canonical-merge-verification-20261009`** (`b1431bf`, the first attempt's
   report): keep or delete after review.
5. **Omnichannel production items:**
   - the 30-day opt-out recovery window after 0085 is deployed;
   - the Meta App field and secret checks;
   - the Graph API upgrade before 2027-01-21.
6. **Deferred product decisions:**
   - staff actions on a reduction;
   - shortening a grant to a date.
7. **`FRONTEND_MASTER_PLAN.md`:** add the staff screens of the platform report's
   section 26. The file is untracked in `E:\wasla`; commit it or back it up.
8. **After review, remove:**
   - the implementation-stage worktrees and containers listed in §19;
   - the stale `prunable` worktree entries (`git worktree prune`).
9. **The first GitHub Actions run on the pushed branch.** It is the final proof
   of three things:
   - the tmpfs test;
   - the cached-build mechanics;
   - the `docker` job's liveness probe on a Linux runner.
10. **Merging into `main`:** whether and when the canonical branch is merged into
    `main` (not part of this stage).

## 21. Final Verdict

**CANONICAL MERGE VERIFIED AND PUSHED**

**The question.** If someone cloned `worktree-billing-google-auth` tomorrow and
built a fresh database or upgraded yesterday's, then ran the full suite on both
schema paths and every CI job, would they get exactly the verified code, with
every test green, every customer's data intact and every channel still connected?

**Answer: yes.**

- **The code they get** is `ccfe9e8`, byte-identical to the `e50a275` tree every
  gate here ran on, and gated again on itself.
- **Yesterday's database** (origin's code, 0081, with customers, numbers,
  top-ups, grants, opt-outs, a stored card and AI turns) upgrades to 0100 in one
  run:
  - 0 rows lost, every changed column the documented one;
  - every number a connection in its old state;
  - every workspace's capacity equal to its old limit, 0 reductions opened.
- **Both schema paths** and all five CI jobs are green, F-1's sweep included.

**What they would need to ask about before deploying:**

- the RUNBOOK's stop-before-migrate rule;
- the open entitlement placeholders in §20;
- the first GitHub run as proof of the tmpfs test and the Linux liveness probe.

## Required Final Runtime Matrix

| Scenario | Expected | Observed | Verdict |
|---|---|---|---|
| Fresh database → head | 100 steps, check clean, catalogue 0/0/0, preflight ok | 100 steps, clean, 0/0/0, ok (twice: `e50a275`, `ccfe9e8`) | PASS |
| Origin's database with data → 0100 | one run, no row lost, no reduction opened, preflight ok | 19 steps in 7.5 s; 373 rows, 0 lost; 0 reductions; ok | PASS |
| Downgrade refused at 0090 | stamp 0100, 0091 index valid | stamp 0100; valid; 309 indexes identical | PASS |
| Downgrade refused at 0069 | stamp 0100, every 0075/0078/0079/0081/0091 index valid | stamp 0100; all 14 valid | PASS |
| Downgrade refused at 0099 | stamp 0100, 0100 index valid | stamp 0100; valid | PASS |
| Index dropped by hand | preflight exit 1, names it | exit 1, `index missing: conversations.ix_conversations_tenant_id_channel_last_message_at` | PASS |
| Upgraded owner vs staff channel reads | equal, no secret | equal field by field; 0 tokens, 0 ciphertexts | PASS |
| Grant withdrawn, workspace over capacity | reduction with 7-day grace, 0 disabled | `grant_withdrawn`, 7 days, 0 disabled | PASS |
| Grace passes | newest disabled, oldest kept, 0 released, 0 deleted | as expected; `resolved_automatically` | PASS |
| AI quota exhausted | `AI_QUOTA_EXHAUSTED`, no provider call | handoff, provider calls still 1 | PASS |
| CI kept-data sweep (F-1) | ci.yml selection, 0 failed, 3 / 3 runs | 300 / 300 ×3, selection equal | PASS |
| Every other CI job | run locally and green, or GitHub-only with a reason | quality, tests, migration-parity, monitoring, docker green; tmpfs + caches GitHub-only | PASS |
| Merged canonical tree vs verified tree | byte-identical | `git diff --exit-code e50a275 ccfe9e8` → 0 | PASS |
| origin after push | equals local canonical; contains `aa11098` | reported with the push (§17) | see §17 |

## Required Numbers

| Item | Value |
|---|---|
| Commits landed | 83 (omnichannel 29, entitlements 37, platform 9, kept-data 8) + 4 merge commits (`dd6503e`, `7038585`, `1634160`, `ccfe9e8`) + this report |
| Migrations | 0081 → 0100; 16 added (0085–0100); `downgrade()`-only edits: 0075, 0078, 0079, 0081, 0091 |
| Alembic from empty | 100 steps; 39.9 s (beside a lane), 19 s (alone) |
| Alembic from origin on data | 19 steps, 7.5 s |
| Refused downgrades | 3 / 3 refused; stamp 0100; 309 indexes identical and valid each time |
| Populated upgrade | 49 tables, 373 rows, 710 columns; 11 differences, all documented; 0 rows lost; 0 reductions |
| Model-built | 6,819 / 6,803 / 0 / 16 / 0 xfailed / 0 xpassed (MODEL-BUILT) |
| Migration-built | 6,819 / 6,807 / 0 / 12 / 0 xfailed / 0 xpassed (MIGRATION-BUILT, run 3; runs 1–2 recorded in §9) |
| Time-sensitive test | 5 / 5 alone: 12.73, 13.41, 8.87, 9.79, 9.86 s |
| Kept-data sweep | 3 runs × 300 / 300 / 0 / 0 (+ observed 4th); 222 turns, 155 charges; selection equal to ci.yml: yes |
| CI jobs | 5 listed, 5 run locally, 5 green; GitHub-only: tmpfs test, caches, Linux liveness |
| Races | 7 suites × 3 runs, all green |
| Invariants | E01..E18 all 0 (fresh, upgraded, CI-steps, E2E, final) |
| Oracle | suite 168 comparisons / 0 mismatches; E2E 3 / 0; upgraded 3 / 0 |
| Mutations | 21 total, 21 killed, 0 survived, 21 restored |
| APP-E2E | V-1..V-9 PASS |
| Secrets | upgrade: 12 over 502,328 bytes, 0 found; E2E: 9 over 117,965 bytes, 0 found |
| Untracked files | 11 before, 11 after, identical hashes |
| origin | before `354db53`; after: see §17 |
