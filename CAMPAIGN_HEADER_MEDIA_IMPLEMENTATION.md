# Campaign Header Media — Implementation

| | |
| --- | --- |
| Date | 2026-10-15 |
| Type | **Implementation** of CMP-05's deferred half (CAMPAIGNS_FINDINGS_REMEDIATION.md section 9, 30) |
| Base | `CMP_REM_HEAD` = `427a0f1` (campaigns findings remediation; unmoved) |
| Branch / worktree | `campaigns-header-media-20261015` at `E:\wasla-campaign-media` |
| Alembic | `0106` → **`0107`** (single head) |
| ADR | **ADR-144**; ADR-137 and ADR-134 amended |
| Final code head | **`14081fe`** (CMV-01 test fix, section 24); first verified head `1f86c63` |
| Pushed / merged / deployed | No (Part B of the stage merges and pushes) |
| Meta / production | Not touched. No upload, send or template sync against real Meta |

---

## 1. Executive Summary

**Verdict: CAMPAIGN HEADER MEDIA IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING.**

A workspace owner or admin uploads an image, a video or a PDF to Wasla; a
campaign on a template with an IMAGE, VIDEO or DOCUMENT header names it; the
campaign worker holding the lease uploads it to Meta **once per campaign and
connection** and sends every recipient the **same Meta media id**. Counted at
the transport, never as rows:

| Scenario | Uploads | Sends | People twice |
| --- | --- | --- | --- |
| 30 people, one worker | **1** | 30 | 0 |
| 30 people, 2 workers / 4 workers | **1** / **1** | 30 / 30 | 0 / 0 |
| lease taken over during the upload | 2 (one never sent) | 30, one id | 0 |
| worker held before its upload, past its lease | **1** | 10 | 0 |
| two campaigns sharing one file | 2 (one each) | 5 + 5 | 0 |
| id older than 25 days | +1 before the batch | — | 0 |
| send answered 131053 once | 2 | N + 1 (that person resent once) | 0 |
| send answered 131053 twice | 2 | **2**, then FAILED | 0 |

Every failure of the header is a campaign-level fact that spends nobody's
attempt (section 10). LOCATION and GIF headers stay refused. Every remediation
guarantee holds: the two-worker, reservation, served-gate, answer-class,
stop/resume and invariant suites pass unchanged except the one test whose
expectation this stage changes (section 7), and the remediation's regression
mutants are still killed.

**What remains EXTERNAL:** that Meta honours one uploaded id across many
messages for its stated 30 days - its pages recommend sending by uploaded id
and state the lifetime, but none says "reusable" in words (section 3).

## 2. Starting Repository State

```text
canonical          worktree-billing-google-auth at 989584b = origin (0 ahead / 0 behind)
CMP_REM_HEAD       427a0f1 (campaigns-findings-remediation-20261015; not moved)
audit              6ecbdd6 = 989584b + CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md only
alembic            0106 (remediation), 0100 (canonical)
Python             3.12.7
PostgreSQL         16.15 (pgvector/pgvector:pg16)
Redis              7.4.11
Docker / Compose   29.8.0 / v5.5.1
app.__file__       E:\wasla-campaign-media\app\__init__.py (asserted per lane)
untracked E:\wasla 11 documents, untouched
```

Infrastructure of this stage: `wasla-cmh-pg` 55931, `wasla-cmh-pg2` 55932,
`wasla-cmh-redis` 56931, `-redis2` 56932, `-redis3` 56933, `wasla-cmh-minio`
59931 (bucket `wasla-media`); the remediation's (`wasla-cmp-rem-*`) and the
audit's (`wasla-cmp-*`) containers were left alone.

## 3. Meta's Documentation

Read 2026-10-15. Where Meta differs from the prompt, Meta was used.

