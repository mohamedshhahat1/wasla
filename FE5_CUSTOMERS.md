# FE-5 — Customers and Leads (ADR-199)

**Verdict (at the report commit):** every focused local gate green; merged as one normal merge commit; the push follows this
commit. GitHub's `frontend.yml` and `ci.yml` are the one full check - their runs, Part C and the final verdict line are in
the addendum at the end of this file.

## Phase status

| Phase | State | Evidence (E:\wasla-fe5-notes\) | Commit / action |
| --- | --- | --- | --- |
| A0 state, skills, untracked, infrastructure | done (session 1, 2026-11-16 01:52 UTC) | evidence/untracked-start.txt, logs/venv.log | worktree E:\wasla-fe5-fix, branch fe5-customers-20261116 from 9d15210 |
| B1 inventory, "before" probes | done (session 1) | probes/ | no commit |
| B2 CONTACT (0140) | done | logs/gb5-1_focused.log | 4ecba5e |
| B3 REVOKED | done | logs/gb5-1_focused.log | 242f760 |
| B4 NGINX | done | probes/P-H13b_nginx.txt | 3d631a2 |
| B5 GRAPH, FOLDING, CODES | done | logs/gb5-1_focused.log | 2a7ce91 |
| B6 seed helpers, contract, docs | done | logs/gb5-2_neighbours_rerun.log | b006d65 = BE_HEAD |
| A1 install | done (48 min) | logs/fg5-1_install.log | lockfile unchanged |
| A2 composites | done | logs/final_fg5-3.log | cd31a2c, c05cf3e |
| A3 contacts | done | | 7029f18 |
| A4 leads | done | | f65395d |
| A5 panel, ICU | done | | 65cf64c |
| A6 session rows, preview notice | done | | 92ae735 |
| A7 e2e image, smoke, journeys, measurement, audit | done | logs/smoke.log, final_*.log, screens/, skills/ | f75d4ec, 4e592e5, eb45619 |
| A8 frontend.yml, docs, ADR | done (frontend.yml needed no change) | | d620c0e |
| A9 mutation, diff guard, gates | done | mutation/be_results.log, mutation/fe_results.log | FE5_HEAD = eb45619 |
| Part B merge, report, push | see sections 23-25 and the addendum | | |
| Part C previews | see the addendum | | |
| Part D cleanup | see E:\wasla-cleanup\CLEANUP_fe5_2026-11-16.md | | |

## 1. Executive Summary

**For the owner first.**

