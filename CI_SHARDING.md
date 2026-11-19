# CI sharding: ci.yml from ~55-63 minutes to ~12

Branch `ci-shard-20261117`, cut from canonical `worktree-billing-google-auth` at
`baa0664`. Local commits only; nothing pushed, `main` untouched, no PR.

## What changed

| Job | Before | After |
| --- | --- | --- |
| `tests` | one pytest process, model-built schema, `--cov=app` (53-62 min) | 7-way matrix, each shard a separate session with its own PostgreSQL, Redis, MinIO and tmpfs; same gates per shard |
| `migrations` | tail steps of `tests` (app starts, upgrade/downgrade/upgrade, `db_preflight verify`, `alembic check`) | own job on a fresh database, so it no longer waits an hour |
| `migration-suites` | one step inside `migration-parity` (35-38 min) | 5-way matrix against the migration-built schema, **now with a skip gate** (it had none) |
| `migration-parity` | parity, then integration/e2e, then the kept-data sweep | parity check + the kept-data sweep, unsharded, one session as required |
| `coverage` | (none) | after every shard: proves every collected test ran exactly once, then `coverage combine` and `coverage report -m` |

The split is done by `scripts/ci_shards.py`, which is deterministic. It places
whole files, longest first, onto the least-loaded shard (LPT), using
`scripts/ci_test_durations.json`. A file the timings do not know yet is costed
at the mean, so it is still run. Within a shard, files keep the unsharded run's
order. No new dependency was added and the lock is unchanged; the only new
workflow action is `actions/download-artifact` v8.0.2, pinned by SHA.

What still holds, now pinned in `tests/unit/test_delivery_pipeline.py`:

- Each shard passes only its `$FILES` and the same two kept-only `--deselect`s:
  no `-k`, no `-m`, and no other root.
- Every shard runs the skip gate on its own output. The model shards allow the
  same four reasons as before. The migration shards allow exactly the two
  reasons the single run skipped for (CI run 44146445974): 73 tests for "No
  object store configured" and 2 for the opt-in old-pgvector lane. They do
  **not** allow the schema-parity skip.
- The model shards keep `--cov=app`, MinIO, the tmpfs and the "No media suite
  was skipped" gate.
- The kept-data sweep and the parity check stay in one unsharded job with
  `WASLA_TEST_SCHEMA=migrations`. `test_kept_data_sweep.py` passes unchanged.