| Value | Meta | Prompt | Used | Page |
| --- | --- | --- | --- | --- |
| Upload | `POST /{phone-number-id}/media`, `file`, `type`, `messaging_product=whatsapp` | same | same | business-phone-numbers/media.md |
| Image | JPEG, PNG; 5 MB; 8-bit RGB/RGBA | same | same | media.md |
| Video | **MP4 and 3GPP**; 16 MB; H.264 video, AAC audio | MP4 | MP4 and 3GPP | media.md |
| Document | text, Excel, Word, PowerPoint, PDF; 100 MB | PDF, 16 MB | PDF, 16 MB (Wasla's cap) | media.md, templates/components.md ("a document such as a PDF") |
| Media id lifetime | "Media IDs returned by the API expire after 30 days" | ~30 days | 30 days; refresh at 25 | media.md |
| Reuse across messages | not stated | assumed | assumed - EXTERNAL VERIFICATION; Meta "recommends that you upload your media assets and use their IDs when sending messages" | templates/template-media.md |
| Header parameter | `{"type": "header", "parameters": [{"type": "image", "image": {"link"\|"id"}}]}` | same | `id` form; video alike | template-media.md |
| Document filename | "`filename`: Document filename, with extension" | same | the Meta-safe upload name | messages/document-messages.md |
| Header formats | IMAGE, VIDEO, **GIF** (Marketing Messages API), DOCUMENT, LOCATION | four | GIF refused like LOCATION | templates/components.md |
| Expired / unknown id on a send | **no code documented** | "Meta's code" | 131053 "Unable to upload the media used in the message" - the only media-in-message code | cloud-api/support/error-codes |
| Upload refused | no dedicated codes | — | handled by class: any 4xx not throttle/credential/connection → `header_media_refused` with the code | error-codes |

Pages are under developers.facebook.com/documentation/business-messaging/whatsapp/
(the older /docs/whatsapp/cloud-api/guides/send-message-templates page now
redirects there). Two prompt values were corrected by Meta's pages (3GPP video;
GIF headers exist). Neither contradicts HM-01..HM-06; the reuse question is
recorded, not a stop: nothing Meta says forbids it and its own guidance assumes it.

## 4. Decisions Applied

| ID | Decision | Applied as |
| --- | --- | --- |
| HM-01 | Uploaded through Wasla, never a link | `POST /campaigns/header-media`; the worker uploads bytes; no URL anywhere |
| HM-02 | One asset route, then the campaign references it | three routes; `header_media_id` on create; table `campaign_header_media` (section 5); immutable, tenant-scoped (404), counts against `STORAGE_BYTES` (402) |
| HM-03 | Types by bytes, sizes per header format | JPEG/PNG image 5 MB, MP4/3GPP video 16 MB, PDF 16 MB; wrong/missing/extra → `422 template_parameters` naming the header; LOCATION and GIF → `422 template_unsupported` |
| HM-04 | Upload once per campaign and connection, under the lease | after `hold()`, before the first `take`; id written by one statement conditioned on the lease token, own transaction; connection's own credential |
| HM-05 | Keep the id fresh | `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS` = 2,160,000 (25 days); re-upload when older, after a long pause, or through another number |
| HM-06 | Every failure campaign-level | section 10 |
| HM-07 | Lifecycle and retention | retention skips files a draft/scheduled/running/paused campaign names; purge ledger; served gate before any upload |
| HM-08 | Preview | `CampaignRead.header_media` = `{id, kind, mime_type, size_bytes, filename, created_at}` or null |

