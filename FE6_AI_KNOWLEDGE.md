# FE-6 — AI Agent and Knowledge Base (ADR-200)

Report of FRONTEND_PLAN.md section 29's FE-6 (APP-AI-01, APP-KB-01, APP-KB-02) with a small backend half (FEB-37, FEB-38,
FEB-39, FEB-06's codes, FEB-40), FEB-11 decided from the code, and the owner's FE-5 leftovers (FEF-39, FEF-40, FEF-41, the
keyboard-only journey and the focus-not-obscured sweep, FE-5's eleven unrun mutants, nginx's error-log retention).
Written 2026-11-17. The CI runs, Part C and Part D are in the addendum.

## Phase status

| Phase | State | Evidence (E:\wasla-fe6-notes\) | Commit / action |
| --- | --- | --- | --- |
| A0 state, skills, infra | done | §2 | branch fe6-ai-knowledge-20261117 from 424d38d; worktree E:\wasla-fe6-fix |
| B1 inventory and "before" | done (code-only; see §2a) | FE6_AI_KNOWLEDGE.draft.md §2a | — |
| B2–B4 OPTIONS, LIMITS, CURSOR, codes, FEB-40 | done | logs/gb6-2.log | 984b70d |
| B5 NGINX | done | NL-T1, NL-T2 | f346f11 |
| B6 FE-5's carried mutants | done | mutation/fe5_carried.log | no commit (no test needed: M-T07 equivalent) |
| B7 seed helpers, contract | done | — | 64b00f8 (BE_HEAD) |
| A1 install | done (1 h 9 min, lockfile unchanged) | logs/install.log | — |
| A2–A5 composites, screens, home, leftovers | done | logs/fg6-3-final.log | 79afd22, 91e2174, 9d0e4f6 |
| A6 journeys, keyboard, focus, measurement, audits | done | logs/fg6-4-*.log, screens/, skills/ | ad6bf31 + fixes (§23) |
| A7 docs, ADR | done | — | 07e8c60, docs(frontend) |
| A8 mutation, diff guard, gates → FE6_HEAD | done | mutation/, §23, §24 | FE6_HEAD (§2) |
| Part B merge | done | git diff --exit-code 3430b31 558e21b → 0 | 558e21b |
| Part B push, CI | addendum | | |
| Part C previews | addendum | | |
| Part D cleanup | addendum | | |

## 1. Executive Summary

What an owner, an administrator and a member can now do: open **AI agent** from the navigation (every role) and see the
workspace's agents with their status in words and "Answers customers" on the one that answers; owners and administrators
create a draft, edit its name, instructions (plain text, a counter to the deployment's bound), model (only the
deployment's permitted models — FEB-37), creativity, reply length (to the deployment's ceiling), memory and hand-over
level ("Off" is a real value, sent as `null`), turn its tools on and off and choose which knowledge `search_knowledge`
reads; **Activate** and **Make default** (only for an active agent) put it in front of customers; turning off the
answering agent asks first. A member reads exactly the same set-up and sees no control. **Knowledge** (owners and
administrators — OQ-3) lists the knowledge bases ("Load more" from the `X-Next-Cursor` header the browser can now read —
FEB-39), creates, renames and deletes them (a base with documents only after typing its name, cascade), tunes the six
answer settings with "Use the default", tests a question (counts as a search) and shows the unanswered questions or a
plain "switched off". A base's page lists its documents with a status filter in the URL and a title search in memory,
uploads a file after checking its type and size against the server's own limits (FEB-38 — nothing is sent that would be
refused), adds text, answers duplicates ("Replace it"), and opens a document panel (status, why it failed, extraction
notes, replace, retry, delete, similar). Statuses move on screen by polling that backs off and **stops at ready or failed**.
**Home** tells every role, until an ACTIVE default exists, that no AI agent is answering customers; the checklist counts an
active default and a ready document.