- Every ci.yml job that installs Python installs the lock by hash first
  (RGM-21's rule, now applied to ci.yml too).
- The coverage job needs both matrices, verifies both suites with the same
  deselections, and combines the model shards' data.

`scripts/ci_shards.py` collects each suite exactly as the unsharded command
would and requires the JUnit reports to hold, for every test function, exactly
the number of cases collected, all from one shard. It counts cases per function
instead of matching full IDs because three tests have parametrize IDs that
differ between processes (see Findings). With a split by file, counting is just
as strict: a dropped case lowers a count, and a file run twice doubles one.

## Measurements

**Data sources.**

- **CI timings:** green run 44146445974 on `baa0664`, the exact base. Per-file
  seconds come from the timestamped progress lines of the two unsharded steps.
  They reconcile exactly: 8,309 and 4,292 tests, against 8,292 passed + 17
  skipped, and 4,292 collected.
- **Local check:** all 12 shards run as separate sessions, in three parallel
  lanes with their own containers (Windows, slower than CI).

### Slowest files (CI seconds, line-smoothed to about ±28 s)

| # | Model-built run (3,195 s) | s | # | Migration-built run (2,260 s) | s |
| --- | --- | --- | --- | --- | --- |
| 1 | integration/test_rag_ingestion_lifecycle.py | 103 | 1 | integration/test_campaign_header_media_api.py | 67 |
| 2 | integration/test_campaign_messaging_limit.py | 51 | 2 | integration/test_rag_ingestion_lifecycle.py | 55 |
| 3 | integration/test_rag_integrity.py | 48 | 3 | integration/test_campaign_invariants.py | 34 |
| 4 | integration/test_rag_invariants_sweep.py | 45 | 4 | integration/test_knowledge_formats.py | 30 |
| 5 | integration/test_campaign_header_media_api.py | 44 | 5 | integration/test_campaign_messaging_limit.py | 29 |
| 6 | integration/test_deployment_configuration.py | 41 | 6 | integration/test_email_webhook_endpoint.py | 29 |
| 7 | integration/test_agent_options_and_knowledge_limits.py | 34 | 7 | integration/test_annual_billing.py | 28 |
| 8 | integration/test_knowledge_formats.py | 33 | 8 | integration/test_deployment_configuration.py | 27 |
| 9 | integration/test_account_lifecycle.py | 33 | 9 | integration/test_rag_integrity.py | 26 |
| 10 | integration/test_annual_billing.py | 33 | 10 | integration/test_rag_invariants_sweep.py | 24 |
| 11 | integration/test_platform_billing_api.py | 32 | 11 | integration/test_campaign_header_media_lifecycle.py | 24 |
| 12 | integration/test_environment_hardening.py | 31 | 12 | integration/test_campaigns.py | 24 |
| 13 | integration/test_email_webhook_endpoint.py | 30 | 13 | integration/test_ai_security.py | 22 |
| 14 | integration/test_campaigns.py | 30 | 14 | integration/test_topups.py | 22 |
| 15 | integration/test_campaign_quiet_hours.py | 28 | 15 | integration/test_platform_billing_api.py | 21 |
| 16 | integration/test_campaign_served_gate.py | 28 | 16 | integration/test_agent_options_and_knowledge_limits.py | 20 |
| 17 | integration/test_ai_security.py | 28 | 17 | integration/test_platform_role_lifecycle.py | 20 |
| 18 | integration/test_rag_operations.py | 26 | 18 | integration/test_environment_hardening.py | 18 |
| 19 | integration/test_error_envelope_contract.py | 26 | 19 | integration/test_inbox_contract.py | 18 |
| 20 | integration/test_events_stream.py | 26 | 20 | integration/test_platform_hierarchy.py | 18 |
| 21 | integration/test_inbox_contract.py | 25 | 21 | integration/test_provider_session_lifetime.py | 18 |
| 22 | integration/test_platform_hierarchy.py | 25 | 22 | integration/test_queue_commit_visibility.py | 18 |
| 23 | integration/test_campaign_personalisation.py | 24 | 23 | integration/test_rag_turns.py | 17 |
| 24 | integration/test_campaign_reservation.py | 24 | 24 | integration/test_ai_invariants.py | 17 |
| 25 | integration/test_campaign_results.py | 24 | 25 | integration/test_malformed_authenticity_values.py | 17 |
| 26 | integration/test_campaign_segments.py | 24 | 26 | integration/test_error_envelope_contract.py | 16 |
| 27 | integration/test_google_endpoints.py | 24 | 27 | integration/test_events_stream.py | 16 |
| 28 | integration/test_topups.py | 23 | 28 | integration/test_workspace_gates.py | 16 |
| 29 | integration/test_workspace_gates.py | 23 | 29 | integration/test_commercial_api.py | 16 |
| 30 | integration/test_lead_crm.py | 23 | 30 | integration/test_workspace_lifecycle.py | 15 |

No single file dominates: the largest is 3% of its run, so sharding scales
almost linearly. Integration tests account for 96% of the model-built run.

The slowest individual tests come from local JUnit, where times are about 2x
CI's: `test_rag_eval::test_the_structural_evaluation` (191 s),
`test_campaign_messaging_limit::test_an_unlimited_portfolio_is_paced_by_throughput_only`
(153 s), `test_omnichannel_migrations::test_the_channel_foundation_carries_real_rows_forward_and_back`
(113 s), `test_events_capacity::test_two_hundred_streams_on_one_process` (99 s,
loaded), and nine more `test_campaign_messaging_limit` cases at 43-53 s each.

**Real sleeping is not the cost.** Every `asyncio.sleep`/`time.sleep` of at
least 0.5 s in the suite adds up to about 30 s of the 3,195 s. The largest are
`test_neighbour_expansion` (3 s), `test_media_url_and_limits` (2.5 s) and the
1-1.5 s waits in the campaign, events-stream and database-timeout suites.

### Wall time and runner minutes (estimated from measured CI seconds)

Fixed job overhead measured in CI: about 65-71 s before the first test, with
installs cached.

| Job | Estimate |
| --- | --- |
| `tests` shard (x7) | ~71 s + 457 s + ~15 s gates/upload, about **9.1 min** |
| `migration-suites` shard (x5) | ~60 s + 452 s + ~10 s, about **8.7 min** |
| `migration-parity` (parity + kept sweep) | ~64 s + 12 s + 417 s, about **8.2 min** |
| `migrations` | about 1.5 min |
| `coverage` (after the shards) | about 2.1 min (setup, two collections, combine) |

| | Before | After (estimate) |
| --- | --- | --- |
| ci.yml wall time | 55.1 min (run 44146445974), 63.5 min (run 44122041770) | **about 11-12 min**: slowest shard about 9-10 min, then coverage about 2 min |
| Runner minutes per run | about 105 | about 123 (**+18 min, +17%**) |
| Concurrent jobs at peak | 5 | 17 (the account cap is 20; see below) |

The repository is public, so standard-runner minutes are not billed. The real
cost is concurrency: a push that also triggers `frontend.yml` (up to about 6
jobs) may queue a job or two briefly. When frontend paths change,
`frontend.yml` (21-23 min, set by the Playwright jobs) is now the longest
workflow. It was out of scope here.

Local shard results (Windows, three lanes in parallel, so slower than CI): all
12 green. Model shards ran 14-23 min each and migration shards 10-21 min; the
spread comes from lane contention, not from the split. Completeness check run
locally:

- Migrations: "4292 tests, each run exactly once across 5 shards".
- Model: every function's count matched. The only reported problem was one
  test added to `test_ci_shards.py` after the shards had started, which shows
  the check catches a test that did not run.

### COVERAGE_CORE=sysmon: measured, not adopted

Model shard 3 was run alone twice on a quiet machine:

- Default tracer: 997 passed in 870 s.
- sysmon: 997 passed in 683 s, **21.5% faster**.

Coverage did not match:

- Two default-tracer runs of the same shard differ by 95 lines in 47 files
  (normal noise).
- Default tracer against sysmon differs by **3,049 of 53,071 measured lines
  (-5.7%) across 137 files**.

The brief was to keep it only if coverage matched, so it stays off.

### Coverage baseline for the first sharded CI run

The single run in 44146445974 reported `TOTAL 42054 3484 92%` over 418 files.
The `coverage` job's report should match this within normal run-to-run noise.
It is the first thing to check after the push.

## Findings

1. **No cross-file coupling found.** Every shard passed as its own session,
   each with a different set of preceding files, so no test depended on running
   after another file.
2. **`test_events_capacity.py::test_two_hundred_streams_on_one_process` depends
   on load (pre-existing, not caused by sharding).** It mints all 200 stream
   tickets before opening any, and tickets live `RT_TICKET_TTL_SECONDS` = 60.
   It failed once with 401 when three lanes overloaded the machine: minting
   took more than 60 s. It passed alone (22 s) and in its whole shard rerun on
   a quiet machine (997 passed). On a slow runner it would be flaky. Fix
   options: mint each ticket just before its stream, or raise the TTL in that
   test. Left for the owner; not hidden by retries.
3. **Three tests have parametrize IDs that change between processes.** This
   breaks `pytest "<node id>"` reproduction and any ID-based tooling:
   - `test_auth_hardening.py::test_the_other_invitation_routes_still_require_credentials`
     (a fresh UUID in the ID)
   - `test_media_type_enforcement.py::test_a_spoofed_upload_is_refused_and_nothing_is_sent`
   - `unit/test_media_types.py::test_bytes_contradicting_the_claim_are_refused`
     (this and the one above: zip bytes with a timestamp in the ID)

   Giving them `ids=` would fix it. Not changed here.
4. **The migration-built run had no skip gate before.** The new per-shard gate
   is stricter than what it replaces and allows only the two reasons that run
   already skipped for.

## Proposed (not applied): narrowing ci.yml's triggers

Nothing was added to `paths-ignore`. Few paths can be proven unread by every
test:

- **Safe as far as found:** the 49 top-level report `.md` files that no test,
  script or `app` module names (for example `FE6_AI_KNOWLEDGE.md`,
  `CANONICAL_MERGE_VERIFICATION.md`, `DECISIONS.md`). Tests read `README.md`,
  `ARCHITECTURE.md`, `claude.md`, `RAG_SYSTEM_REVIEW.md`,
  `CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md`, `TASKS.md` and a few others, and
  `test_documentation_truth.py` globs `docs/*.md`. Those must stay.
- **Not safe:** `frontend/**`.
  - `test_error_envelope_contract.py` reads
    `frontend/packages/api-client/openapi.json`.
  - `test_client_ip_forward.py` reads
    `frontend/packages/api-client/fixtures/forward-signature.v1.json`.
  - `test_backup_scripts.py` searches the whole tree for committed `*.dump` and
    `*.sql.gz` files, including `frontend/`.

  A frontend-only push must therefore still run ci.yml.

The gain would be small: these files change in docs-only commits. It is the
owner's call.

## Maintenance

- Refresh the timings from a green run's artifacts:
  `python -m scripts.ci_shards record --suite model shard-model-*/*.xml`, and
  the same for `migrations`.
- Change a shard count by editing the matrix list. `--shards` comes from
  `strategy.job-total`, and the README command is checked against the matrix by
  `test_ci_shards.py`.
- Reproduce a red shard:
  `pytest $(python -m scripts.ci_shards files --suite model --shards 7 --index N)`.