Not taken, as locked: LOCATION headers (DEFERRED), per-recipient header images,
per-recipient body variables, contact import, malware scanning. Follow-ups and
manual templates do not get media headers from this code path (it is the
campaign worker's); nothing was widened.

## 5. Migration 0107

`20261015_0107_campaign_header_media.py` (ADR-144):

| Adds | Online how |
| --- | --- |
| `audit_action` label `campaign_header_media_uploaded` | autocommit block |
| table `campaign_header_media` (kind CHECK, byte_size > 0, the `message_media` storage-state CHECK, `UNIQUE(tenant_id, id)`, `UNIQUE(storage_key)`, retention and pending partial indexes) | new, empty |
| `campaigns.header_media_id`, `header_provider_media_id` (255), `header_provider_uploaded_at`, `header_provider_account_id` | nullable, no default: catalogue-only |
| `fk_campaigns_tenant_header_media`, `fk_campaigns_tenant_header_provider_account`, `ck_campaigns_header_provider_complete`, `ck_campaigns_status_reason_known` (+2 reasons) | `NOT VALID` → `VALIDATE`, 15 s `lock_timeout` |
| `ix_campaigns_header_media_id` (partial) | `CONCURRENTLY`, autocommit, INVALID leftover dropped first |

**Why a new table** (decided after reading MEDIA.md and the model):
`message_media` cannot hold an asset by adding one nullable owner column - its
`message_id` and `conversation_id` are NOT NULL and tenant-composite, `UNIQUE
(message_id, position)` is its replay guard, and media recovery releases an
agent turn on the row's conversation. Relaxing two NOT NULLs would change what
every reader may assume. Everything about the object is shared instead.

**Downgrade** refuses ("Nothing has been changed", stamp stays 0107) while any
asset exists, any campaign names one or Meta's id, any campaign carries a
header stop reason, or any audit entry records an upload.

## 6. The Asset Route

| Route | Who | Answer |
| --- | --- | --- |
| `POST /api/v1/campaigns/header-media` (multipart `file`) | owner, admin | 201 `{id, kind, mime_type, size_bytes, filename, sha256, created_at}` |
| `GET /api/v1/campaigns/header-media/{id}` | any member | metadata (no sha256, no key) |
| `GET /api/v1/campaigns/header-media/{id}/file` | any member | bytes, `attachment`, `nosniff`, `no-store` |

Write path (`CampaignHeaderMediaService.upload`): `resolve` from the bytes →
kind → the WhatsApp policy's `media_limits` → `STORAGE_BYTES` reserve under the
workspace's advisory lock + intent row `pending` (TX1, committed by `released`)
→ `storage.write_object` with nothing held → TX2 row locked, `stored`,
`STORAGE_USED` metered, audited with `{kind, byte_size}` only. A write that
fails leaves the intent for reconciliation (503).

**Found by the tests (fixed):** the request-size middleware held the new route
to the authenticated default cap - every header file above it would have been
413 in a deployment. Added to `UPLOAD_ALLOWANCES` with the attachment route's
cap. `read_capped` moved to `app/api/uploads.py` for both routes.

## 7. Composition and Preview

`template_parameters.refusal` (ADR-137 amended): `FILE_HEADER_FORMATS` =
IMAGE, VIDEO, DOCUMENT need a file of their kind; TEXT and no header refuse one;
any other format (LOCATION, GIF, unknown) is `template_unsupported`. A file not
stored is `422 header_media_not_stored` at create and at schedule. The per-batch
`_blocking_reason` re-checks with the file's kind.

**Changed test:** `test_a_media_header_is_refused_at_composition` →
`test_a_media_header_without_its_file_is_refused_at_composition` (IMAGE without
a file is now `template_parameters`; LOCATION is still `template_unsupported`).

**"Cannot change after scheduling"**: no route changes a campaign's fields -
only named transitions; `PATCH` is 405 and the audience and schedule bodies
refuse an unknown `header_media_id` with 422 (test). The file is fixed at create.

## 8. The Upload, the Lease and the Send

```text
claim (lease token) -> claim_by_id (row locked) -> served? blocked? -> claim_pending
  -> media header?  id fresh, through this number?  -- yes --> use it
       -- no --> hold(token, renew)  -- no --> stop, nothing uploaded
                 released: read the object, SHA-256 checked   (no transaction held)
                 upload_header_file: adapter.sender (own credential) -> prepare
                                     released across the Meta call
                 record_header_upload(... WHERE lease_token = token) ; COMMIT
                     0 rows -> stop: this worker's upload is never sent
  -> for each recipient: hold, served, take, send with the one id
```

`MessagingService.upload_header_file` reuses the adapter's sender and
`prepare` - the attachment upload - so there is one Meta upload client.
`WhatsAppClient.upload_media` now classifies every ending (all still
`ExternalServiceError`s, attachment sends unchanged; 14 unit tests).

**Found while testing (fixed):** reading the template once for the batch and
holding the object kept it in the session's identity map, so the per-send
registry check saw a stale status and a template paused by another sender
mid-batch was sent 6 times instead of 2. The header check now reads the format
without keeping the row.

## 9. Refresh

Reused while `clock - header_provider_uploaded_at < 25 days` and
`header_provider_account_id = account_id`. Tested: batch at day 0 (1 upload),
+2 min (reused), +26 days (1 re-upload, the last 5 sends carry the new id);
paused 31 days then rescheduled (1 re-upload before the first send).

## 10. Failures (HM-06)

| Class | Outcome | Requests to /messages | Attempts spent | Test |
| --- | --- | --- | --- | --- |
| stored object deleted | PAUSED `header_media_unavailable` | 0 | 0 | `..._gone_or_altered...[deleted]` |
| stored object altered (SHA-256) | same | 0 | 0 | `[altered]` |
| file purged by retention | same; schedule refused `header_media_not_stored` | 0 | 0 | lifecycle |
| store unreachable | waits; nothing recorded | 0 | 0 | (code path; class as reconciliation) |
| upload refused (400, code 100) | FAILED `header_media_refused`, "code 100" in `last_error`, template APPROVED | 0 | 0 | `..._refusing_the_upload...` |
| connect error | waits; PAUSED `provider_unreachable` after 6 h | 0 | 0 | `..._unreachable_meta...` |
| read timeout / 500 / 2xx without id | no id; next batch uploads again and sends | 0 then N | 0 | `..._uncertain_upload...` ×3 |
| 429 / throttling code | waits; next batch uploads again | 0 | 0 | client unit test |
| 401 / 190 on upload | FAILED `credential_refused` | 0 | 0 | `..._refused_credential...` |
| workspace token undecryptable | FAILED `credential_refused`; **0 uploads** (never the platform token) | 0 | 0 | `..._never_replaced_by_the_platforms` |
| 131053 once | 1 re-upload; that person resent; campaign completes | N + 1 | 0 | `..._media_unknown_on_a_send...` |
| 131053 twice | FAILED `header_media_refused` after **2** sends; id forgotten | 2 | 0 | `..._media_unknown_twice...` |

New `CampaignStopReason` values `header_media_unavailable`,
`header_media_refused` (CHECK extended; `stops_total` domain test updated).

## 11. Retention and Lifecycle

| Case | Result |
| --- | --- |
| file older than retention, campaign draft / scheduled / running / paused | kept (4 tests) |
| file named only by completed / cancelled / failed, or by none | purged, object gone (4 tests) |
| interrupted write, object present / absent | reconciled `stored` / `absent` |
| workspace purge | row deleted, key in `media_purge_objects`, object deleted by the purge worker, ledger empty |
| workspace suspended before the first batch | 0 uploads, 0 sends, PAUSED `workspace_suspended` |

## 12. Invariants CR-08..CR-10

| Check | Injection test | Meaning |
| --- | --- | --- |
| `cr08_live_media_header_campaign_without_a_matching_file` | 2 (no file; wrong kind) | live media-header campaign without a file of the header's kind |
| `cr09_header_media_uploaded_through_another_number` | 1 | Meta's id through another number |
| `cr10_campaign_naming_another_workspaces_header_file` | 1 (FK trigger set aside in the rolled-back transaction) | defence in depth beside the composite key |

The oracle census derives its expected names from the tuple, so it needed no
change.

## 13. Observability

`wasla_campaign_events_total{event}` += `header_media_uploaded`,
`header_media_refreshed`, `header_media_reupload_on_send`;
`wasla_campaign_stops_total{reason}` += the two reasons. Alert
`CampaignHeaderMediaRefused` (warning) with a promtool unit test: fires on a
refusal, clears after an hour, silent on `header_media_unavailable`. `promtool
check rules` 75 rules SUCCESS; `promtool test rules` SUCCESS; `amtool
check-config` valid. Meta's id, the key and file names appear in no label, log
line or audit entry (leakage test with `caplog` and the audit trail).

## 14. Mutation Matrix

Strict runner (`audit/run_header_media_mutations.py`, untracked; results in
`audit/mutations_results.txt`): every anchor exact and found once, named
killers with `-x`, every file restored byte for byte (SHA-256), detached
worktree `E:\wasla-cmh-mut` at `50a9df3`, its own database `wasla_cmh_mut`.
Control: **118 passed**. `git status` clean after the run.

| # | Mutant | Verdict | Killer |
| --- | --- | --- | --- |
| M-H01 | upload per recipient | KILLED | `test_thirty_people_one_upload_thirty_sends_one_id` |
| M-H02 | upload without `hold()` | **REDUNDANT GUARD** (proven) | survives behind `claim_by_id`'s row lock, held until the upload's first commit; **M-H02+LOCK** (no hold, no row lock) KILLED by `test_a_worker_held_before_its_upload_past_its_lease_uploads_once`; LOCK-alone (row lock removed, hold kept) survives - `hold()` is the guard once the lock is gone |
| M-H03 | id write ignores the token | KILLED | `test_a_lease_taken_over_during_the_upload...` |
| M-H04 | refresh age ignored | KILLED | `test_an_id_older_than_the_refresh_age...` |
| M-H05 | kind check removed | KILLED | `test_a_wrong_missing_or_extra_file...[IMAGE-pdf]` |
| M-H06 | kind from content_type | KILLED | `test_the_kind_is_the_bytes...` |
| M-H07 | refused upload sends anyway | KILLED | `test_meta_refusing_the_upload_fails_the_campaign...` |
| M-H08 | uncertain upload records an id | KILLED | `test_an_uncertain_upload_records_no_id...[200_no_id]` |
| M-H09 | 131053 re-uploads without limit | KILLED | `test_media_unknown_twice...` |
| M-H10 | retention ignores live campaigns | KILLED | `test_retention_never_removes_a_file_a_live_campaign_names[draft]` |
| M-H11 | asset read drops tenant filter | KILLED | `test_another_workspaces_file_is_not_found_anywhere` |
| M-H12 | Meta's id in `CampaignRead` | KILLED | `test_no_campaign_response_carries_metas_id_or_a_key` |
| M-H13 | platform credential fallback | KILLED | `test_an_unreadable_workspace_token_is_never_replaced...` |
| M-H14 | CR-08 vacuous | KILLED | `test_cr08_live_image_header_campaign_without_its_file` |
| M-C01+M08 | claim and hold removed | KILLED | `test_a_batch_longer_than_its_lease_is_not_shared` |
| M-C12 / M-C13 | header / button not emitted | KILLED | `..._sends_every_component_meta_needs` |
| M-C14 | spec ignored at composition | KILLED | `..._refused_at_composition` |
| M-C15 | 132000 per recipient | KILLED | `..._parameter_refusal_costs_one_request...[132000]` |
| M-C18 | withdrawal continues the batch | KILLED | `..._paused_elsewhere_mid_batch...` |
| M-C24 | hold ignores status | KILLED | `..._pause_or_cancel_mid_batch...[pause-paused]` |

**Exit criterion met:** meaningful survivors 0 (14 of 14 Part A mutants killed
or classified with a proof; 7 of 7 regression mutants killed).

## 15. Model-Built Test Results

MODEL-BUILT (`WASLA_TEST_SCHEMA` unset), `wasla_cmh_models` on `wasla-cmh-pg`
(55931), Redis `wasla-cmh-redis` (56931), full `tests/`, coverage omitted
(deviation, as in earlier stages: CI's `--cov` slows the suite about 4x and
sets no `fail_under`).

| Run | Head | Collected | Passed | Failed | Skipped | xfailed / xpassed | Time (UTC) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `05e4aa1` | 7,053 | 7,034 | **1** | 18 | 0 / 0 | 43 min 35 s |
| 2 (final) | **`1f86c63`** | 7,053 | **7,035** | **0** | 18 | 0 / 0 | 17:53:03 → 18:38:07 |

Run 1's failure was `test_channel_topups.py::test_staff_state_a_products_channel_and_plans_and_every_change_is_audited`:
it ordered two audit entries by `occurred_at`, which is the transaction's
`now()` - both entries share it inside the test's single transaction, so their
order is arbitrary. A pre-existing test defect (the file is unchanged since
`cb029fd`, before canonical `989584b`), unrelated to this stage; fixed
test-only in `1f86c63` (each entry found by its action), run three times, and
**both full lanes were run again on `1f86c63`**.

Skips (exactly the known set): real-provider OpenAI contract x11 (no key);
schema parity x4 (model-built only); tmpfs x1 (`WASLA_TEST_TMPFS`); the AI and
tool kept-data-only checks x2. The remediation recorded 93 skips because it
ran without an object store; this stage configured MinIO (`TEST_S3_*`), so the
S3 and storage suites ran.

Collected at `427a0f1`: 6,955; at `1f86c63`: 7,053 - **+98, exactly the
tests this stage added**: `test_campaign_header_media.py` 24,
`test_campaign_header_media_api.py` 42 (21 x local and MinIO),
`test_campaign_header_media_lifecycle.py` 11, `test_campaign_invariants.py`
+4, `test_whatsapp_upload_classes.py` 14, and +3 generated by the route guards
(`test_body_allowances.py` +2, `test_request_field_bounds.py` +1).

## 16. Migration-Built Test Results

MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, fresh `alembic upgrade head`),
`wasla_cmh_migrations` on `wasla-cmh-pg2` (55932), Redis `wasla-cmh-redis3`
(56933) - never sharing a server with the model-built lane.