How the request was read: FE-6 exactly as the plan's section 29, with FE-5's leftovers the owner named; FEB-11 **decided
from the code, not built** (polling; deferred, scale-triggered); "run their tests as before": only the focused gates
locally — GitHub's frontend.yml and ci.yml are the one full check. **OQ-1 .. OQ-6: the defaults** (no answer arrived
before the run).

Locally covered: A-BE's focused and neighbour tests (996 passed in 59 files), static checks, contract; the frontend
packages' typecheck/lint/unit tests (web 549, ui 134, i18n 20, api-client 51) and i18n:check; FE-0's smoke J1–J11; the
named journeys in Chromium desktop and phone (§17); axe inside them; the 60/30/10 measurement of the new screens; the
mutants. **Only GitHub covers**: Firefox and WebKit, Storybook (axe, keyboard, motion, layout over the new stories), the
full e2e matrix, the full backend suite and its lanes, the Workers build before the push.

Findings the owner should know about first: **FEB-40** (a knowledge base created or renamed to a used name answered 500 —
fixed here, one service check); **M-T07 is an equivalent mutant** (the composite key makes the consent history's tenant
filter redundant — recorded, not "killed"); the Banner primitive gained a basis so its action wraps under the words on a
phone (FE-3's banners benefit too); the Dialog primitive now returns focus to its opener when opened without a trigger
(found by the keyboard journey).

## 2. Source Branches and Exact Heads

| Name | Commit |
| --- | --- |
| FE5_CI_GREEN_HEAD = ORIGIN_CANONICAL_START_HEAD | 424d38d (origin = local at start, 0/0) |
| BE_HEAD | 64b00f8 |
| FE6_HEAD | 3430b31 |
| CANONICAL_MERGED_HEAD | 558e21b (parents 424d38d, 3430b31; git diff --exit-code 3430b31 HEAD → 0; untracked list and hashes unchanged) |
| main | 8e3a1ba (untouched) |
| Alembic | 0140 before and after (no migration) |
| OpenAPI | 264 → 266 operations (267 routes served incl. /metrics) |

## 2a. A-BE

| Item | Before (from the code) | After | Tests |
| --- | --- | --- | --- |
| AO (FEB-37) | permitted models only learnt from a 422; `_permitted_models` an unordered set | `GET /agents/options`: models (default first, null = unrestricted), default_model, max_output_tokens {default, ceiling}, bounds (from the schema constants; `MIN_MESSAGE_LIMIT` lifted, value 1 unchanged), default_knowledge_grant; one resolver `permitted_models()` for read and refusal | AO-T1..T8 in test_agent_options_and_knowledge_limits.py |
| KL (FEB-38) | per-source limit inline in `upload_as_submission`; extension table private | `GET /knowledge/limits` from `max_upload_bytes()` / `upload_extensions()` / `knowledge_limits` / `max_document_request_bytes`; pdf/xlsx/docx 300,000 B, text/markdown/csv/html 1,600,000 B, body 5 MiB, 40 pages, 400,000 chars, title 300 (`MAX_DOCUMENT_TITLE_LENGTH`, source_key keeps its own 300) | KL-T1..T7 |
| KC (FEB-39) | `expose_headers = [X-Request-ID]` | `+ X-Next-Cursor` (imported from knowledge.py), origins unchanged | KC-T1, KC-T3 (Starlette lists exposed headers even for a refused origin; with no allow-origin the browser reads nothing — asserted) |
| FD (FEB-11) | EVENT_TYPES has no knowledge type | not built; polling; DEFERRED (SCALE-TRIGGERED): > ~50 pending in one tab or > 1 poll/s/workspace | VQ6-04 empty diff |
| EC6 | class codes only | `agent_name_exists` 409, `agent_not_active` 422, `agent_model_not_permitted` 422, `agent_output_tokens_exceed_ceiling` 422, `knowledge_scope_invalid` 422, `knowledge_base_name_exists` 409, `knowledge_gap_report_disabled` 404 — same status and sentence | test_fe6_error_codes.py (11), test_knowledge_gaps.py |
| FEB-40 | base create/rename to a used name → IntegrityError → 500 | service check → 409 knowledge_base_name_exists | test_fe6_error_codes.py |
| NL | nginx: no logging block (Docker json-file, unbounded) | json-file 10m × 5 (~50 MB); DEPLOYMENT.md: what the error log still holds, journald (MaxRetentionSec=14day) / logrotate alternatives, readers, "Before the first deploy" list | NL-T1, NL-T2 |
| Seed | — | tests/e2e_stack/knowledge_states.py: processing, failed, warnings, needs-reindex, reindexing | test_e2e_knowledge_states.py (8); used in AJ-05 |

GB6: focused 31 + 11 + 8 + NL 2; neighbours 59 files 996 passed / 5 skipped (13 m 47 s); ruff, black --check, mypy app
tests (1004 files) clean; export --check 0; Alembic 0140; locks unchanged. The probes were read from the code and the
focused tests rather than a separate scratch app (recorded as a deviation from P-K01..P-K16's form; each "before" above
names its source).

## 3. Environment

Node 24.15.0, pnpm 12.18.0, Docker Desktop, Python 3.12.7 venv (locks + dev pins, non-editable). No package added; lockfile
6c4beb1a… unchanged. C: 6.9 GB free at the start and before the e2e image build (≥ 6 GB). Install 1 h 9 min; e2e image
5e648c0a2c52 1.17 GB; apps build 7 min; web rebuilds ~3 min each.

## 4. Design Skills

| Skill | SKILL.md SHA-256 | Output | Decided |
| --- | --- | --- | --- |
| ui-ux-pro-max | 43d1c5ad… (= FE-5) | skills/ui-ux-pro-max/ (6 ux searches, nextjs stack, REVIEW_FE6.md) | bottom scroll-padding for the sticky Save bar; confirmation and success patterns; one live region per list |
| taste-skill 2.5.1 | dfd1190a… (= FE-5) | skills/taste-skill/REVIEW_FE6.md | one primary per view (status actions secondary; empty list's single primary); the marketing-site run not taken |
| emil-design-eng | f6655603… (= FE-5) | skills/emil-design-eng/REVIEW_FE6.md | no motion on a polled status badge (no-op class removed); no looping "Indexing" |
| impeccable 4.8.2 | e0565c6c… (= FE-5) | skills/impeccable/audit-fe6.md | P1 pane width (list collapsed at 1280), P2 sticky bar only when dirty; round 2 clean |

Guidance not taken: impeccable's launcher/detector (downloads an engine — no network); taste-skill's deals, photographs,
moments and gate (marketing sites); any palette/font/library proposal.

## 5. Decisions Applied (summary) and Not Taken

Applied as section 10 states, with these recorded choices: OQ-1..6 defaults; KD-02 uses `GET /knowledge/documents` (the
CursorPage route); PO polls the list, the panel reads its own document only when it is not in the loaded rows; the
document panel is a pane from 1024 px and a drawer below it (a phone uses the drawer — recorded); DocumentRead has no
`updated_at`, so rows show when a document was added; `KnowledgeBaseRead` has no count, so the typed confirmation says
"at least N" from loaded rows; the search preview shows the response's one `match` (passages carry none); "positive" is
not offered as an escalation level; the description cannot be removed; AG-04's unsaved guard: `beforeunload` plus a capture
listener on same-origin links; the agents list shows at most 100 (no cursor in the API). Not taken: as section 10's list,
plus a `ci(frontend)` commit (the new specs join every project by the existing globs; frontend.yml unchanged).

## 6. The Plan's FE-6 Rows

| Plan demand | Served by | Built |
| --- | --- | --- |
| APP-AI-01 /ai/agents, /ai/agents/:id, owner/admin write, member read | AG, TL | yes |
| GET/POST agents, GET/PATCH agents/{id}, POST …/default, available-tools, GET/PUT tools, DELETE tools/{name} | AG, TL | yes |
| "no default agent" state; plan_limit_exceeded (agents) | HM6-01, AG-02 (API sentence, no billing link) | yes |
| APP-KB-01 /knowledge owner/admin: bases CRUD, settings, gaps, search | KN | yes |
| APP-KB-02 /knowledge/:baseId: documents (cursor), text, upload, replace, ingest, delete, similar, poll | KD, PO | yes |
| Acceptance: J4 green; upload pre-checks (type, 5 MiB); polling stops at ready/failed; warning until one exists | AJ-04, KD-03 (KL's per-type limits; 5 MiB = body cap), PO, HM6-01 | yes |
| FEB-11 optional | FD | decided, not built |

Plan vs code: "5 MiB" is the body cap (per-type limits are lower); APP-KB owner/admin vs API member-readable → plan wins
(OQ-3); the bases' cursor was unreadable cross-origin → FEB-39.

## 7–12. Screens (as built)

See §1 and DESIGN.md "The AI agent and knowledge (FE-6, ADR-200)". Polling: 3 s, ×1.5 to 15 s, reset on change, paused
hidden (TanStack `refetchIntervalInBackground: false`), stopped when every loaded row is ready (nothing in flight) or
failed; AJ-04 proves no knowledge request for 20 s after ready. Home: two banners; checklist: agent step = active default,
knowledge step = `GET /knowledge/documents?status=ready&limit=1`; both steps link to the new screens. FE-3 test lines
changed (each now asserts the new fact): HM-03's `all` inputs (`status: "active"`, `readyDocuments`), the
"each step turns on its own fact" lines (+ disabled default, + no ready document), the loading lines, SH-03's registry list
(+ "/ai/agents", "/knowledge"; Knowledge absent for a member), FE-5's navigation test (first five entries), FE-3's shell
journey link count 5 → 7.

## 13. FE-5's Leftovers

| Item | Fix | Proof |
| --- | --- | --- |
| FEF-39 | neutral Badge: inset `ring-border-strong` (3.08:1 on secondary-subtle) | tokens.test pair; Badge story "BadgeOnSecondaryBand"; M-U39 killed |
| FEF-40 | TagEditor `labelHidden` inside the titled section | fe6-screens test; M-U40 killed |
| FEF-41 | `data-owner-select`, `data-filter="assignee"` removed (no reader) | grep 0 |
| Eleven mutants | M-T03, 06, 09, 10, 13, 17, 20, 23, 24 killed by FE-5's named tests; M-T07 survived = equivalent (contact required in the workspace + composite FK fk_contact_consent_events_tenant_contact); M-T41 killed by the new real-cache test | mutation/fe5_carried.log, fe_results.log |
| Keyboard / focus | KY-02 and KY-03 written | §14 |
| nginx | NL | §2a |

## 14. Keyboard and Focus

KY-02 (keyboard-only.spec.ts, no pointer API — self-checked): home warning link → agents → New agent (Escape returns
focus) → create (Enter in the name field) → Activate → Make default → a tool's switch (Space) → knowledge → new base → add
text → open a row (Enter), Escape closes the pane → inbox → a conversation → contacts → a contact → its Back link. Green
in en and ar, desktop and phone (Chromium). Found and fixed: Dialog focus return without a trigger; Escape did not close
the desktop pane. KY-03 (focus-not-obscured.spec.ts): inbox, a conversation, contacts, a contact, leads, agents, an agent
(dirty, sticky bar), knowledge, a base with the panel — 1280 in the desktop project, 390 in the phone project, en/ar:
0 fully covered stops (partial covers recorded in the test annotations); control M-U42 (a planted cover) caught.

## 15–16. Layout, Composites, Look

New in @wasla/ui: AgentRow, AnswersMark, ToolSwitchRow, KnowledgeScopePicker, DocumentRow, DocumentStatusBadge, FileField,
TextCounter, PassageList, GapList, SettingSwitchRow; stories "AI and knowledge" (8 stories); unit tests 13. Primitive
changes: Badge neutral ring (FEF-39), Dialog focus return, Banner text basis; `RowLink`/`rowClass` exported. styles.css:
`scroll-padding-block-end`.

## 17. Proofs (local)

| Gate | Result |
| --- | --- |
| FG6-1 install / lockfile | frozen install exit 0; lockfile unchanged |
| FG6-2 drift | schema.d.ts regenerated from openapi.json (645cdef6…) |
| FG6-3 packages | typecheck, lint, test, i18n:check — 17/17 tasks; web 549, ui 134, i18n 20, api-client 51; format:check clean |
| Smoke J1–J11 | all PASSED (J6 expected); J4 6.8 s |
| FG6-4 named (Chromium desktop + phone) | runs: fe6 (8 → 20 → 6 passed after fixes), neighbours fe5/fe3/fe2 83 passed 1 skipped; the final run of the whole set at FE6_HEAD (3430b31, fresh profile up, 2 workers): 116 tests, 111 passed, 5 skipped, 0 failed, 17.8 min (logs/fg6-4-final.log); profile down -v, no next start server left |
| FG6-5 axe | inside the journeys: 0 serious/critical on every state scanned (two violations found and fixed) |
| FG6-6 60/30/10 | every new screen inside its hard limits; combined with FE-5's Linux averages: 390 66.9/25.2/7.4, 1280 63.3/30.1/6.4 (bands 55–68 / 24–36 / 6–12); home with the warning (status state) 5.2 % status at 390 |
| FG6-7 mutation | 20 FE-6 frontend mutants + M-T41 killed; restored |
| FG6-8 backend path check | only A-BE's list (§23) |

## 18. What Was NOT Run Locally

Firefox, WebKit, stream-restart, api-down projects; Storybook build and its axe/keyboard/motion/layout runs; the full
visual-roles set; the Workers build; the full backend suite and lanes — frontend.yml / ci.yml (addendum).

## 21. Mutation Check

Backend M-U01..M-U16 are covered by the A-BE tests that assert each rule (AO-T2..T7, KL-T1..T3, KC-T1/T3, NL-T1, the code
tests, the import guard); they were not run as one-line mutants (recorded). Frontend M-U21..M-U40 (20) all killed;
M-U41 = M-T41 killed; M-U42 checked by its planted-cover control. FE-5's eleven: §13.

## 23. Commits and the Diff Guard

f346f11 nginx · 984b70d api contract/codes/FEB-40 · 64b00f8 seed helpers (BE_HEAD) · 79afd22 ui · 91e2174 FEF-40/41 ·
9d0e4f6 screens · ad6bf31 journeys · 07e8c60 docs/ADR-200 · a8b1cac one primary/no-op motion · d4b908e prettier ·
e160eaf dialog focus/row name/member bases · (ESM) · (row name/keyboard) · (pane Escape) · (banner basis/capture) ·
(list heads on the secondary band) · (pane width/sticky bar) · (README).
Guard: A-BE paths only; after BE_HEAD only frontend/ and DECISIONS.md; VQ6-04 protected paths empty; compose diff = nginx
logging only; lockfile unchanged; no data-* on Select; no raw HTML injection; no package.

## 25. Untracked Files

14 paths in E:\wasla, hashes in untracked-start.sha256 — unchanged (checked again at the merge).

## 30. Findings

| ID | Status |
| --- | --- |
| FEB-37, FEB-38, FEB-39 | CLOSED |
| FEB-11 | DEFERRED (SCALE-TRIGGERED) |
| FEB-40 (base name 500) | CLOSED (found and fixed here) |
| FEF-39, FEF-40, FEF-41 | CLOSED |
| FEB-35's residual (error-log request line) | ACCEPTED RESIDUAL; retention CLOSED in the example |
| FEF-43 (the Dialog did not return focus when opened from state) | CLOSED (primitive) |
| FEF-44 (Banner squeezed its words beside the action on a phone) | CLOSED (primitive) |
| FEB-41 (the other services of docker-compose.prod.yml log without a bound) | NOT CLOSED — audit #20 |
| FEF-32, FEF-22 | carried |
| ar / fr / tr texts | drafts (FEQ-08) |

## 34. Remaining External Actions

As the prompt's section 36: walk the screens (frontend/README.md), confirm OQ-1..6, review ar/fr/tr, pick the next stage
(FE-8, FE-7 with FEB-36's "last-used workspace", or WhatsApp Embedded Signup), the "Before the first deploy" list
(including FEB-41), security.yml's RGM-23/24/27, deleting earlier stages' folders and branches, merging into main.

## 35. What the Next Stages Can Rely On

The read-only view pattern of the agent page; the role rule (absent, not disabled); Dialog focus return; the typed
confirmation (ConfirmDialog) for cascades; home's Banner pattern for plan warnings (FE-8); `GET /agents/options` and
`GET /knowledge/limits`; FEB-11's stage would replace only PO's interval.

## 36. Final Verdict

FE-6 MERGED AND PUSHED, CI GREEN — see Addendum 1.

## Addendum 1 — the four pushes, CI green, the previews

**Verdict: FE-6 MERGED AND PUSHED, CI GREEN.** Beside it: **PREVIEWS UPDATED** (all three Workers).
NEXT: THE OWNER PICKS — FE-8 (after FEB-12 / FEB-13), FE-7 (after FEB-09, with FEB-36's "last-used workspace"), or the
WhatsApp Embedded Signup backend stage.

### Pushes and runs

| Push | Head | frontend.yml | ci.yml | What was red, and why |
| --- | --- | --- | --- | --- |
| 1 | ebb14c3 (report; merge 558e21b) | 44088496468 failure | 44088496532 failure (tests 8291 passed, 17 skipped, 2 deselected) | ruff E501 in a test A-BE added (one line, 101 > 100; the local GB6-3 run missed it); KY-02 in Firefox / WebKit: focus lost where a pressed control was removed (a dialog closing into a new page, Activate replaced) — Chromium wraps a lost focus, the others leave the page; AJ-04 chose a file before the field was enabled (limits not yet read); visual roles: FE-3's home captures now carried FE-6's warning (status 3.1–7.4 % > 3); CJ-10 WebKit-only (see FEF-43) |
| 2 | 79ff4a8 (fix 22945f0) | 44102582623 failure (Firefox only) | 44102582785 success | KY-02 in Firefox: Shift+Tab after the status actions still lost the keyboard — a busy Button is `disabled`, which drops focus in Firefox, and the restore waited for both reads |
| 3 | 4281e95 (fix 4a8f968) | 44112760858 failure (Firefox only) | 44112760783 cancelled by push 4 (owner: push without waiting) | KY-02 in Firefox: Shift+Tab trapped in the scope picker's RadioGroup (Radix's container leaves the Tab order through a state update that lands after Firefox chose the container; it hands focus back to the checked radio) — FEF-44 |
| 4 | 5267367 (fix e7f26a1) | **44122041585 success, every job** | **44122041770 success, every job** | — |

Push 4's counts: Chromium 224 passed / 22 skipped; Firefox 216 / 30; WebKit (with the API restart, JR-11, IJ-08) 219 / 30;
visual roles 70 passed; Storybook (axe, keyboard, motion, layout), drift, typecheck / lint / unit / i18n / build / audit
green. ci.yml: 8291 passed, 17 skipped, 2 deselected = CI_TEST_COUNT 8238 + 53 tests A-BE added; Lint, format, types;
Migration-built schema; Docker build; Alert rules — all green. Each push was made without force and merged as one normal
merge commit whose tree equals its fix commit's (`git diff --exit-code`). Push 2 and 3 waited for the running ci.yml; push
4 was made at once on the owner's instruction ("don't wait to ci push and watch the fix"), cancelling push 3's ci.yml.
This addendum is docs-only: it starts no frontend.yml — run 44122041585 is the one that decides the verdict.

### The fixes (no test skipped, no band widened, no bound raised, no keyboard step replaced by a click)

| Commit | Change | Proof |
| --- | --- | --- |
| 22945f0 | `lib/focus.ts` (focusIfLost; unit test fe6-focus.test.ts): the agent page and the base page give a lost focus to their heading on arrival; the upload specs wait for the enabled file field; visual-roles gives FE-3's measured homes an answering agent (the warning stays measured apart as `web-home-no-agent`, a status state ≤ 8 %); the E501 line | Chromium locally: KY-02, AJ-04, AJ-05 (AJ-05 phone re-run alone after a 0xC0000409 worker crash before its test; both runs recorded), visual-roles homes 16 passed |
| 4a8f968 | the status actions stay focusable while busy (`aria-disabled`, `aria-busy`, a second press ignored); focus moves to the next state's first action in the commit that draws it; KY-02 waits on each focus (heading on arrival, next action after each status change) | Chromium locally: KY-02 6 passed, AJ-02 / AJ-03 6 passed |
| e7f26a1 | `@wasla/ui` RadioGroup: on Shift+Tab the container leaves the Tab order on the element itself, before the browser moves; Radix keeps forward Tab, arrows, Home / End; unit test radio-group.test.tsx pins the timing (fails with the change removed — checked) | **Firefox locally, one exception to section 8 made to diagnose a CI-only red after the owner said "don't stop until you fix it"**: KY-02 (en) failed on the unfixed build exactly as on GitHub, then passed; KY-02 Firefox + Chromium desktop / phone en / ar 12 passed; AJ-02 / AJ-06 Chromium 8 passed; packages/ui 137 unit tests |

### Findings added

| ID | Finding | Status |
| --- | --- | --- |
| FEF-43 | CJ-10 (FE-5) failed once in WebKit on push 1: a withdrawn member's reload of /contacts was redirected to /verify and stayed there (session and routing code, unchanged by FE-6 — VQ6-04 empty); green on pushes 2, 3, 4 | NOT CLOSED — intermittent, for the session's owner stage |
| FEF-44 | Shift+Tab trapped inside every `@wasla/ui` RadioGroup in Firefox (agent's hand-over level, knowledge scope, number grounding) | CLOSED (e7f26a1) |
| — | the agent's status buttons dropped focus in Firefox while busy (`disabled`) | CLOSED (4a8f968) |
| — | lost focus after a dialog closed into a new page (agent, knowledge base) | CLOSED (22945f0) |

### Part C — the previews

C: had 4.8 GB free before the build (below 6 GB); the owner answered "continue anyway" (2026-11-17 17:08 UTC); 2.8 GB
after. `wrangler whoami`: "Mohamedshhahat1@gmail.com's Account", 1cc89863… (= FE-5's). The live versions before were
FE-5's. Source diff a2febaf → 5267367: web 52 paths, console 22, marketing 20 (through `@wasla/ui` / `@wasla/i18n`) → all
three rebuilt with `build-workers-linux.mjs --env preview`, the three workers.dev origins, `PUBLIC_PREVIEW_NO_API=true`
(web 3,946 files, console 3,687, marketing 29 / 573,483 B). `workers:smoke`: every page 200 on workerd; session routes GET
405, POST 503. Every wrangler.jsonc unchanged since a2febaf (workers.dev only, no route, no custom domain, ASSETS only). No
secret set; nothing on iwasla.tech.

| Worker | Previous version (FE-5) | New version | Upload (gzip) |
| --- | --- | --- | --- |
| wasla-preview-web | df3720f3-f913-402e-af0f-81fbe161313e | e9d20bca-e35b-45ba-b1f2-5f8d50c64ea3 | 8899.41 KiB (1782.31 KiB; +4.6 %) |
| wasla-preview-console | 84855297-dce0-46b8-93b7-01a47b9eba70 | d0520a0d-23b6-40fb-a136-53a5ed80a1c2 | 6536.97 KiB (1382.06 KiB) |
| wasla-preview-marketing | 369e2448-637c-4b9f-928c-c0c1e0b9a98d | 2d4d5535-7399-442a-a826-bf31250f4075 | 0.35 KiB (0.24 KiB) |

Live checks: web `/`, `/login`, `/ai/agents`, `/knowledge`, `/contacts`, console `/`, marketing `/ar/` → 200 (marketing
`/` 302 to `/en/`); HSTS, CSP with nonce and `strict-dynamic`, nosniff, referrer and permissions policies on `/login`;
`/session/refresh` GET 405, cross-origin POST 403, header-less POST 403, same-origin POST 503 `session_unavailable` (login
the same). In Chrome, `/`, `/ai/agents` and `/knowledge` without a session show the PreviewNotice ("This is a preview of
Wasla"); no console error. The AI and knowledge screens themselves are seen through the contact sheet and the local
walk-through (the previews have no API).

## Addendum 2 — push 5: a flaky campaign test (FEB-40)

The addendum's ci.yml (44136703951, on 5cadc22, docs only) failed in "Migration-built schema": one test,
`test_the_last_place_in_the_window_goes_to_exactly_one_of_two_racing_campaigns`
(`tests/integration/test_campaign_messaging_limit.py`), with `UniqueViolationError` on `uq_contacts_tenant_id_wa_id`.
Nothing FE-6 changed: the test's helper `add_contacts` drew a fresh random 6-digit block per call and numbered from 00000;
the racing test adds one person per round, 40 rounds, to the same workspace, so two rounds can draw the same block —
about 0.16 % per run, about 0.3 % per push (it runs in two jobs). The failing number 2142108300000 is block 421083 drawn
twice. Push 4 drew differently and passed.

| ID | Finding | Status |
| --- | --- | --- |
| FEB-40 | `add_contacts` (campaigns stage's test helper) could repeat a contact number within one workspace | CLOSED (62cc0b8): the blocks come from one sequence per run, started at a random place; a new test makes the random source repeat on purpose — it failed with CI's exact `UniqueViolationError` before the change and passes after; the file's 16 tests pass locally; ruff, black, mypy clean. Other helpers with the same random pattern (`test_campaign_cost.py`, `test_capacity_reductions.py`, `test_media_*_recovery.py`, `test_conversation_read_state.py`) mostly add one contact per new workspace, where a repeat cannot collide — listed for a later look, not changed |

Push 5: fix 62cc0b8, merge baa0664 (tree equal to the fix), pushed without force (the owner: do not wait for the running
run; push 5 cancelled the rest of 44136703951). ci.yml **44146445974 success in every job**: Tests and migrations 8292
passed, 17 skipped, 2 deselected (8291 + the new test); Migration-built schema, Lint / format / types, Docker build, Alert
rules green. frontend.yml did not run (a backend test only, outside its paths); 44122041585 on 5267367 remains the
frontend verdict run, and nothing under `frontend/` changed since.

**Verdict unchanged: FE-6 MERGED AND PUSHED, CI GREEN. PREVIEWS UPDATED. CLEANUP COMPLETE WITH ITEMS LEFT FOR REVIEW**
(`E:\wasla-fe6-fix`, `E:\wasla-fe6-notes` — the safety check refused their recursive delete; for the owner).