- **What you can open.** The customers screens need an API, so walk them locally (`frontend/README.md`, "Customers and
  leads"): FE-0's e2e profile up, the web app on localhost, a customer message through the fake webhook, then `/contacts`
  and `/leads`. The captures are in `E:\wasla-fe5-notes\screens\contact-sheet.html` (18 captures, en / ar, 1280 / 390; not
  committed, not a baseline). The previews still have no API; after Part C their "/" says so.
- **How the request was read.** FE-5 exactly as the plan defines it (APP-CUS-01..03, APP-INBOX-03's FEB-19 part), with
  FEB-19 folded into this stage's backend half, FEB-34 built together with BE1's FEF-27, FEB-35, FEF-26, FEF-28 and FEF-33.
  Testing on the BE1 / FE-4 basis: focused local gates, one push, GitHub as the full check.
- **Open questions - the defaults were used.** OQ-1 the contact's edit is the name only. OQ-2 any member may name a contact
  (audited). OQ-3 a withdrawn member stays signed in with "Open another workspace" / "Sign out". OQ-4 the previews show a
  notice. OQ-5 the consent history starts with this stage; an older opt-out is one marked row. OQ-6 folding applies in
  every workspace.
- **Where the code differed from the prompt, and the code won** (each recorded in section 5):
  1. Consent is **per channel** (ADR-131): there is no person-wide opt-out. The contact page shows one consent line per
     channel; the history carries the channel.
  2. A **reload sends no workspace**, so a withdrawn member's reload never met the refusal: it lands in a workspace they
     still belong to, or on "create a workspace". `membership_revoked` on refresh is met by a tab that still names the
     workspace (its token expired); that tab now shows the access-removed state. "Connection lost" for a withdrawn member is
     gone in both cases.
  3. Signing in when every workspace is suspended **succeeds without a workspace** (case "a" of the prompt, in a variant):
     the page now shows the suspended state instead of the shell.
  4. The lead graph in the code allows skips (New → Qualified, Contacted → Proposal, Qualified → Won) that docs/CRM.md's
     drawing did not show. `next_statuses` reads the code's graph; CRM.md now says so.
  5. Turkish folding needed more than the i-letters: "isik" → "Işık" needs ş → s. The fold covers İ I ı and Ş Ğ Ç Ö Ü.
  6. The opt-out is marketing consent: the inbox composer does not change when one is recorded (it stops only when the API
     refuses a send). The panel shows the consent state itself.
- **Not done, said plainly** (section 27 lists each): a separate keyboard-only journey and a focus-not-obscured sweep were
  not written (axe ran inside the journeys; Storybook's keyboard suite runs in CI); ui-ux-pro-max's pre-delivery checklist
  was not re-run on the finished screens; impeccable's audit ran once from its playbook without its detector (it downloads
  its engine); eleven of the 44 listed mutants were not run (named in section 18); EXPLAIN plans were not recorded;
  PreviewNotice is not in the visual-roles set (it needs a preview build) - it has a story and unit tests.
- **Findings to know:** FEB-36 (the default workspace at sign-in when it is suspended: the API opens none - a product
  decision if another active workspace should be preferred; today it already is, P-H12b reads as "skips suspended").
  FEF-39 (a neutral badge on the secondary header band loses its pill - audit P2). FEF-40 ("Tags" said twice on the lead
  page - audit P2). The ar / fr / tr texts are unreviewed drafts (FEQ-08).

## 2. Source Branches and Exact Heads

| Name | Value |
| --- | --- |
| FE4_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | 9d15210 (origin = local at the start, 0/0) |
| main | 8e3a1ba (untouched) |
| BE_HEAD | b006d65 |
| FE5_HEAD | eb45619 |
| CANONICAL_MERGED_HEAD | ec0454e (parents 9d15210, eb45619); `git diff --exit-code eb45619 HEAD` = 0 |
| Report commit, pushes | addendum |
| Alembic head | 0139 → 0140 |
| ADR | ADR-198 was the last → this stage is ADR-199; none kept for the Tend restyle |

## 2a. A-BE

**CD (FEB-19).** Before: a contact had no read and no member-given column; `display_name` was the channel's profile name; a
clear nulled `marketing_opt_out_at / source / via`, so nothing kept a past opt-out except the audit log (an administrator's
record). Migration `20261116_0140_contact_name_and_consent_events.py`: `contacts.name` (String 200, the lead's name bound) and
the append-only `contact_consent_events` (channel, action, source, via, actor_user_id SET NULL, created_at
`clock_timestamp()`, composite FK to contacts ON DELETE CASCADE, index tenant/contact/created_at DESC/id DESC).
`GET /contacts/{id}` → ContactRead (identities, profile_name, name, display_name, channels with consent, opted_out,
opted_out_at, created_at, last_inbound_at in one aggregate). `PATCH /contacts/{id}` name only. `GET
/contacts/{id}/consent-events` (cursor, 50). `GET /conversations?contact_id=` bound into the view cursor. A rename stages
one `conversation.updated` (`fields: ["participant"]`) per conversation. Tests: tests/integration/test_contact_detail.py, 29.

**MR (FEB-34).** Before (BE1's test, P-H08): 404 for a withdrawn membership when the workspace exists; a session without a
workspace for a sole withdrawn membership. After: 403 `membership_revoked`, FEB-07's sentence, token unspent; a stranger, a
deleted workspace and an unknown slug keep 404; a refresh without a slug unchanged (only active memberships are candidates,
so MR-03 needed no change). tests/integration/test_refresh_workspace_refusal.py: 9 (two rows changed, one added).

**NX (FEB-35).** Before (P-H13b): `"GET /api/v1/conversations?q=probe-term-123 HTTP/1.1" 404 … "-" "Wget"` - the file is
mounted inside the image's `http` block and every server but the stream location inherited the image's `main` format.
After: `"GET /api/v1/conversations HTTP/1.1" 404 335 0.019 0.019 <request id>` (format `wasla_safe`, named by every server).
Error log, before and after: `request: "GET /api/v1/conversations?q=probe-term-456 HTTP/1.1"` on an upstream timeout -
**not closed, cannot be**; documented in docs/DEPLOYMENT.md with the retention duty. `nginx -t` successful in the throwaway
`--rm` container (session 1). Tests in tests/integration/test_deployment_configuration.py (73 in the file).

**LG.** `next_statuses(status)` in app/db/models/lead.py reads `ALLOWED_TRANSITIONS`; `LeadRead.next_statuses` on every
LeadRead. **TF.** Before (P-H14..16): ILIKE found 6 of 11 terms (missed isik, ISIK, ıstanbul, ilgin, ILGIN). After: one
predicate `contains_folded` (app/repositories/base.py: `translate` then `lower`, both sides) behind the contacts, leads and
conversations searches; a grep test holds it written once. EXPLAIN plans were not recorded (no index served the old ILIKE
'%term%' either; stated, not measured). **EC5.** Added, each at its raise site with status and sentence unchanged:
`lead_status_transition_not_allowed` (422), `assignee_not_active_member` (404), `contact_name_invalid` (422). Already
specific: stale_lead_status, open_lead_exists, stale_assignment, membership_revoked, workspace_suspended,
cursor_view_mismatch, search_term_invalid. **Seed helpers** (tests/e2e_stack/customer_states.py, test-only):
`second-identity`, `agent-lead`, `old-opt-out`; everything else the journeys make through the API and the fake webhook.

**Contract.** openapi.json 261 → 264 operations (+ contact read, PATCH, consent events); test_openapi_export.py counts 265
routes; schema.d.ts regenerated; three codes in four catalogues.

| Gate | Result |
| --- | --- |
| GB5-1 focused | test_contact_detail 29, test_lead_graph_and_codes 2, test_e2e_customer_states 2, test_refresh_workspace_refusal 9, test_deployment_configuration 73 - 0 failed |
| GB5-2 neighbours (one file at a time) | 30 files, 0 failed: contact_endpoints 8, channel_consent 5, opt_out_ingestion 27, opt_out_recovery 7, provider_consent_signals 5, opt_out 15, lead_crm 42, lead_endpoints 19, lead_models 35, lead_validation 37, inbox_contract 22, inbox_error_codes 4, inbox_incremental 2, conversation_read_state 21, channel_pause_and_inbox 8, refresh_reuse 20, error_envelope_contract 14, route_authorization 8, documentation_truth 27, openapi_export 16, e2e_gate_states 10, e2e_inbox_states 15 + 1, campaign_contact_cap 9, audit_trail 16, tenant_isolation 10, models 16, omnichannel_schema 20, migration_downgrade_atomicity 5; schema_parity 4 passed on a migration-built database (session 1) |
| GB5-3 static | ruff clean, black 1018 files unchanged, mypy 1002 files no issues |
| GB5-4 contract | `export_openapi --check` 0; frontend drift 0; alembic head 0140; Python locks unchanged |
| GB5-5 mutation | section 18 |
| GB5-6 migration | upgrade → downgrade → upgrade on wasla_fe5_mig (logs/gb5-6_migration.log) |
| GB5-7 nginx | `nginx -t` successful; the quoted lines above |

The first session's neighbour log showed errors ("schema public already exists"): two pytest runs had shared one database.
Re-run one file at a time in this session - the table above.

## 3. Environment

Node v24.15.0, pnpm 12.18.0 (corepack 0.34.6), Docker 29.9.3, compose v5.7.0, gh 2.98.0, wrangler 4.158.0 (account id
1cc89863…, the one FE-4 used). No package added; lockfile 6c4beb1a… unchanged. Free disk on C: 8.3 GB at the start, 8.2 GB
before the e2e image build. Durations here: frozen install 48 min; e2e image build ~8 min; three-app build 8 min 22 s; the
18 FE-5 journey runs 3.9 min at 2 workers. One background-worker start timeout in vitest (two files) - re-run alone, passed.

## 4. Design Skills

| Skill | SKILL.md SHA-256 | Same as FE-4 | Outputs (skills\) | Used for | Not taken |
| --- | --- | --- | --- | --- | --- |
| ui-ux-pro-max | 43d1c5ad… | yes | ui-ux-pro-max\01..11 | inline-edit keys and focus return, errors beside the control, rows not a table at 390 | 04 (timeline) and 07 (stale data) returned no usable match - the plan and docs/CRM.md decided; the pre-delivery checklist was **not re-run** on the finished screens |
| taste-skill | dfd1190a… | yes | taste-skill\direction-fe5.md | the filter band as the secondary, one primary per view, plain stat cards | - |
| emil-design-eng | f6655603… | yes | emil-design-eng\motion-review-fe5.md | no motion on rows, badges, the edit swap, the timeline | - |
| impeccable | e0565c6c… | yes | impeccable\context-fe5.md, audit-fe5-1.md | the audit: 17/20, P0 0, P1 0, P2 3, P3 3 | its launcher and detector download an engine - not run; one audit, not two (nothing at P0 / P1 to fix) |

## 5. Decisions Applied, and Decisions NOT Taken

Applied: RS-01/02; CD-01..11 (with consent per channel); MR-01, 02, 05 (MR-03 needed no change; MR-04 - ADR-194 does not
require the session to end); NX-01..05; LG-01..03; TF-01..05 (fold widened to Ş Ğ Ç Ö Ü); EC5-01..05; SK5-01..04; CL-01..06;
LL-01..05; LD-01..10; PX-01..04 (one GET /contacts/{id} per opened conversation - the conversation carries no consent);
SE-01..06 as the code allowed (above); PR-01..04 (`PUBLIC_PREVIEW_NO_API`, new, build time only); IC-01..04; LY5-01..03;
UI5-01..05; CJ-01..11; VQ5-02 (the dialog measured at 1280 only, as FE-4 measured its dialog), VQ5-04; PV5 in the addendum.

Navigation: FE-3's registry is flat, so two entries - "Customers" (/contacts) and "Leads" (/leads).

Not taken: everything in the prompt's "Decisions NOT taken" list, unchanged; plus react-hook-form for the lead forms (plain
state and pure functions in `lib/customers/lead-form.ts`, which the unit tests and mutants exercise); a `ci(frontend)`
commit (the new spec joins every project by the existing globs - nothing to change in frontend.yml).

## 6. The Plan's FE-5 Rows

| The plan asks | Served by | Built |
| --- | --- | --- |
| APP-CUS-01 /contacts: GET contacts (q, account_id, cursor) | CL-02 | yes |
| APP-CUS-01 /contacts/:id; POST / DELETE opt-out; states E, opted out; FEB-19 | CD, CL-03 | yes |
| APP-CUS-02 /leads: filters status, source, assigned_to_id, unassigned, tag, search, cursor; statistics | LL-01..03 | yes |
| APP-CUS-03 /leads/:id: detail, notes, activity, PATCH, status, score, assignment, notes, POST leads; NF; FEB-06 | LD, LL-04, EC5 | yes |
| APP-INBOX-03's FEB-19 part | PX | yes |
| Acceptance: lead status / score / notes / assignment flows | CJ-05..08 journey | yes |
| Acceptance: Turkish search folding | TF, CJ-02 journey (en, tr) | yes |
| Section 20: user content in bdi / dir=auto; Intl; Turkish casing | UI5-04, TF-03, lint `wasla/no-uppercase` | yes |
| Section 21: cursor lists; never a token, phone or text in the URL | LY5-02, unit tests | yes |
| Section 22: customer content untrusted; links inert | composites' markup tests, NoteList | yes |

FE-4's section 32: "the panel is where the contact detail and lead editing plug in" → PX (components/customers/
panel-actions.tsx, proved by CJ-11); "the inbox composites in @wasla/ui" → ConversationRow, LinkConfirm, InertLink reused.

## 7. Contacts

`/contacts`: one infinite query on GET /contacts; the search term in component state only, debounced 300 ms, sent as typed;
`account_id` in the URL only when the workspace has more than one connection; rows name the customer in `<bdi>`, the number
in `<bdi dir="ltr">`, the open lead with its status, "Opted out". `/contacts/[id]`: the header band (name, "Opted out",
"WhatsApp name: …" as one ICU message, the inline name edit, "Use the WhatsApp name"); identities with Copy; consent per
channel with "Record opt-out" (any member, a dialog that says what stops) and "Clear opt-out" (owner / admin only, behind a
required checkbox; absent for a member); the history with "Load more"; the conversations (FE-4's ConversationRow → /inbox/id);
the leads (→ /leads/id) and "New lead" when none is open. Unknown or another workspace's id → the not-found state.

## 8. Leads

`/leads`: status, source and assignee in the URL (one parser, one builder; unknown values dropped); search and tag in memory
only. The statistics query has `enabled: admin` - for a member it is never created. "New lead": budget must be plain digits
(Arabic-Indic digits converted), "500k" is refused before any request; 409 open_lead_exists links to the open lead found
through the list. `/leads/[id]`: three field sections, each PATCHing only what changed (emptied → null); "Confirmed by a
person" beside a field in `human_verified_fields`; the status menu draws `next_statuses` and sends `expected_status`; a
stale move reloads and says where the lead stands; score 0-100; the owner as a Select for owner / admin (sending
`expected_assigned_to_id`) and as text for a member; notes (add only, links behind the confirmation); the timeline (a
person, the AI, the system; previous and new values). Nothing optimistic.

## 9. The Inbox Panel

FE-4's content unchanged. Added: the team's name for the customer (inline edit), the consent state with "Record opt-out",
"Open customer"; with a lead - the status control and "Open lead"; without - "Create lead" (linked to the contact and the
conversation, name prefilled, staying in the inbox). One more read per opened conversation: GET /contacts/{id}.

## 10. The Session's Two Rows and the Previews

Hunks under SE-01's protected paths (all listed): `packages/session/src/refresh-outcome.ts` (outcome `membership-revoked`,
the keeping set), `browser/backoff.ts` (`MembershipRevoked`, re-thrown like `WorkspaceSuspended`), `browser/index.ts` (the
export), `test/refresh-outcome.test.ts` (one row), `apps/web/lib/session/web-session.ts` (status, two catches, the outcome
case, `openAnotherWorkspace`). `session-handlers.ts` and `app/session/**`: **unchanged** (the route already keeps the cookie
for every keeping refusal). PageGate / routing.ts: the revoked state, `onlySuspendedWorkspaces`, the preview notice.
Stream helper, coordinator, invalidation map, api-client `src/client.ts` / `stream.ts`: unchanged (empty diff).

## 11. ICU Messages

| Sentence | Before | After |
| --- | --- | --- |
| "Assigned to <name>" (conversation header) | `t("assignedTo")` + `<bdi>` | one rich message `inbox.assignedTo` with `<name></name>`, four locales |
| "Seen by <name> · <time>" | already one rich message (FE-4) | unchanged |
| "Reference:" + id; "Reply:" / "Template:" + excerpt | label and value | left (not sentences) |
| every new sentence with a value (profile name, owner, stale owner, consent events, activity) | - | one ICU message each, values through rich tags into `<bdi>` |

## 12. Layout

Lists then full-page details at every width; two columns from 1024 px on the record pages; one column below. URL: /contacts
`account_id`; /leads `status`, `source`, `assignee`. Nothing else.

## 13. Composites and Look

New in @wasla/ui (composites/customers.tsx, one story file, test/customers.test.tsx 21 tests): RecordList, ContactRow,
LeadRow, FilterBar, RecordHeader, RecordPanel, InlineEditField, IdentityList, ConsentHistory, StatusMenu, ScoreField,
FieldConfirmedMark, NoteList, NoteComposer, Timeline, PreviewNotice. No colour token added; the pairs they draw
(text / text-muted / primary on secondary-subtle) were already in the contrast tests.

## 14. Proofs

| Gate | Result |
| --- | --- |
| FG5-1 | install exit 0; lockfile unchanged (pnpm audit and the licence check: CI's checks job) |
| FG5-2 | drift 0 |
| FG5-3 | at FE5_HEAD: web 506 passed, ui 121, session 91, i18n 20, api-client 51; eslint 0 and tsc 0 in each and in e2e; i18n:check ok (789 keys); format clean (logs/final_fg5-3.log) |
| FG5-4 smoke | J1-J11 PASSED (J6 "expected until FE-2", as before), image 9cf87e29543c built at BE_HEAD's backend |
| FG5-4 FE-5 journeys | tests/fe5-customers.spec.ts: 9 tests × chromium-desktop and chromium-phone = 18 passed at FE5_HEAD (logs/final_fe5_b.log). The run before it had CJ-11 (en, desktop) time out: the step read the panel's visibility at one instant; fixed to wait on a fact (eb45619), then 8 / 8 on repeat |
| FG5-4 neighbours | IJ-07, IJ-13, JR-02, JR-04, be1-suspended-reload, J6, J7 in chromium-desktop and chromium-phone: 36 passed, 4 skipped, 0 failed (logs/final_neighbours.log) |
| FG5-5 axe | inside the journeys (en): contacts with rows / no match, contact detail, record opt-out dialog, contact opted out, leads with the strip, new-lead dialog with an error, lead detail, lead with the stale notice, leads for a member, inbox panel with a lead, suspended after sign-in - 0 serious / critical |
| FG5-6 60 / 30 / 10 | table below; targets: 0 failing at 390 and 1280 |
| FG5-7 | section 18 |
| FG5-8 | section 20 |

New screens, this machine, Chromium (dominant / secondary / accent / status / text):

| Screen | en 1280 | ar 1280 | en 390 | ar 390 |
| --- | --- | --- | --- | --- |
| web-contacts | 63.9 / 30.4 / 5.7 / 0.1 / 1.0 | 63.4 / 30.5 / 6.0 / 0.1 / 0.9 | 75.3 / 17.8 / 6.7 / 0.3 / 2.5 | 74.7 / 18.0 / 7.1 / 0.2 / 1.9 |
| web-contact | 54.3 / 39.9 / 5.6 / 0.2 / 1.8 | 54.1 / 40.2 / 5.6 / 0.1 / 1.4 | 70.5 / 23.9 / 5.1 / 0.4 / 3.6 | 69.9 / 24.6 / 5.2 / 0.3 / 2.9 |
| web-leads | 59.1 / 34.6 / 6.0 / 0.2 / 1.8 | 59.4 / 34.2 / 6.2 / 0.2 / 1.6 | 70.1 / 24.2 / 5.2 / 0.4 / 3.1 | 68.9 / 25.0 / 5.8 / 0.3 / 2.9 |
| web-lead | 65.0 / 32.3 / 2.6 / 0.1 / 1.2 | 64.5 / 32.8 / 2.6 / 0.1 / 1.0 | 76.5 / 20.7 / 2.4 / 0.3 / 2.6 | 77.9 / 19.4 / 2.5 / 0.2 / 2.1 |
| web-lead-new (dialog, 1280 only) | 96.6 / 2.3 / 0.9 / 0.2 / 1.3 | 96.8 / 2.2 / 0.9 / 0.1 / 1.1 | - | - |

Averages of the new screens: 1280 (10) 67.7 / 27.9 / 4.2; 390 (8) 72.9 / 21.7 / 5.0. With FE-4's estimate for the earlier
screens (1280 ≈ 63.2 / 29.6 / 7.3, 390 ≈ 65.6 / 25.9 / 8.0, about 45 captures per width - an estimate, FE-4's report section
14): 1280 ≈ 64.0 / 29.3 / 6.7, 390 ≈ 66.7 / 25.3 / 7.5 - inside the bands (55-68 / 24-36 / 6-12). The first measurement
projected 390 at ≈ 68.8 / 23.4 - outside; fixed by c05cf3e (the header band and RecordPanel), no band widened. CI's
visual-roles project measures the whole set on Linux.

Evidence the journeys hold: the PATCH bodies (`{email}` then `{name: null}`), the status body (`{status, expected_status}`),
the stale move (responses `[409]`, nothing re-sent, the API still says qualified), the assignment body
(`{assigned_to_id, expected_assigned_to_id: null}`), "500k" with zero POSTs, a member's run with zero
`/leads/statistics` requests and exactly one GET /leads, the search terms absent from the URL, history, storages and
cookies (`kept()`).

## 15. What Was NOT Run Locally

Firefox and WebKit; the full e2e matrix; Storybook's build with its axe / keyboard / motion / layout runs; the whole
visual-roles set; the Workers build before the push; the full backend suite and its lanes; pnpm audit and the licence
check - frontend.yml / ci.yml run each. Cost accepted by the owner's rule: a failure only those would reveal is found after
the push.

## 16. The Contact Sheet

`E:\wasla-fe5-notes\screens\contact-sheet.html` with `screens\after\*.png` (18) and each capture's shares. Not committed.
The "before" captures of the panel and navigation were not taken (session 1 skipped A1's step 1).

## 17. frontend.yml

No change: `fe5-customers.spec.ts` joins every browser project and the visual-roles projects pick up the new block by the
existing globs. Durations: the addendum.

## 18. Mutation Check

Controls green before both sets. Backend (mutation/be_results.log): M-T01, 02, 04, 05, 08, 11, 12, 14, 15, 16, 18, 19, 21,
22 - **14 killed**, tree restored clean. Frontend (mutation/fe_results.log): M-T25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
36, 37, 38 (twice: the web test and the session table), 39, 40, 42, 43, 44 - **20 killed**, restored clean.

**Not run (11):** M-T03 (webhook overwrites the name), M-T06 (event outside the transaction), M-T09, M-T10 (N+1), M-T13
(the refusal spends the token), M-T17 (a copied graph), M-T20, M-T23, M-T24 - not expressed as one-line mutants in the time
this session had; the tests that would meet them exist and pass (the webhook-keeps-the-name test, the one-transaction test,
the statement-count test, the token-unspent test). M-T07: equivalent (the contact is looked up in the workspace before its
events are read). M-T41 (optimistic move): the unit test asserts the badge waits for the answer, but the control draws the
lead its parent passes, so a cache-only mutant would survive it - the rule rests on the code (no cache write before the
answer) and the journeys' read-backs. These are gaps, not passes.

## 19. Documentation and ADR Changes

DECISIONS.md ADR-199; frontend/packages/ui/DESIGN.md "Customers and leads (FE-5, ADR-199)"; frontend/README.md "Customers
and leads"; docs/API.md (Contacts rewritten, Leads `next_statuses`, Conversations `contact_id`, the codes); docs/CRM.md;
docs/DEPLOYMENT.md (nginx's log format, the error-log residual, retention); README.md's documentation-truth lines.

## 20. Part A Commits and the Diff Guard

4ecba5e, 242f760, 3d631a2, 2a7ce91, b006d65 (A-BE) · cd31a2c, 7029f18, f65395d, 65cf64c, 92ae735, f75d4ec, d620c0e,
c05cf3e, 4e592e5, eb45619 (A-FE). 15 commits, no merge, no amend; 98 files, +11,484 / −216.

9d15210..b006d65 touches only A-BE's paths (alembic: the one 0140 file; nginx: nginx.conf, log lines only). b006d65..eb45619
touches only `frontend/` and DECISIONS.md. Lockfile unchanged; event-types.json, fixtures, api-client `src/client.ts` /
`stream.ts`, stream-coordinator.ts, stream-handlers.ts, `app/session/**`, session-handlers.ts: empty diff. 0 raw colours,
0 dangerouslySetInnerHTML / innerHTML in the added lines; 0 token-shaped strings. Customer values in fixtures are the
profile's fake ones ("Nour Test", "İpek Işık Test", +20 10 555 xxxxx).

Earlier-stage tests changed, each because this stage decided the behaviour it asserted: test_refresh_workspace_refusal.py
(two rows to 403 membership_revoked, one test added); test_contact_endpoints.py (a stub's signature); test_openapi_export.py
(262 → 265); packages/session/test/refresh-outcome.test.ts and apps/web/test/fef24-suspended-session.test.tsx (the
membership_revoked row); apps/web/test/fe3-rules.test.ts and e2e/tests/fe3-shell.spec.ts JR-02 (navigation 3 → 5 entries).
No test deleted, skipped or weakened.

## 21. Gates at FE5_HEAD

Unit tests at the start head → FE5_HEAD: web 440 → 506 (+44 fe5-customers, +11 fe5-screens, +11 fe5-session); ui 100 → 121;
session 91 → 91 (one row changed); i18n 20; api-client 51. Catalogue keys 789 per locale, 13 plurals, i18n:check ok. 

## 22. Untracked Files

E:\wasla: 14 untracked paths at the start (11 *.md, docs/ui × 3), hashes in evidence/untracked-start.txt; identical before
the merge (evidence/untracked-before-merge.txt). No incoming path under docs/ui/; FE5_CUSTOMERS.md did not exist there.
docs/ui/contacts-page-mockup.html was read as design input (what was taken: DESIGN_BRIEF_FE5.md).

## 23-26. Merge, Push, GitHub Actions, Part C

In the addendum.

## 27. Findings

| Id | What | Status |
| --- | --- | --- |
| FEB-19 | contact read, name, consent history, contact's conversations | CLOSED |
| FEB-34 with FEF-27 (BE1) | membership_revoked on refresh, token kept, no oracle; the session's access-removed state | CLOSED (the reload case was never the refusal - see section 1) |
| FEB-35 | nginx access log | CLOSED (tested); error log: ACCEPTED RESIDUAL, documented |
| FEF-26 | previews without an API | addendum (CLOSED when the live preview shows the notice) |
| FEF-28 | sign-in with only suspended workspaces | CLOSED (GT-03's state) |
| FEF-33 | one ICU message per sentence | CLOSED |
| FEB-06 (FE-5) | three codes | CLOSED |
| Turkish folding | one predicate, three searches | CLOSED |
| FEB-36 | which workspace a slug-less sign-in opens when some are suspended | DEFERRED PRODUCT DECISION (today: the oldest active one, none if all are suspended) |
| FEF-39 | neutral badge on the secondary header band has no pill (audit P2) | NOT CLOSED |
| FEF-40 | "Tags" said twice on the lead page (audit P2) | NOT CLOSED |
| FEF-41 | dead `data-*` props on Select in the leads screens (audit P2) | NOT CLOSED |
| CJ-12 keyboard-only journey; focus-not-obscured sweep; ui-ux-pro-max checklist re-run; EXPLAIN plans; 11 mutants; PreviewNotice in visual-roles; the "before" captures | not done | NOT CLOSED (each stated above) |
| FEF-32, FEF-22 | carried | ACCEPTED RESIDUAL (FEF-32 re-observed: a `next start` server outlived a run and had to be killed by PID) |
| ar / fr / tr texts | drafts | FEQ-08 (the owner) |

A neighbour run under load showed J6 (en, desktop) "two refreshes" and JR-04 (en, desktop) never reaching network idle;
both passed when re-run (logs/fg5-4_neighbours.log, fg5-4_rerun.log). Neither file is changed by this stage except the
session rows above; recorded, watched in CI.

## 28. Decisions Ledger

APP-CUS-01, -02, -03, APP-INBOX-03 (FEB-19 part): CLOSED locally in Chromium; three browsers: the addendum. Acceptance -
status, score, notes, assignment, Turkish folding: CLOSED (CJ-05..08, CJ-02; chromium-desktop and chromium-phone). OQ-1..6:
defaults. Four skills: USED as section 4 says, with the gaps named there. Privacy: CLOSED for the places `kept()` reads and
the URL builders; a console and BroadcastChannel sweep was not added.

## 29. Infrastructure Created by This Stage

Worktree E:\wasla-fe5-fix; notes E:\wasla-fe5-notes (venv inside); containers wasla-fe5-pg, wasla-fe5-redis and network
wasla-fe5-net (removed by name once A-BE's gates and mutants were done); the throwaway nginx container (session 1, `--rm`);
the e2e image wasla-fe-e2e-app:local 9cf87e29543c (the earlier image 0d31c32e7cc2 lost that tag to this build and was left
in place, untagged - not this stage's to remove); FE-0's e2e profile, up for the runs. No prune of any kind was run.

## 30. Part D

Planned: rescue to E:\wasla-verification-scripts\fe5\, then by exact name the e2e profile (down -v), image 9cf87e29543c, the
worktree. The record: E:\wasla-cleanup\CLEANUP_fe5_2026-11-16.md.

## 31. Remaining External Actions

As the prompt's section 36 lists; plus: decide FEB-36; look at FEF-39 / FEF-40 with the contact sheet; set nginx's
error-log retention before the first deploy that uses the example.

## 32. What the Next Stages Can Rely On

FE-6: RecordHeader, RecordPanel, InlineEditField, StatusMenu, Timeline and the list pattern (FilterBar + RecordList + "Load
more"). FE-7: the role rule (absent, not disabled), the `membership-revoked` session state for a withdrawn member, the flat
navigation registry. FE-8: the StatCard strip. Embedded Signup: the contact page's identities are channel-neutral. Still
waiting: FEB-11 (FE-6), FEB-09 (FE-7), FEB-12 / EXT-FE-04 (FE-8).

## 33. Final Verdict

In the addendum.

---

# Addendum 1 — the pushes and their runs (2026-11-16)

This addendum records Part B's merge, the three pushes and GitHub's runs. Part C, Part D and the final verdict line follow
in addendum 2, once the run this commit starts has finished.

## 23. Merge and tree equality

| Step | Commit | Proof |
| --- | --- | --- |
| Merge of FE5_HEAD | ec0454e (parents 9d15210, eb45619) | `git diff --exit-code eb45619 HEAD` = 0; Alembic head 0140; the 14 untracked paths unchanged |
| Report | 2c62bd8 | one file, FE5_CUSTOMERS.md |
| Merge of CI fixes 4560ce9, b3b9790 | 64f413c | tree equal to b3b9790 apart from this report |
| Merge of CI fix 78552dd | e9eb921 | tree equal to 78552dd apart from this report |

## 24-25. Pushes and GitHub Actions

Every push without force, each after the previous `ci.yml` run had finished; origin = local after each.

| Push | Head | frontend.yml | ci.yml |
| --- | --- | --- | --- |
| 1 | 2c62bd8 | [43882980521](https://github.com/mohamedshhahat1/wasla/actions/runs/43882980521): Chromium, Firefox, visual roles (Linux averages inside the bands), Storybook, checks, drift green; **WebKit red** - CJ-03 (webkit-phone), axe color-contrast on "contact opted out"; 187 passed, 1 failed | [43882980672](https://github.com/mohamedshhahat1/wasla/actions/runs/43882980672): **red** - 5 failed, 8233 passed, 17 skipped, in both test jobs |
| 2 | 64f413c | [43893352671](https://github.com/mohamedshhahat1/wasla/actions/runs/43893352671): all green but **WebKit red** again - CJ-05..08 (webkit-desktop and -phone), the same rule on "lead with the stale notice"; 186 passed, 2 failed | [43893352627](https://github.com/mohamedshhahat1/wasla/actions/runs/43893352627): **green in every job** |
| 3 | e9eb921 | [43905604482](https://github.com/mohamedshhahat1/wasla/actions/runs/43905604482): WebKit, Firefox, visual roles, Storybook, checks, drift green; **Chromium red** - FE-4's IJ-03 (ar, chromium-desktop); 195 passed, 1 failed | [43905604215](https://github.com/mohamedshhahat1/wasla/actions/runs/43905604215): **green in every job** |

**The five backend failures of push 1** were tests outside this stage's local neighbour set - the cost the testing rule
accepted. Fixed in b3b9790:

- `test_workspace_switching.py` and `test_workspace_lifecycle.py` (ADR-038) hold that POST /auth/workspace answers 404 for a
  withdrawn member; the resolver is shared, so the switch had begun to answer 403. `membership_revoked` is now told **only
  on refresh** (`tell_withdrawn`), as FEB-34 asks; a sign-in and a switch answer as before. Neither test was changed.
- `contact_consent_events` was in neither list of `workspace_purge_service` - it is personal data, purged before contacts.
- `test_conversation_endpoints.py`: the list stub's expected call gained `contact_id: None`.
- `test_campaign_page_migrations.py` (0120): the seed wrote contacts through the model, which now maps 0140's column on a
  0119 database; written with explicit columns, as the conversation already was.

**The WebKit reds** (pushes 1 and 2) were the same rule in two different journeys, in WebKit only. 4560ce9 made CJ-03 wait
for its dialog to be gone; that was not enough. After the second red the work stopped for the owner's review (section 27);
the owner said to go on. 78552dd makes the scan wait for no menu, no closing dialog and no busy button, and makes a failure
name the element and the measured colours. WebKit is green on push 3. **The element that failed was never identified** -
the fix is the wait, and the diagnostic is in place if it returns.

**The Chromium red of push 3 (FEF-42).** FE-4's IJ-03, last step: after the cursor-mismatch restart, 34 of 35 rows were on
screen and the older open conversation never appeared within 60 s. `conversation-list-pane.tsx`, `lib/inbox/**` and
`fe4-inbox.spec.ts` are unchanged by this stage (empty diff from 9d15210), and IJ-03 passed in the other eight browser runs
of these three pushes. Recorded as an intermittent finding in FE-4's paging; **not proven to fail at the start head** (an
intermittent cannot be shown that way in one run), and not fixed here. No run was re-run; the run of this addendum's commit
is a fresh full run of the same code.

## Corrections to the report above

- Section 2a (MR): `membership_revoked` is answered on refresh only (b3b9790); the sentence "a refresh without a slug
  unchanged" stands, and a sign-in or a switch naming the workspace is unchanged too.
- Section 20: the branch has 18 commits with the three CI fixes (4560ce9, b3b9790, 78552dd); `app/services/
  workspace_purge_service.py`, `tests/integration/test_conversation_endpoints.py` and
  `tests/integration/test_campaign_page_migrations.py` join A-BE's changed paths.
- Section 27 gains FEF-42 (above): NOT CLOSED, FE-4's.