| Run | Head | Collected | Passed | Failed | Skipped | Time |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `05e4aa1` | 7,053 | 7,039 | 0 | 14 | 43 min 22 s |
| 2 (final) | **`1f86c63`** | 7,053 | **7,039** | **0** | 14 | 17:53:05 → 18:38:07 |

Skips: OpenAI x11, tmpfs x1, kept-data-only x2. Schema parity ran and passed.

## 17. Kept-Data Sweep, Races, Alembic, Docker

**Kept-data sweep** - `ci.yml`'s exact command (`grep -l ai_harness
tests/integration/test_*.py | grep -v _invariants` = 28 suites, then
`test_ai_invariants.py` and `test_tool_invariants.py`), `WASLA_TEST_KEEP_AI_DATA=1`,
migration-built, a fresh database each run, nothing else on that server:

| Run | Passed | Failed | Skipped |
| --- | --- | --- | --- |
| 1 | 301 | 0 | 0 |
| 2 | 301 | 0 | 0 |
| 3 | 301 | 0 | 0 |

**Races**, three consecutive runs (`test_campaign_two_workers.py`,
`test_campaign_reservation.py`, `test_topup_concurrency.py`,
`test_campaign_header_media.py` - 2 and 4 workers, lease takeover, held before
upload): **48 passed** x3 (116 s, 120 s, 120 s).

**Alembic** (`wasla_cmh_mig`, fresh):

```text
alembic heads                     0107 (head) - single
fresh 0001 -> 0107                8.9 s
alembic check                     No new upgrade operations detected
catalogue                         0 NOT VALID, 0 invalid indexes, 0 disabled triggers
db_preflight verify               ok
omnichannel_invariants verify     ok (CR-01..CR-10 = 0)
head -> 0100 -> head              clean; check clean
populated 0106 -> 0107 -> 0106 -> 0107   wasla_cmh_probe (P07 data): 55 tables, 80 rows,
                                  checksums over the 0106 columns equal at all four points
