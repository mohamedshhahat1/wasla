# Campaigns Canonical Merge Verification

| | |
| --- | --- |
| Date | 2026-10-15 / 16 |
| Lands | campaigns outbound audit (`6ecbdd6`), findings remediation (`427a0f1`), campaign header media (`e38f35f`) |
| Into | `worktree-billing-google-auth` (not `main`) |
| Origin at start | `989584b` (0 ahead / 0 behind; unmoved at every check) |
| Alembic | `0100` → **`0107`** |
| Real providers | none contacted: Meta, OpenAI, Paymob and Resend faked at the httpx transport |

---

## 1. Executive Summary

**Verdict: recorded in section 21 after the first GitHub Actions run.** Every local gate is green and the merges are done.

The campaigns line - the audit, its remediation and the header media stage -
was verified independently of the stages that built it, in detached worktrees
that were never edited and on this stage's own containers, then merged into
the canonical branch as two normal merge commits whose trees are byte-identical
to the verified head.

| Gate | Result |
| --- | --- |
| Ancestry `989584b ⊂ 6ecbdd6 ⊂ 427a0f1 ⊂ e38f35f` | holds |
| Existing migrations 0001–0100 | byte-identical; 0101–0107 added (7 files) |
| Fresh database → 0107 | 107 steps, check clean, catalogue 0/0/0, preflight ok, 54 invariant checks 0 |
| Origin's populated database → 0107 | one run, 7 steps, 55 tables / 511 rows / 809 columns, **0 differences**; old campaigns resume, nobody twice |
| Full lanes (independent) | MODEL-BUILT 7,035 passed / **0 failed** / 18 skipped · MIGRATION-BUILT 7,039 passed / **0 failed** / 14 skipped |
| Kept-data sweep | 301 passed, 0 skipped - 3 / 3 (and 1 / 1 on the final tree); selection equal to `ci.yml` |
| Races | 48 passed - 3 / 3 (and 1 / 1 on the final tree) |
| Mutation spot-check | control 129 passed; every spot-check mutant killed by its named test (M-H02 the proven redundant guard) |
| APP-E2E V-1..V-8 | all PASS; 0 secrets found |
| Merges | `f43e454`, `a8e8f24` - normal merge commits, no conflicts, each tree byte-identical to its branch |
| Push | section 16 |
| First GitHub Actions run | section 17 |

