# FE-8 leftovers — DK-05, the mutants, keyboard and focus over billing, 60/30/10, impeccable (ADR-203)

**Verdict: PENDING CI** (completed in addendum 1).

| | |
|---|---|
| start head | `f15d926` (= origin; FE-8's addendum; code head `3d95093`) |
| BE_HEAD | `4bfce1a` |
| FE8L_HEAD | `20ca564` (branch `fe8l-leftovers-20261121`) |
| merge | fast-forward of the canonical branch to `20ca564` (tree identical) |
| Alembic | 0142 → 0142 |
| OpenAPI | 268 operations, sha256 `ba725a3d…` → unchanged |
| ADR | ADR-203 (the owner's choices on the visual-roles gate) |

## 1. The request, read

The owner (2026-11-20 22:17 UTC): "OK, do these — I freed the space, about 19.5 GB." Read as the prompt's table: DK-05
measured; FE-8's backend mutants (M-R01 .. M-R12) and frontend mutants (M-R13 .. M-R26) run, plus this stage's M-S01 ..
M-S48; KY8 and VQ8 written and run; impeccable's audit; light gates; previews only if app code changed. OQ-1 .. OQ-6 ran on
their defaults; OQ-4 and OQ-5 were then put to the owner twice during the run (section 7): **"pages only"**, then **"hard
limits only"**.

RSL-03: M-R01 .. M-R12 → A-BE, M-R13 .. M-R26 → A-FE (FE-8's ID table said "M-R01..M-R24 backend"; its section 23 table
splits at M-R12/M-R13; none dropped).

| FE-8's words | specified by | here | outcome |
|---|---|---|---|
| "DK-05 was NOT measured — NOT CLOSED (DEFERRED, disk)" | DM-01 .. DM-07 | §2a | **CLOSED** |
| "Not run: the backend mutants (M-R01 .. M-R24)" | §23 | §4 | **CLOSED** — 36 run, 36 killed |
| "Not done locally: … the frontend mutants" | §23 | §5 | **CLOSED** — 38 run, 36 killed, 2 not applicable |
| "… keyboard-only journey and focus sweep extended over the billing screens (KY8)" | KY8-02 / 03 | §6 | **CLOSED** |
| "… the 60/30/10 measurement of the new screens (VQ8), the captures" | VQ8-01 / 02 | §7 | **CLOSED** (gated on hard limits, out of the averages — ADR-203) |
| "Their full audit (impeccable) was not run" | SK8-03 | §8 | **CLOSED** — round 2: nothing at P0 / P1 |

## 2. State

C: 19.08 GB free at the start (rule 8 GB), 16.65 after DML, E: 220 GB. Docker build cache 20.94 GB (217 entries) at the
start — the owner freed disk elsewhere; the cache is the owner's to prune. origin = `f15d926`; main `8e3a1ba` untouched;
the owner's 12 untracked paths in E:\wasla unchanged; no path under docs/ui touched. Node 24.15.0, pnpm 12.18.0, Docker
29.9.3, Python 3.12.7. The four skills unchanged from FE-7/FE-8: ui-ux-pro-max `43d1c5ad`, taste-skill `dfd1190a` (2.5.1),
emil-design-eng `f6655603`, impeccable `e0565c6c` (4.8.2).

## 2a. DK-05 — CLOSED

The start head's Dockerfile (FE-7's order) against c30d35f's, same context, BuildKit plain logs, default build arguments;
the one-line code change in `app/__init__.py` made and reverted uncommitted each time (`git status --short` empty).

| build | Dockerfile | C: free before | time | the dependency step | build cache after |
|---|---|---|---|---|---|
| old, cold | c30d35f | 19.04 GB | 257 s | ran (cold) | 22.05 GB |
| old, code-only | c30d35f | — | 65 s | `#13 [builder 5/5] RUN pip install --require-hashes …` **re-ran** (`#13 1.210 Collecting alembic==1.20.0`) | 22.65 GB (+0.60) |
| new | start head | 17.02 GB | 69 s | built | 22.28 GB |
| new, code-only | start head | 17.02 GB | 23 s | `#10 [builder 4/7] RUN pip install --require-hashes …` → **`#10 CACHED`**; only `COPY app`, `pip install --no-deps .` and the runtime venv copy re-ran | 22.66 GB (+0.38) |

The installed distributions of the two orders: 67 each, sha256 `ef150d31…` — identical. Images 624 MB each, removed by
name. DML-03: the builder's dependency COPY names only the two runtime locks; `tests/` is dockerignored. ADR-201's claim
holds; no ADR for it.

## 3. The venv — only the three locks, by hash

`pip install --require-hashes -r <requirements.lock minus uvloop> -r requirements-build.lock -r requirements-dev.lock -r
<colorama file>` then `pip install --no-deps -e .` — ci.yml's lines, with uvloop (Linux-only) left out exactly as FE-8's
rebuilt venv did (the derived file is byte-identical to FE-8's rescue copy) and colorama 0.4.6
(`sha256:4f1d9991…85fc6`, notes only, never committed) in the same hashed install (pytest's unpinned `colorama>=0.4`
otherwise refuses `--require-hashes`). Proof at the start and at BE_HEAD: 80 pinned, 0 differ (uvloop absent, colorama
apart); pydantic 2.13.5 = the lock; `pip check` clean; `export_openapi --check` 0; openapi.json sha unchanged; alembic
0142. FE-8's venv was not reused.

## 4. The backend mutants — 36 run, 36 killed

The runner (FE-7's folder held none; a new one, `mut.py`): each mutant is a list of exact replacements, only its killing
test run, the tree restored with `git checkout --` and `git status --short` + `git diff --stat` proved empty, a ledger row
written. Control run first: 20 commands green.

Round 1: 31 killed by FE-8's tests; 5 survived → tests written (test-only commits), all killed in round 2:

| mutant | what survived | the test now | commit |
|---|---|---|---|
| M-S05 | audit meta from/to swapped (only sorted "to" values were compared) | each row's from/to per person | `0424450` |
| M-R03 | the service's own owner refusal removed (the schema answered first) | the service refuses `tenant_owner` itself | `0424450` |
| M-S06 | the role change without the tenant row lock | the lock taken before the membership is read | `0424450` |
| M-R06 | senders read one query per sender | one statement on `users` for 1 sender and for 3 | `54cc9b7` |
| M-S14 | API_PUBLIC_URL removed from docker-compose.prod.yml | every service given BILLING_PROVIDER gets API_PUBLIC_URL | `4bfce1a` |

Note: M-S07 (another workspace's member found through this path) is killed indirectly — the route's lookup then raises and
answers 500 instead of the refusal; the test asserts the status. No OQ-1 defect: every new test was green on the
unmutated code. The full ledger (file, patch hash, failing line, restored) is `mutation/ledger.csv` in the rescue.

## 5. The frontend mutants — 38 run, 36 killed, 2 not applicable

Round 1: 18 killed by FE-8's tests (M-R13/14/15/17/21/25/26, M-S25/26/27/28/34/36/37/40/42/44/48). 18 survived because
the rule lived in a component no unit test rendered, or a test asked a blind question — killed after:

- `0576b3f` CK: no billing module touches web storage (M-R16); `e2dd37a` TM8: Change role never on one's own row, even
  when the roster shows another role (M-R24 — near-equivalent with a consistent roster).
- `fe8-leftovers-screens.test.tsx` (jsdom, a recorded API, fake timers for the schedule): `9526cc7` RT (M-R18 unreached
  read shown as failed, M-R19 no replaceState, M-R20 reads while hidden, M-S29 no focus once settled, M-S30 DECLINED not
  named), `ace3b37` BP/CK (M-R23 admin asks for the summary, M-S35 keep-the-plan without a change, M-S32 a second checkout
  while busy, M-S31 one key for every press, M-S33 a busy control `disabled`, M-S46 a plan name as markup), `40867d6` IN
  (M-R22 refund review on a refunded payment), `bee4fcc` BL/CY/TM8 (M-S38 admin gets the plans link, M-S47 admin gets
  Buy, M-S39 a row action named by other words — checked in all four locales), `2010355` AC8 (M-S41 no profile re-read
  after linking Google).

Not applicable: M-S43 (Print is drawn only inside the loaded invoice — no state has it without one), M-S45 (no FE-8 screen
adds or rounds amounts). The return page's rule (success only when `PaymentRead.status` is `succeeded`) is guarded by
M-R17, M-R18, M-R25, M-S25 .. M-S28 — all killed (MF-06).

## 6. The keyboard journey and the focus sweep — `3feac3a`, `20ca564`

KY8-02 (keyboard-only.spec.ts, no pointer API): Billing from the navigation (by address on a phone, where it sits behind the
menu) → Upgrade with Enter → the fake Paymob page (outside the app: it answers and redirects by itself) → the return page,
focus on its heading → Go to Billing → the cancel dialog and "cancel now"'s typed field dismissed with Escape, focus back
on the opener → Change role confirmed on /team (FE-8 built it as a confirm dialog offering the other role — no arrow
choice) → an invoice → Print (window.print stubbed in an init script; called once) → the refund review dismissed with
Escape. KY8-03 (focus-not-obscured.spec.ts) with an axe scan on each state:

| state | stops 1280 / 390 | fully covered | axe serious/critical |
|---|---|---|---|
| return succeeded | 15 / 6 | 0 | 0 |
| /billing | 22 / 13 | 0 | 0 |
| cancel dialog | 4 / 4 | 0 | 0 |
| invoices | 20 / 11 | 0 | 0 |
| an invoice | 21 / 12 | 0 | 0 |
| refund dialog | 4 / 4 | 0 | 0 |
| payment methods | 19 / 10 | 0 | 0 |
| usage | 19 / 10 | 0 | 0 |
| capacity | 19 / 10 | 0 | 0 |
| /team | 20 / 11 | 0 | 0 |
| change-role dialog | 4 / 4 | 0 | 0 |
| invitations | 20 / 11 | 0 | 0 |

(en and ar alike; counts before FEF-46 — the after-run is FGL-4 in section 10.) No keyboard or focus defect was found.
Not reached: the open custom offer (needs a platform offer the e2e seed cannot make), a scheduled change, the reduction
dialog, a saved card (FEB-46).

## 7. The captures and 60 / 30 / 10 — `b6052f4`

52 captures (en / ar × 1280 / 390): plan (owner, administrator), return succeeded and declined, past due, invoices, an
invoice, payment methods, usage, capacity, the cancel / refund / change-role dialogs. Before / after images and the
contact sheet are in the rescue (`screens/`, `contact-sheet.html`), never committed.

- **Before:** /billing/invoices, /payment-methods and /usage at 1280 measured dominant 37.9–44.4 % — outside FE-RD's hard
  limit (≥ 45 %) — secondary 50–56 %: the tabs stacked inside the head band (FEF-46).
- **After:** every capture inside its hard limits, 0 failing targets (e.g. plan 1280 66.8 / 29.0 / 3.8; invoices 61.2 /
  33.0 / 5.7; usage 59.5 / 34.5 / 5.8).
- **The averages.** Joined in full, they left the bands (390: 69.4 / 23.3; 1280 accent 5.8). The owner chose "pages only"
  (dialogs apart, phone tabs back in the band): 390 came inside (67.5 / 25.1 / 6.8) but 1280 accent was 5.9 — and Linux CI
  had already measured exactly 6.0 for the earlier screens on FE-8's run (44799730106: 390 67.2 / 25 / 7.2, 1280 63.8 / 30
  / 6). The owner then chose **"hard limits only"**: every billing capture is gated on its hard limits and targets on
  every push, none is in the averages (a new capture kind `apart` keeps the screen's own limits; dialogs and status states
  are gate states). Local `--check` after: 390 67.6 / 24.8 / 7.1, 1280 63.8 / 30.0 / 6.0, exit 0. ADR-203.
- Print view: not measured (a document, not a screen).

## 8. The four skills

impeccable's launcher was not run — it downloads a binary (IM-06); "context loading did not run" and DESIGN.md, the code
and the captures were read directly. Round 1 (on the before set): **P0 0, P1 1, P2 1, P3 4** — IM1-01 (P1) the tabs
stacked in the head band, screens outside the hard limits → FEF-46; IM1-02 (P2) limits as raw integers, storage in bytes →
FEF-47; IM1-03 (P3) the invoice-lines table outside a card with tighter padding than its neighbours — recorded; IM1-04
(P3) the "Active" badge's empty-circle icon reads as "off" — recorded; IM1-05 (P3) the API's English plan descriptions on
Arabic screens — server content, not taken; IM1-06 (P3) no billing section in DESIGN.md → added (ADR-203). Round 2 (after
the fixes): one P2 regression of FEF-46 — plan cards squeezed at 1280 → FEF-48; then **P0 0, P1 0**, the P3s above.
ui-ux-pro-max's pre-delivery checklist (native-scoped; its web rows applied): vector icons, targets, focus, busy as
`aria-disabled`, token-only colour — pass. taste-skill: no gradients, shadows or extra badges; one primary per view —
pass. emil-design-eng: no animation, transition or duration class in FE-8's billing code or composites; motion only from
FE-RD's Dialog — nothing to change. Skill guidance not taken: impeccable's launcher and detector (download).

## 9. The fixes

| commit | finding | screen | change |
|---|---|---|---|
| `d2999b4` | FEF-46 (IM1-01, VQL) | every BillingFrame page | tabs beside the page from md, as SettingsFrame |
| `1329765` | FEF-47 (IM1-02) | plans, usage | grouped digits, storage in GB; "Storage" label ×4 (ar/fr/tr drafts) |
| `ee60885` | FEF-48 (round 2) | plans | one / two / three cards at <xl / xl / 2xl |
| `3057513` | FEF-46 follow-up (owner) | every BillingFrame page | on a phone the tabs stay inside the head band |

None changes what a screen sends, when, or what it decides from (VQL-07: session, stream, payment-return.ts and
checkout.ts untouched).

## 10. The diff guard and the gates at FE8L_HEAD

f15d926..4bfce1a: two test files. 4bfce1a..20ca564: FE-8's billing components and lib, four i18n catalogues (one key),
three unit-test files, three e2e specs and `e2e/support/fe8.ts`, DESIGN.md, DECISIONS.md. Byte-identical: openapi.json
and schema.d.ts, the three locks, requirements-dev.in, pyproject.toml, pnpm-lock.yaml, every Dockerfile and compose file,
.github, alembic, scripts/ci_shards.py and its durations, nginx, app/.

| gate | command | result |
|---|---|---|
| GBL-1 | check_installed_lock / freeze vs locks / pydantic / export --check | 0 differ (uvloop, colorama apart) / 2.13.5 / 0 — twice |
| GBL-2 | DML | DK-05 CLOSED |
| GBL-3 | `ruff check .` / `black --check .` / `mypy app tests` | 0 / 1182 unchanged / 1022 files clean |
| GBL-4 | mutants | 36/36 killed |
| GBL-5/6 | 29 neighbour files by file | 408 passed, 0 failed |
| GBL-7 | alembic heads | 0142 |
| FGL-1 | `pnpm install --frozen-lockfile` | exit 0, lockfile unchanged |
| FGL-3 | turbo typecheck lint test i18n:check (web, ui, i18n); format:check | 13/13; web 688 (was 667), ui 151, i18n 20; 1531 × 4 |
| FGL-4 | smoke J1–J11; the named specs | smoke green (J6 expected); named specs (keyboard-only, focus-not-obscured, fe8-billing, fe7-team-settings, fe3-shell; Chromium desktop + phone, one worker): 84 passed, 9 skipped (the other width), 3 red — KY8-02 desktop en/ar (ArrowDown sent while the cancel dialog was still opening; the journey now waits on facts, `20ca564`; 8/8 since) and JR-08 ar desktop ("worker process exited unexpectedly (code=3221226505)" before the test began; green alone, both locales) |
| FGL-5 | visual-roles (whole spec, both projects) + `--check` | 82 passed; exit 0 |
| FGL-6 | axe in KY8-03 | 0 serious / critical on 48 states |
| FGL-7 | frontend mutants | 36 killed, 2 not applicable |
| FGL-8 | impeccable round 2 | P0 0, P1 0 |

## 11. Merge, push, CI

origin had not moved (`f15d926`); `git merge --ff-only fe8l-leftovers-20261121` in E:\wasla; the merged tree equals FE8L_HEAD's; the owner's 12 untracked paths unchanged, none incoming. The push and the runs: addendum 1.

## 12. Previews

PENDING (app code changed: `apps/web` billing components and lib, `@wasla/i18n` messages).

## 13. Findings

Closed: DK-05; FE-8's unrun backend and frontend mutants, KY8, VQ8, impeccable's audit. New and fixed: FEF-46, FEF-47,
FEF-48. New and open:

- **FEB-46** — `billing_states.py saved-card` cannot run in the e2e stack: docker-compose.e2e.yml sets no
  PAYMENT_TOKEN_FINGERPRINT_KEY ("DependencyUnavailableError: No payment token fingerprint key is configured"); its
  integration test supplies its own key. The fix needs the compose file (outside this stage).
- **FEB-47** — the fake Paymob page numbers transactions `190_000_000 + int(time.time()) % 1_000_000`
  (tests/e2e_stack/fakes.py:198, FE-0's b9e54de): two pages in the same second share an id, the API rightly drops the second
  callback as a duplicate, and that payment stays pending (seen locally: two workspaces' checkouts at 23:51:02.437, one
  never settled). Outside FE-8's lines (OQ-1) — not fixed; a likely source of intermittent reds in parallel checkout
  journeys (fe8-billing and this stage's specs) on CI. Fix: one counter per run started at a random place (FEB-40's rule).
- **FEF-49** — the P3s of section 8 (IM1-03 invoice lines' card, IM1-04 the "Active" badge icon).

## 14. Numbers

| | before | after |
|---|---|---|
| OpenAPI operations / sha | 268 / ba725a3d… | same |
| alembic head | 0142 | 0142 |
| ADRs | … ADR-202 | + ADR-203 |
| backend tests in the changed files | team 16 + config 83 | +4 / +1 |
| web / ui / i18n unit tests | 667 / 151 / 20 | 688 / 151 / 20 |
| i18n keys | 1531 × 4 | 1531 × 4 |
| mutants | 0 run | 74 run: 49 killed by FE-8's tests, 23 after a new test, 2 n/a, 0 defects |
| keyboard spec tests (per project) | 3 | 5 |
| focus sweep tests (per project) | 2 run + 2 skipped + control | + 2 run + 2 skipped |
| visual-roles captures | 73 / 77 in the averages | same in the averages + 52 billing gated apart |
| impeccable | — | round 1: 0 / 1 / 1 / 4 → round 2: 0 / 0 / 0 / 2 (P0/P1/P2/P3) |
| DK-05 dependency step on a code change | old re-runs (65 s, +0.60 GB) | new CACHED (23 s, +0.38 GB) |
| C: free | 19.08 GB | END_DISK |
| build cache | 20.94 GB | END_CACHE |

## 15. ADR

ADR-203 — the billing tabs' place per width, the billing captures gated on their hard limits and out of the averages, the
amounts people read.

## 16. Left for the owner

See the final message and the cleanup record.