refusing downgrade (after P07b)   RuntimeError "... Nothing has been changed: campaign header
                                  media files: 1; campaigns naming a header file or Meta's id
                                  for one: 1"; stamp stays 0107; preflight ok
test_migration_downgrade_atomicity, test_schema_parity   green (both lanes)
```

**Monitoring:** `promtool check rules` 75 rules SUCCESS; `promtool test rules`
SUCCESS; `amtool check-config` valid.

**Docker:** `docker build --target runtime` ok (`wasla:cmh`); the container
(`ENVIRONMENT=staging`, generated `JWT_SECRET`) answered `{"status":"alive"}`
after 4 s, two empty replies first (the remediation saw two as well).

## 18. Ruff / Black / MyPy

```text
ruff check . (tracked tree)   All checks passed!
black --check .               909 files would be left unchanged
mypy app tests                Success: no issues found in 813 source files
mypy scripts                  Success: no issues found in 11 source files
```

No `# type: ignore` in application code. The untracked `audit/` probes are not
in CI and are excluded.

## 19. Documentation Changes

| File | Change |
| --- | --- |
| DECISIONS.md | ADR-144; ADR-137 and ADR-134 amended |
| docs/CAMPAIGNS.md | the upload in the send flow, the header-failure table, routes, `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS`, the one-file and LOCATION/GIF limits, the EXTERNAL reuse note |
| docs/MEDIA.md | the header file as a writer of the protocol; retention keeps a live campaign's file |
| docs/API.md | three routes, 212 operations, `header_media_id`, `header_media`, every refusal code |
| docs/WHATSAPP.md | the upload classes and the header component, citing Meta's pages |
| docs/RUNBOOK.md | `header_media_unavailable`, `header_media_refused` and the waiting state in "Campaigns are not sending"; "Campaign header media (0107)" |
| docs/OBSERVABILITY.md | events and `CampaignHeaderMediaRefused` |
| README.md | migrations `0001`-`0107` (in the migration's commit) |
| .env.example | `CAMPAIGN_HEADER_MEDIA_REFRESH_SECONDS` |

`test_documentation_truth` passed at every commit of this branch.

## 20. Before / After

| Scenario | Before (427a0f1) | After | Verdict |
| --- | --- | --- | --- |
| IMAGE-header template in a campaign | 422 `template_unsupported` (P07) | created; P07b: **1 upload, 25 sends, one header id** | PASS |
| VIDEO / DOCUMENT header | 422 `template_unsupported` | sent; document carries its filename | PASS |
| LOCATION header | 422 `template_unsupported` | 422 `template_unsupported` (deferred) | PASS |
| GIF header | **not refused** (would send without its header) | 422 `template_unsupported` | PASS (found here) |
| Wrong asset kind for the header | - | 422 `template_parameters`, header | PASS |
| 2 / 4 workers, one campaign with an image | - | 1 upload, 0 people twice | PASS |
| Lease taken over between upload and write | - | 2 uploads, one id sent, 0 twice | PASS |
| Media id older than the refresh age | - | re-uploaded before the batch | PASS |
| Stored object missing | - | PAUSED `header_media_unavailable`, 0 sends | PASS |
| Meta refuses the upload | - | FAILED `header_media_refused`, 0 sends, template kept | PASS |
| Meta says the media is unusable on a send (131053) | - | 1 re-upload; twice: FAILED after 2 sends | PASS |
| Retention while the campaign is live | - | file kept | PASS |
| Fresh database to head | 106 steps | 107 steps, check clean, catalogue 0/0/0 | PASS |

P07 after (`audit/p07_header_template.py`, unchanged probe): IMAGE without a
file is now refused `template_parameters` - "This template's header is an
image; choose a header file."; the TEXT-header payload is unchanged.

## 21. Updated Score

Same dimensions as the remediation's section 33; only "Template registry and
parameters" may change. **A self-score is not an independent measurement.**

| Area | Weight | Remediation | Now | Remaining |
| --- | --- | --- | --- | --- |
| Template registry and parameters | 7 | 6 | **7** | media headers sent; LOCATION and per-recipient images are deferred scope, not defects |
| every other area | 93 | 88 | 88 | unchanged |
| **Total** | **100** | **94** | **95** | |

## 22. Remaining External Actions

```text
real-Meta lane (only when authorised): one campaign with an IMAGE header to 1-3 numbers the
    owner names - the image arrives, wamids, statuses, rows reconciled; the media id lifetime
    and reuse Meta actually honours; whether 131053 arrives synchronously or as a failed status
subscribe message_template_status_update in the Meta App (CMQ-05)
CMQ-07 legal review; the deferred CMQ-03/04/06/08/10; LOCATION headers
FRONTEND_MASTER_PLAN.md Phase 13: the header-file upload, preview, new fields and refusal codes
```

## 23. Part A Verdict

**CAMPAIGN HEADER MEDIA IMPLEMENTED WITH EXTERNAL VERIFICATION REMAINING.**

A media-header campaign uploads its file once per campaign and connection and
sends one id to everyone; two and four workers upload once and nobody receives
twice; every header failure is campaign-level and costs at most one request
(two for the documented re-upload-once case); LOCATION and GIF stay refused;
every meaningful mutant is killed and the remediation's regression mutants are
still killed; both full lanes are 0 failed on `14081fe`, the final code of the
branch (section 24; the report commit after it changes only this file).

## 24. Addendum - CMV-01, found by Part B, fixed here

Part B's first independent verification of `6d9728e` passed its lanes, the
populated upgrade, APP-E2E and the kept-data sweep, but the **control** of its
mutation spot-check, run while the kept-data sweep loaded the machine, failed
one test: `test_many_workers_upload_once_and_send_everyone_once[4]` - **2
uploads** where it asserted 1.

**Cause (reproduced, a test defect).** The test used a 0.2 s lease and one
600-a-minute batch. Under CPU load an upload outlasted the lease; another
worker took the campaign over and uploaded again - exactly the takeover case
HM-04 allows (at most two uploads, one id sent; `test_a_lease_taken_over_during_the_upload...`).
The application behaved as specified; the test was stricter than its own
lease. Reproduced in **3 of 5** runs with six CPU-bound processes beside it.

**Fix (`14081fe`, test-only).** Every worker sweeps each round of a
three-batch campaign (rate 10) with a 5 s lease, which outlasts an upload as
the production 300 s lease does: one upload in all, every batch reusing the
recorded id, nobody twice. **5 of 5** under the same load (with the takeover
and held-before-upload tests).

Re-run on `14081fe` (both full lanes, as the brief requires after a fix):

| Lane | Head | Collected | Passed | Failed | Skipped | Time (UTC) |
| --- | --- | --- | --- | --- | --- | --- |
| MODEL-BUILT (`wasla-cmh-pg`) | `14081fe` | 7,053 | **7,035** | **0** | 18 | 20:37:49 → 21:30:35 |
| MIGRATION-BUILT (`wasla-cmh-pg2`) | `14081fe` | 7,053 | **7,039** | **0** | 14 | 20:37:51 → 21:30:35 |

Races on `14081fe`, three consecutive runs: **48 passed** x3. Skips: the known
set, unchanged. The mutation matrix (section 14) needs no re-run: no mutant's
killer is this test, and the code under every mutant is unchanged.