**One finding, CMV-01, found here and fixed in Part A.** The first independent
verification (of `6d9728e`) passed every gate except one: the control of the
mutation spot-check, run while the kept-data sweep loaded the machine, failed
`test_many_workers_upload_once_and_send_everyone_once[4]` - 2 uploads where it
asserted 1. Reproduced 3 of 5 under CPU load: a 0.2 s lease outlasted by an
upload is the takeover case HM-04 allows (two uploads, one id sent), so the
test, not the application, was wrong. Part B may not fix tests; the fix went
into Part A's branch (`14081fe`, test-only), Part A re-ran both full lanes on
it, and **everything in this report was then re-run on the new head
`e38f35f`** (sections 6–12). The application code verified at `6d9728e` and at
`e38f35f` is identical (`git diff 6d9728e e38f35f` touches one test file and
Part A's report).

## 2. Source Branches and Exact Heads

| Name | Branch | Head |
| --- | --- | --- |
| ORIGIN_CANONICAL_START_HEAD | `origin/worktree-billing-google-auth` | `989584b4a0fe45b14e81b82ceec65ad61a276084` |
| audit | `campaigns-outbound-audit-20261013` | `6ecbdd6` |
| CMP_REM_HEAD | `campaigns-findings-remediation-20261015` | `427a0f1` (not moved) |
| HM_HEAD (verified) | `campaigns-header-media-20261015` | **`e38f35f`** (code `14081fe`) |
| first HM_HEAD | same branch | `6d9728e` (superseded by CMV-01's fix) |
| ORIGIN_MIGRATION_HEAD | — | `0100` |

## 3. Part A Summary

`CAMPAIGN_HEADER_MEDIA_IMPLEMENTATION.md`: **CAMPAIGN HEADER MEDIA IMPLEMENTED
WITH EXTERNAL VERIFICATION REMAINING.** A media-header campaign uploads its
file once per campaign and connection under the lease and sends one Meta id to
everyone (30 people: 1 upload, 30 sends; 2 and 4 workers: 1 upload; a lease
taken over mid-upload: 2 uploads, one id sent; 131053 twice: FAILED after 2
sends). Migration 0107, ADR-144, CR-08..CR-10, alert
`CampaignHeaderMediaRefused`. Mutation: 14 Part A mutants all killed or
classified (M-H02 a redundant guard proven by a combined mutant); 7 remediation
regression mutants killed. Part A's own lanes on `14081fe`: model-built 7,035 /
0 failed, migration-built 7,039 / 0 failed. Self-score 95/100.

## 4. Ancestry and Commits Landing

```text
git merge-base --is-ancestor 989584b 6ecbdd6   true
git merge-base --is-ancestor 6ecbdd6 427a0f1   true
git merge-base --is-ancestor 427a0f1 e38f35f   true
git merge-base --is-ancestor 989584b e38f35f   true
git diff --stat 989584b 6ecbdd6                CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md only (1,373 lines)
```

`git log --oneline 989584b..e38f35f`: **31 commits** - audit **1**, remediation
**19**, header media **11** (Phases 0-10, the CMV-01 test fix and the report
addendum). Two merge commits are added by section 14.

## 5. Migration Changes

`git diff --name-status 989584b e38f35f -- alembic/versions`: 7 lines, all `A`
(0101–0107); **0 existing migrations modified**.

| Revision | Adds |
| --- | --- |
| 0101 | `campaigns.lease_token` |
| 0102 | `campaigns.status_reason` (CHECK), three audit actions |
| 0103 | `campaigns.template_parameters`, `messages.failure_code` |
| 0104 | recipient `reason_code`, `not_before`; `provider_unreachable_since`, `consecutive_batch_failures` |
| 0105 | `opt_out_via` label `provider_status` |
| 0106 | `ck_campaigns_messages_per_minute_bounded`, `fk_campaigns_tenant_account` |
| 0107 | `campaign_header_media`; four header columns on `campaigns`, two keys, a CHECK, two stop reasons, an audit action, a concurrent index |

## 6. Independent Static and Alembic Gates

`E:\wasla-cmv-verify` (detached at `e38f35f`, never edited; `app.__file__` =
`E:\wasla-cmv-verify\app\__init__.py` asserted):

```text
ruff check .                     All checks passed!
black --check .                  932 files would be left unchanged
mypy app tests                   Success: no issues found in 813 source files
mypy scripts                     Success: no issues found in 11 source files
alembic heads                    0107 (head) - single
fresh empty wasla_cmv_alembic    107 steps, 24.7 s (beside the running lanes; 11.3 s on a quiet machine)
alembic check                    No new upgrade operations detected
catalogue                        0 NOT VALID, 0 invalid indexes, 0 disabled triggers
db_preflight verify              ok
omnichannel_invariants verify    ok - 54 checks, all 0: E01..E18 and CR-01..CR-10 among them
head -> 0100 -> head             clean; alembic check clean
refused downgrade                a campaign with status_reason; downgrade 0101 -> RuntimeError
                                 "Campaign stop reasons cannot be downgraded ... Nothing has been
                                 changed: campaigns carrying a status_reason: 1"; stamp 0107; preflight ok
test_documentation_truth         green; README 0001–0107
```

## 7. Populated Upgrade from Origin

`wasla_cmv_upgrade` was built in `E:\wasla-cmv-origin` (detached at `989584b`)
by `alembic upgrade head` (100 steps) and populated **through that revision's
own services and worker** (`populate.py`, scratch), Meta faked at the transport:

| Data | How (origin code) |
| --- | --- |
| 2 workspaces; owner, admin, member each | models + memberships; plan with a campaign allowance (`tests.channel_plans`) |
| 2 numbers each, sealed tokens (`CredentialService.seal`), one disabled | `CredentialService` |
| MARKETING body-only, TEXT header, IMAGE header, a to-be-withdrawn one; one UTILITY | template rows as a sync writes them |
| 46 inbound messages (40 + 6 customers wrote first) | `WhatsAppIngestionService.ingest` |
| outbound text, template, image (with a stored object) | `MessagingService.send_text / send_template / send_media` |
| campaigns **draft, scheduled, running, paused, completed, cancelled, failed** (+1 completed in workspace 2) | `CampaignService` + the origin `CampaignWorker` |
| recipients **pending, sent, failed (500, unconfirmed), skipped (opted out after materialising)**, 15 linked to messages | the origin worker |
| opt-outs on WhatsApp (2) and Instagram (1) | `CampaignService.set_opt_out` |
| audit entries (10) | side effects |

No origin process was alive at upgrade time. From `E:\wasla-cmv-verify`:

| Measure | Result |
| --- | --- |
| Upgrade | one run, **7 steps (0100 → 0107), 5.2 s**, exit 0 |
| `alembic check` / catalogue / preflight / invariants | clean / 0/0/0 / ok / all 0 |
| Checked | **55 tables, 511 rows, 809 columns** (every column that existed at 0100, per-table checksum over all rows) |
| Differences | **0** |
| Pre-existing campaigns | status unchanged; `status_reason`, `lease_token`, `template_parameters`, every header column null; `consecutive_batch_failures` 0 |
| Pre-existing recipients | status and attempts unchanged; `reason_code`, `not_before` null |
| `messages.failure_code` | null everywhere |
| Opt-outs | unchanged per channel |
| Sealed tokens | the HM_HEAD worker sent with the number's own decrypted token (U-3); `pg_dump --data-only` holds 0 plaintext tokens |

Then the application of HM_HEAD against it (uvicorn, real login, real
`CampaignWorker`, fake Meta at the transport):

| # | Result |
| --- | --- |
| U-3 | the pre-existing **running** campaign (old-code lease in `next_send_at`, no token) taken by the new worker: its 4 remaining recipients sent, **8 sent**, `completed`; `Authorization` = the workspace's own token |
| U-1 | the pre-existing **scheduled** body-only campaign sent to its 5 remaining recipients, **each once**, `completed` |
| U-2 | a new campaign on the IMAGE-header template with a file uploaded through the API: **1 upload, 8 sends, one header id** |
| after | CR-01 = CR-02 = 0; every invariant 0 |

(The first attempt at U-2, on the first verification, gave the worker a
different media directory than the API wrote to - a harness mistake - and the
campaign paused `header_media_unavailable` with 0 requests to Meta, which is
HM-06 working; the harness was corrected and U-2 passed.)

## 8. Model-Built Test Results

MODEL-BUILT (`WASLA_TEST_SCHEMA` unset), `E:\wasla-cmv-lane` (detached at
`e38f35f`; `app.__file__` = `E:\wasla-cmv-lane\app\__init__.py`),
`wasla_cmv_models` on `wasla-cmv-pg` (55941), Redis 56941. Coverage omitted
(deviation recorded by every earlier stage: `--cov` slows the suite about 4x
and CI sets no `fail_under`; GitHub runs it).

| Run | Head | Collected | Passed | Failed | Skipped | xfailed / xpassed | Time (UTC) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| first verification | `6d9728e` | 7,053 | 7,035 | 0 | 18 | 0 / 0 | 19:14:59 → 20:00:10 |
| **final** | **`e38f35f`** | 7,053 | **7,035** | **0** | 18 | 0 / 0 | 21:38:51 → 22:24:59 |

Equal to Part A's counts. Skips, the known set and nothing else: real-provider
OpenAI contract x11 (no key; forbidden); schema parity x4 (migration lane
only); `test_local_storage_cleanup` x1 (`WASLA_TEST_TMPFS`, a Linux tmpfs CI
mounts); the AI and tool kept-data-only checks x2 (CI deselects them in this
job and runs them in the sweep). CI's skip gate, applied to this output with
those two local-only reasons added, finds **0** unexpected skips.

## 9. Migration-Built Test Results

MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`),
`E:\wasla-cmv-verify`, `wasla_cmv_migrations` on `wasla-cmv-pg2` (55942), Redis
56943 - never on the same server as the model-built lane.

| Run | Head | Collected | Passed | Failed | Skipped | Time (UTC) |
| --- | --- | --- | --- | --- | --- | --- |
| first verification | `6d9728e` | 7,053 | 7,039 | 0 | 14 | 19:15:01 → 19:59:22 |
| **final** | **`e38f35f`** | 7,053 | **7,039** | **0** | 14 | 21:38:53 → 22:25:04 |

Skips: OpenAI x11, tmpfs x1, kept-data-only x2. Schema parity ran and passed.

## 10. Kept-Data Sweep and CI Job Inventory

**Kept-data sweep** - `ci.yml`'s command exactly (`SUITES=$(grep -l
"ai_harness" tests/integration/test_*.py | grep -v _invariants)`: the same 28
files, listed in `kept_selection.txt`; then `test_ai_invariants.py` and
`test_tool_invariants.py`), `WASLA_TEST_KEEP_AI_DATA=1`, migration-built, a
fresh database each run, nothing else on that server:

| Head | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| `6d9728e` | 301 passed, 0 skipped | 301 / 0 | 301 / 0 |
| **`e38f35f`** | **301 passed, 0 skipped** | **301 / 0** | **301 / 0** |

**CI jobs, each run locally at `e38f35f`:**

| Job | Steps | Local result |
| --- | --- | --- |
| quality | ruff, black, mypy app tests | all clean (section 6) |
| tests | full model-built suite (`--cov` dropped); skip gate; object store not skipped; application factory; upgrade → base → upgrade; db_preflight; alembic check | 7,035 / 0 failed; 0 unexpected skips; `test_object_store.py` 53 passed, "No object store configured" 0; `application factory ok`; 107 / 107 / 107 steps; ok; no operations |
| migration-parity | schema parity; parity not skipped; integration + e2e migrated; kept-data sweep | 4 passed, 0 skipped; full migrated lane 7,039 / 0 (a superset: it includes `tests/unit`); sweep 3 / 3 |
| monitoring | promtool check config, test rules; amtool check-config | `75 rules found` SUCCESS; SUCCESS; SUCCESS |
| docker | build `runtime`; `/health/live` | `wasla:cmv` built; `{"status":"alive"}` after 7 s, three empty replies first (earlier stages saw two to four: the app's import inside Docker Desktop's VM) |

**GitHub-only:** the tmpfs-backed storage-cleanup test (Linux tmpfs), `--cov`,
and the real-provider OpenAI tests if the repository holds a key.

## 11. Races, Invariants and Mutation Spot-Check

**Races** (`test_campaign_two_workers.py`, `test_campaign_reservation.py`,
`test_topup_concurrency.py`, `test_campaign_header_media.py`), three
consecutive runs at `e38f35f`, `wasla_cmv_races`: **48 passed** x3 (118 s,
113 s, 113 s). Each test asserts its own people-twice (0), requests, uploads
and meters at the transport; none failed.

**Invariants** (`python -m scripts.omnichannel_invariants verify`; 54 checks,
E01..E18 and CR-01..CR-10 among them), every check 0 on: the fresh alembic
database; the upgraded origin database after U-1..U-3; the APP-E2E database
(V-7); the final canonical database. The suite databases drop their schema at
session end by design (`prepared_database`), so they are not left to sweep.

**Mutation spot-check**, `E:\wasla-cmv-mut` (detached, own database
`wasla_cmv_mut`), the strict runner (anchor found once, named killers, every
file restored by SHA-256), alone on the machine:

| Mutant | `e38f35f` | Killer |
| --- | --- | --- |
| control | **129 passed** | — |
| M-C01 + M08 | KILLED | `test_a_batch_longer_than_its_lease_is_not_shared` |
| M-C02 | KILLED | `test_a_campaign_leased_but_not_started_is_not_claimed_again` |
| M-C09 | KILLED | `test_two_admins_gated_at_the_old_decision_point_cannot_both_fit` |
| M-C15 | KILLED | `test_a_parameter_refusal_costs_one_request...[132000]` |
| M-C21 | KILLED | `test_each_answer_class[read_timeout]` |
| M-C25 | KILLED | `test_a_send_that_may_have_left_is_never_re_armed` |
| M-C31 | KILLED | `test_an_admin_cannot_send_a_marketing_template_to_an_opted_out_contact` |
| M-H01 | KILLED | `test_thirty_people_one_upload_thirty_sends_one_id` |
| M-H02 | survives - **REDUNDANT GUARD**, as Part A proved | behind `claim_by_id`'s row lock |
| M-H02 + row lock removed | KILLED | `test_a_worker_held_before_its_upload_past_its_lease_uploads_once` |
| M-H05 | KILLED | `test_a_wrong_missing_or_extra_file...[IMAGE-pdf]` |
| M-H07 | KILLED | `test_meta_refusing_the_upload_fails_the_campaign...` |
| M-H09 | KILLED | `test_media_unknown_twice_fails_the_campaign_after_two_sends` |
| M-H11 | KILLED | `test_another_workspaces_file_is_not_found_anywhere` |

`git status` in the mutation worktree: clean before and after. M-H02 alone is
not a meaningful survivor: it is the classified redundant guard of Part A's
section 14, and its combined mutant is killed here by its named test.

**The first spot-check** (at `6d9728e`, run beside the kept-data sweep) had a
red control - CMV-01, section 18.

## 12. Runtime Verification (APP-E2E)

Setup: the real API under uvicorn on 127.0.0.1 (the harness of the platform
operations merge, read in full and reused), real registration and login,
`wasla_cmv_e2e` built by `alembic upgrade head` from empty (107 steps, 11.7
s), two real `CampaignWorker`s with their own `Database`s, Meta / OpenAI /
Paymob faked at the httpx transport. Additions for this stage: the fake Meta
answers `POST /{id}/media` and `GET /{waba}/message_templates`; the campaign
worker's and the template service's HTTP factories are routed to it; the
client's outbound-URL guard is stubbed so not even a DNS lookup leaves the
machine; email uses the `fake` provider so invitations can be accepted. Run at
`e38f35f`, 2026-10-15 21:42:21 → 21:42:38 UTC; evidence `cmv/e2e/evidence.jsonl`.

| # | Result | Evidence |
| --- | --- | --- |
| V-1 | PASS | owner registers, verifies, buys `pro` (fake Paymob callback), connects a number (201), syncs one MARKETING template with an IMAGE header and a URL button (`created: 1`) |
| V-2 | PASS | 20 customers write first; `STOP` and `ستوب` → 2 opt-outs on WhatsApp |
| V-3 | PASS | an invited admin uploads a PNG (`kind: image`), composes with `header_media_id`, preview **18**, audience, schedule |
| V-4 | PASS | two workers: **1 upload, 18 sends, 0 people twice, 1 header id, 18 meters**, 0 sends to the opted-out |
| V-5 | PASS | an invited member: create 403, upload 403 |
| V-6 | PASS | second campaign (rate 2) sent its first batch of 2; platform staff suspended the workspace; next sweep: **0 sends**, `paused` / `workspace_suspended` |
| V-7 | PASS | invariants verify exit 0, 54 checks all 0 |
| V-8 | PASS | 9 run secrets over 160,934 bytes (`pg_dump --data-only`, run log, evidence, API responses): **0 found**; Meta's media id in no log line, evidence row or response, and in the dump only in `public.campaigns` |

## 13. Untracked Files

`E:\wasla` held the same 11 untracked documents before and after both merges
(`git ls-files --others --exclude-standard`, compared byte for byte):
`AI_FINDINGS_REMEDIATION.md`, `AI_SUBSYSTEM_AUDIT.md`,
`API_ROUTES_AND_ADMIN_GAPS.md`, `AUTHORIZATION_MULTI_TENANCY_AUDIT.md`,
`FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md`, `FRONTEND_MASTER_PLAN.md`,
`PLATFORM_ADMIN_API_COVERAGE.md`, `RAG_SUBSYSTEM_AUDIT.md`,
`REAL_META_WHATSAPP_E2E_VERIFICATION.md`, `WORKERS_QUEUES_AUDIT.md`,
`WORKERS_QUEUES_FINDINGS_REMEDIATION.md`. **Collisions with the incoming trees:
none** (`CAMPAIGNS_*` reports were not held untracked). Nothing was staged,
moved or deleted.

## 14. Merges and Tree Equality

In `E:\wasla`, origin confirmed `989584b` just before:

| # | Command | Merge commit | Parents | Conflicts | `git diff --exit-code <incoming> HEAD` | alembic heads |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `git merge --no-ff 427a0f1` "merge: campaigns outbound audit and findings remediation (ADR-134..143)" | `f43e454` | `989584b`, `427a0f1` | none | **exit 0** | 0106 |
| 2 | `git merge --no-ff e38f35f` "merge: campaign header media (ADR-144)" | **`a8e8f24`** | `f43e454`, `e38f35f` | none | **exit 0** | 0107 |

`CANONICAL_MERGED_HEAD = a8e8f24`; `git diff --exit-code e38f35f a8e8f24` →
exit 0. The audit commit `6ecbdd6` landed as an ancestor of merge 1.

## 15. Gates on the Final Canonical Tree

`E:\wasla-cmv-final`, detached at `a8e8f24`, `app.__file__` asserted:

| Gate | Result | Matches `e38f35f`? |
| --- | --- | --- |
| ruff / black / mypy app tests / mypy scripts | clean / 932 unchanged / 0 in 813 / 0 in 11 | yes |
| fresh `wasla_cmv_final` → head | 107 steps, 10.3 s; check clean; 0/0/0; preflight ok; invariants ok | yes |
| test_documentation_truth | 27 passed | yes |
| MIGRATION-BUILT full lane | 7,039 passed / 0 failed / 14 skipped (23:09:41 → 23:51:48 UTC) | yes |
| kept-data sweep, fresh database | 301 passed, 0 skipped; selection equal | yes |
| race suites, once | 48 passed | yes |

## 16. Push

Procedure (section 30 of the brief), run after this report's commit:

```text
git fetch origin --prune
origin/worktree-billing-google-auth == 989584b  (ORIGIN_CANONICAL_START_HEAD; checked)
git merge-base --is-ancestor origin/worktree-billing-google-auth HEAD   -> true (fast-forward)
git push origin worktree-billing-google-auth                            (no force)
git fetch origin; origin == local HEAD
```

No stage branch, helper worktree branch or tag is pushed; `main` is untouched.
The result and the first GitHub Actions run are recorded in a follow-up
commit of this file (section 17), as the previous canonical merge did.

## 17. First GitHub Actions Run

Recorded after the push (follow-up commit).

## 18. Findings

| ID | Severity | Where | What | Status |
| --- | --- | --- | --- | --- |
| **CMV-01** | test defect (no product impact) | `tests/integration/test_campaign_header_media.py::test_many_workers_upload_once_and_send_everyone_once` | with a 0.2 s lease and one 600-a-minute batch, an upload outlasted the lease on a loaded machine; a second worker took the campaign over and uploaded again (HM-04 allows this: two uploads, one id sent), and the test's "one upload" failed. Found by the first spot-check's control at `6d9728e`; reproduced 3 of 5 runs under six CPU-bound processes; would likely have flaked on a slower CI runner | **fixed in Part A** (`14081fe`, test-only: a 5 s lease that outlasts an upload as production's 300 s does, three batches contended by every worker; 5 of 5 under the same load); Part A's lanes re-run on it; **this whole verification re-run on `e38f35f`** |

No application, migration or documentation defect was found. Part B changed
nothing in the application, the tests or the migrations.

## 19. Infrastructure Created by This Stage

| Kind | Name | Notes |
| --- | --- | --- |
| Worktree + branch | `E:\wasla-campaign-media`, `campaigns-header-media-20261015` | Part A; untracked `audit/` (probes, runners, mutation results) |
| Worktrees (detached) | `E:\wasla-cmh-mut` (50a9df3), `E:\wasla-cmv-verify`, `E:\wasla-cmv-lane`, `E:\wasla-cmv-mut` (e38f35f), `E:\wasla-cmv-origin` (989584b), `E:\wasla-cmv-final` (a8e8f24) | never edited (mutation worktrees restored byte for byte) |
| Containers, Part A | `wasla-cmh-pg` 55931, `-pg2` 55932, `-redis` 56931, `-redis2` 56932, `-redis3` 56933, `-minio` 59931 | left running |
| Containers, Part B | `wasla-cmv-pg` 55941, `-pg2` 55942, `-redis` 56941, `-redis2` 56942, `-redis3` 56943, `-minio` 59941 | left running |
| Images | `wasla:cmh`, `wasla:cmv` (runtime target) | liveness containers removed |
| Scratch | session scratchpad `cmh/`, `cmv/` (populate, checksums, E2E harness and evidence, lane outputs) | |

Untouched: the developer stack (`wasla-api-1`, `wasla-postgres-1`,
`wasla-redis-1`), the audit's and the remediation's containers and worktrees.

## 20. Remaining External Actions

```text
subscribe the message_template_status_update webhook field in the Meta App (CMQ-05)
real-Meta lane (only when authorised):
    one read-only template sync compared with WhatsApp Manager
    one campaign with an IMAGE header to 1-3 numbers the owner names: the image arrives,
        wamids, sent/delivered/read, rows reconciled
    the media id lifetime and reuse Meta actually honours
    a STOP from the owner's phone -> the next campaign skips the number
    a new marketing template's first send: held_for_quality_assessment? (EXT-CMP-08)
    whether 131050 / 131053 arrive as asynchronous failed statuses (EXT-CMP-04)
    past the portfolio limit on a test portfolio (EXT-CMP-01)
Egypt (+20) marketing and utility rates (EXT-CMP-10)
CMQ-07 legal review, then the contact-import stage; CMQ-03/04/06/08/10; LOCATION headers
FRONTEND_MASTER_PLAN.md Phase 13: header-file upload, preview, the new fields and refusal codes
remove the campaigns infrastructure after review (audit, remediation and this stage's, section 19)
the deploy, with the RUNBOOK rule: stop old processes before alembic upgrade
whether and when the canonical branch is merged into main
```

## 21. Final Verdict

Pending the first GitHub Actions run (section 17); recorded in the follow-up commit.
