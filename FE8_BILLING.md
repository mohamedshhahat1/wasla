# FE-8 — Subscription and Payment (ADR-202)

**Verdict: FE-8 MERGED, PUSH PENDING — CI result recorded in the addendum.** (This line is replaced by the addendum's
verdict once GitHub's ci.yml and frontend.yml have run on the pushed head.)

| | |
|---|---|
| start head | `b3caa75` (= origin; FE-7's addendum) |
| backend head (A_BE_HEAD) | `9c4e756` |
| FE8_HEAD | `0ebf66d` (branch `fe8-billing-20261120`) |
| merge | `bb5cab7` (one normal merge commit; tree identical to FE8_HEAD) |
| Alembic | 0141 → **0142** |
| OpenAPI | 267 → **268** operations |

## 1. The owner's request, read

The owner asked for FE-8 together with FEB-42 ("build it"), FEB-43, FEB-44, FEB-45 and DK-05, under the light gates
(2026-11-20). OQ-1 .. OQ-9 ran on their defaults; no other answer came before the run. The owner answered the disk stop
with **"continue anyway"** (2026-11-20 16:00 UTC), which this stage read as overriding the free-space rule for the builds
it could not avoid — not for DK-05's four optional builds.

## 2. State and DK-05

- C: had **5.11 GB** at the start (rule: 8 GB before DK-05, 6 GB before any build) and stayed between 4.87 and 5.11 GB.
- **DK-05 was NOT measured — NOT CLOSED (DEFERRED, disk).** Four ~1.2 GB builds were not safe at ~5 GB. The e2e image
  (1.17 GB) and the apps' builds were made under the owner's "continue anyway".
- The four design skills: unchanged from FE-7 (43d1c5ad, dfd1190a, f6655603, e0565c6c). Their full audit (impeccable) was
  not run in this stage.
- Docker's build cache: 205 entries, 20.4 GB (20.0 GB reclaimable); local volumes 13.6 GB (12.0 GB reclaimable). Never
  pruned here.

## 3. Backend half (A-BE)

| item | what changed | tests | commit |
|---|---|---|---|
| FEB-42 + migration 0142 | `PATCH /workspace/members/{user_id}` {role}: owner changes anyone's but an owner's and their own; an administrator promotes a member but never demotes a peer; ownership never assigned; revoked refused; same role → unchanged, not audited; under the tenant lock; audited `member_role_changed {from,to}`; codes `member_role_self/owner/admin_owner_required`, `member_not_active` | `test_fe8_team_account_api.py`, `test_fe8_migration.py` | `40e1061` |
| FEB-44 | invitation reads carry `created_at` and `invited_by` (null for a closed account; senders in one statement) | same file | `40e1061` |
| FEB-45 | `/auth/me` (and PATCH's answer) carry `has_password`, `google_connected` | same file | `40e1061` |
| FEB-12 | `API_PUBLIC_URL`; the callback is built on it, the return stays on `APP_PUBLIC_URL`; required with Paymob in staging/production; compose, `.env.example`, DEPLOYMENT (settings, "Before the first deploy" incl. the Paymob dashboard URLs) | `test_config.py` | `131a0dd` |
| FEB-06 (FE-8) | `plan_requires_checkout`, `checkout_already_started`, `payment_provider_unavailable`, `offer_not_open`, `refund_already_requested` at their raise sites | `test_fe8_billing_codes.py` | `0ab8070` |
| FEB-43 | `requirements-dev.in` / `.lock` (uv, constrained by the two locks; the 19 versions are the ones the unhashed install resolved); every install is the three locks by hash then `pip install --no-deps -e .`; drift guard over three locks | `test_dev_lock.py`, `test_delivery_pipeline.py` | `ad982fe` |
| fake + seed | the fake Paymob page's `outcome=` paid / declined / pending / late; `billing_states.py` saved-card / refunded | `test_e2e_paymob_outcomes.py`, `test_e2e_billing_states.py` | `9c4e756` |
| FEB-13 | decided, not built (OQ-3): no PDF, tax fields or numbering; the invoice page prints as "a record of Wasla charges" | — | — |

A-BE gates: focused tests green; **78 neighbour files: 1460 passed** (the first run was void — it shared its database with
three focused runs; four genuine stub failures then fixed and re-run green); ruff, black --check, mypy app tests clean;
`export_openapi --check` 0; migration upgrade / downgrade / upgrade, `alembic check`, `db_preflight verify` OK; both
compose files render; ci.yml's quality-job install steps replayed in `python:3.12-slim` exit 0 (the Windows replay found
that `--no-build-isolation` needs `editables`, which no lock carries — the line was changed before the commit); the e2e
image's log shows the three locks installed with `--require-hashes` and `pip check` clean; FE-0's smoke J1–J11 green.
**Not run: the backend mutants (M-R01 .. M-R24).**

## 4. Frontend half (A-FE)

- `/billing` (plan, state in words, renewal, payment owed + Pay now, open offer, plans with monthly/yearly prices and the
  API's own action per price, cancel at period end / now behind the typed address, resume, keep the plan),
  `/billing/checkout/return`, `/billing/invoices`, `/billing/invoices/[id]` (print, refund review), `/billing/payment-methods`,
  `/billing/usage`, `/billing/capacity`. Roles: the code's (OQ-5).
- Every hosted payment through one function (fresh key per press, https only, nothing stored).
- The return page decides only from `GET /billing/payments/{id}`; never reads the query string (dropped unread); polls 2 s
  then 10 s, paused when hidden; network errors decide nothing; finds the payment from the API (OQ-4).
- Links: payment banner "Pay now", ended banner "Choose a plan", checklist plan step, "See plans" after a seats refusal,
  Billing in the navigation (owner, administrator). TEAM8: "Change role"; invitations' sender and sent time. ACCOUNT8: one
  password mode, one Google action, exactly the closing proofs.
- `@wasla/ui`: PaymentStatusPanel, UsageMeter, BarSeries, InvoiceLines, with stories and tests.
- Unit tests: web **667** (634), ui **151** (145), api-client 51; typecheck, lint, format, i18n:check (1531 × 4) green.

### Found by the journeys, fixed in their own commits

| finding | cause | commit |
|---|---|---|
| apps' build refused | BarSeries uses state; the module lacked `"use client"` | `2d98abe` |
| return page found no payment | a checkout invoice has no `issued_at` (ADR-096); the page now uses the attempt's `created_at` | `2bde602` |
| a decline stayed "Confirming" | BILL-07 keeps a hosted decline **pending** with its reason; the page shows it as "did not go through" and keeps reading | `2bde602` |
| axe label-content-name-mismatch on the member row | "Make owner" / "Change role" names now start with their visible words | `2bde602` |
| TJ-04 regression | after linking Google the page kept the old profile; it now reads /auth/me again | `0ebf66d` |

## 5. Journeys (Chromium, local)

`fe8-billing.spec.ts`: J5, declined (redirect still says success=true), pending (three answered reads, still confirming),
late callback, administrator, member, FEB-42/44/45 — **7/7 desktop**; billing + FE-7 team/settings + FE-3 shell on phone
and desktop green after the fixes (JR-02 counts ten links now — the listed BL-03 line). JR-04 (phone) timed out once on
"network idle" and passed alone — FE-7's known runner intermittent, unchanged.

**Not done locally:** the keyboard-only journey and focus sweep extended over the billing screens (KY8), the 60/30/10
measurement of the new screens (VQ8), the captures, impeccable's audit, the frontend mutants. GitHub's frontend.yml runs
the new spec in every browser.

## 6. Decisions and deviations

DK-05 deferred (disk). FEB-13 decided not built. CK-03's e2e allowance is http on `localhost` (any port). CY-01 links to
FE-3's Channels page for the reduction choice. Invoice lines wrap rather than scroll. Invitations show sender and time on
each row (rows, not a table). RT-05's failure sentence: AUTHENTICATION/3DS/DECLINED → "the card's bank did not confirm",
else generic. No TASKS.md or docs/FRONTEND.md exist; FEB items are tracked here and in ADR-202.

## 7. Findings

Closed: FEB-12, FEB-42, FEB-43, FEB-44, FEB-45, FEB-06 (FE-8's part). Deferred: FEB-13 (FEQ-17), DK-05 (disk), FEB-11
(the owner: later). New backend/frontend findings: none.

## 8. Left for the owner

1. Docker's cache: `docker builder prune -f --filter until=168h` (20.0 GB reclaimable) — never run by this stage.
2. Before the first real payment: `API_PUBLIC_URL` in production, and the Paymob dashboard's callback →
   `${API_PUBLIC_URL}/api/v1/webhooks/paymob`, redirect → `${APP_PUBLIC_URL}/billing/checkout/return`.
3. FEQ-17 / FEB-13 (Egypt's e-invoicing, an invoice number).
4. FEQ-08: the ar / fr / tr drafts of the new copy.
5. DK-05's measurement once C: has 8 GB free.
6. security.yml RGM-23 / 24 / 27 (unchanged).
