# Wasla — Final Authentication Findings Remediation

Closing the eleven findings from `FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md`. That
audit found no production-blocking authentication vulnerability, and this work
did not go looking for another one. What it closes is the narrower and more
durable problem the audit actually named: the places where a future security
regression could pass the test suite in silence, where a real attack would be
detected and never surfaced to a person, and where the documentation or the
database had drifted away from the contract the code keeps.

Nothing in the authentication runtime was redesigned. The one behavioural change
is AUTH-09's database index, and the advisory lock that keeps it from turning a
race into a 500.

---

## 1. Repository state

| | |
|---|---|
| Branch | `worktree-billing-google-auth` (unchanged) |
| HEAD at audit | `ee540f04cd4ddd0cacbe35ad24f7bf71eeaa9261` |
| HEAD at start of this session | `7042535cc018538953e565e7d2f14371e75937f2` |
| HEAD at completion | `24cb4bae3d4bd7baae980bc6562129423b388b51` |
| Working tree at start | clean but for untracked `FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md` |
| Working tree at completion | clean; two untracked reports, no modified files |
| Migration head at audit | `0050` |
| Migration head now | `0052` |

**HEAD had advanced past the audited revision before this session began.** Six
remediation commits already existed between `ee540f0` and `7042535`, covering
AUTH-01, AUTH-02, AUTH-03, AUTH-04, AUTH-05, AUTH-06 and AUTH-09. Nothing was
reset to the audited revision. Each of those findings was revalidated against
the code as it now stands rather than taken on the commit message's word, and
the three that remained open — AUTH-07, AUTH-08, AUTH-11 — were reproduced
against current code before being fixed.

`FINAL_AUTH_ACCOUNT_SECURITY_AUDIT.md` is left untracked, exactly as this
session found it. It is the input to this work and not a product of it.

---

## 2. Findings revalidation

| Finding | Still applicable? | Fix | Regression proof | Status |
|---|---|---|---|---|
| AUTH-01 Redis single-use atomicity untestable | Yes | none needed — implementation was already correct | `test_redis_single_use_atomicity.py`, real Redis + `asyncio.Barrier`, 8 contenders | **Closed** |
| AUTH-02 reset/verification exactly-one-winner under-tested | Yes | none needed — guards were already correct | `test_auth_consume_races.py`, real PostgreSQL, barrier planted between read and write | **Closed** |
| AUTH-03 invitation unit tests read the developer's `.env` | Yes | explicit `Settings(_env_file=None, …)` injected per test | `test_ambient_email_configuration_cannot_change_the_outcome` | **Closed** |
| AUTH-04 password login absent from the audit trail | Yes | `LOGIN_SUCCEEDED` / `LOGIN_FAILED`, migration `0051` | `test_auth_audit_durability.py` | **Closed** |
| AUTH-05 invitation accept/revoke actions never emitted | Yes | wired past the successful transitions in `InvitationService` | `test_auth_audit_durability.py` | **Closed** |
| AUTH-06 auth security metrics have no alert rules | Yes | four rules in `deploy/monitoring/alerts.yml` | `promtool test rules` | **Closed** |
| AUTH-07 stale Google workspace-onboarding documentation | Yes | corrected in `google_auth_service.py` and `docs/AUTH.md` | pinned in `test_documentation_truth.py` | **Closed** |
| AUTH-08 verification route documents 400, returns 422 | Yes | docstring corrected | `test_the_route_documents_the_status_it_actually_returns` | **Closed** |
| AUTH-09 one-live-reset-token has no DB constraint | Yes | migration `0052`, partial unique index + advisory lock | `test_password_reset_single_live_token.py` | **Closed** |
| AUTH-10 logout access-token lifetime | Not a defect | **no change, by design** | documented in `docs/AUTH.md:38` | **Intentional** |
| AUTH-11 foreign/missing workspace indistinguishability untested | Yes | none needed — behaviour was already correct | `test_a_foreign_workspace_and_a_nonexistent_one_answer_identically` | **Closed** |

---

## 3. AUTH-01 — atomic Redis, made executable

Both stores spend a credential in one Redis operation: `RefreshTokenStore.spend`
is a single `SET NX`, `OAuthFlowStore.spend` is `GET` and `DEL` inside
`MULTI`/`EXEC`. Both were already correct. Neither had a test that could
observe the race they exist for.

The reason is worth stating because it is a pattern rather than an accident. The
unit suites drive Redis fakes whose methods are `async def` with no `await`
inside. A coroutine that never suspends runs to completion the moment it is
started, so `asyncio.gather` finishes each caller before beginning the next and
no two are ever inside the operation at once.

`tests/integration/test_redis_single_use_atomicity.py` uses real Redis and an
`asyncio.Barrier`. Eight contenders are released into `spend` in the same tick,
so each issues its first command before any of them reads a reply.

| Test | Result | Under check-then-write |
|---|---|---|
| 8 contenders spend one refresh token | exactly 1 `True`, 7 `False` | 8 winners |
| a refresh token spent under contention stays spent | spent | — |
| 8 contenders spend one OAuth state | exactly 1 flow returned, 7 `None` | 8 winners |
| an OAuth state spent under contention is gone | gone | — |

The right-hand column is the point. These tests are deterministic in both
directions: against the shipped implementation Redis serialises the operation
and one caller wins, and against a check-then-write implementation every
contender reads "not spent" before any of them writes and every one wins. See
§12 for the mutation runs, including one that had to be corrected before it was
a real mutation at all.

---

## 4. AUTH-02 — reset and verification races

Both flows end with a conditional `UPDATE` whose *result* is the security
decision. Sequential replay is caught earlier by `is_usable`, so the concurrent
branch is the only one the guard exists for — which is why ignoring the consume
result left the suite green.

`tests/integration/test_auth_consume_races.py` drives real applications over
connections that commit independently, synchronised by a barrier planted inside
the code under test, between the read and the write the guard adjudicates.

**Password reset, 4 simultaneous redemptions, each proposing a different new
password.** Exactly one succeeds; the losers are refused with the uniform
answer; exactly one of the four candidate passwords signs in and the other three
do not; the token is consumed once; `token_version` increments once; the old
access and refresh tokens are both dead; no 500. Checking *which* password works
is what makes "exactly one winner" meaningful — two winners writing the same
string would be indistinguishable from one.

**Email verification, 5 simultaneous submissions of one correct code.** Exactly
one 200, four uniform rejections, one `consumed_at` transition, one
`email_verified` audit entry, no duplicate transition, no 500.

---

## 5. AUTH-03 — test isolation

`InvitationService` defaults its settings argument to `get_settings()`, which
reads `.env` from the working directory. With `EMAIL_ENABLED=true` in a local
`.env`, four of the five tests in `tests/unit/test_invitation_service.py`
failed; on a machine without one, the same four passed.

Every test now builds its own `Settings(_env_file=None, …)` and passes them in.
`_env_file=None` is the load-bearing argument — without it every other value is
a suggestion.

The fix is explicit injection rather than a suite-wide pin, deliberately.
Pinning `EMAIL_ENABLED=false` across the suite would have made the email path
unreachable everywhere, and the security-relevant claim about invitations is
precisely that the token's only destination is the invited mailbox. So the
default here is email off, and
`test_the_token_is_queued_only_to_the_invited_address` turns it on and checks
where the token goes.

`test_ambient_email_configuration_cannot_change_the_outcome` is the regression
for the finding itself: it sets `EMAIL_ENABLED` in the environment to each of
four values and asserts the module answers identically regardless.

---

## 6. AUTH-04 and AUTH-05 — the durable trail

### New audit actions

| Action | Written when | Recorded |
|---|---|---|
| `LOGIN_SUCCEEDED` | a password login opens a session | account, no tenant |
| `LOGIN_FAILED` | a password login is refused **and names a real account** | account, `meta["reason"]` |
| `INVITATION_ACCEPTED` | past the token claim and past the membership write | invitation id, invited address, role, user id |
| `INVITATION_REVOKED` | past the conditional `UPDATE` that actually changed the row | invitation id, invited address, role, revoking actor |

Migration `0051` adds the two login actions to the enum.

### The enumeration decision

**`LOGIN_FAILED` is written only when the refusal names an account.** An address
nobody has registered leaves a metric and a log line and no row. Two reasons:
the endpoint is unauthenticated, so a row anybody can cause is a way to flood a
trail colleagues have to read; and there is no account to attribute it to in any
case. What that costs is visibility of spraying against addresses that do not
exist — which is what `PasswordLoginFailureSpike` covers instead.

Because the row is written only for addresses that *do* have accounts, it is
never returned to the caller and never changes the response, so it creates no
external oracle. The login endpoint's answer for a known and an unknown address
remains identical in status, error code, message and timing — still asserted by
`test_account_enumeration.py`.

### What is never recorded

No plaintext password, no password hash, no submitted password, no access or
refresh token, no Authorization header, no Google token, no verification code,
no reset token, and no raw invitation token. `INVITATION_ACCEPTED` carries the
invitation id, not the credential that claimed it.

`INVITATION_REVOKED` is written past the conditional `UPDATE`, so an invitation
that does not exist, belongs to another workspace, or was already accepted or
revoked leaves the method by an exception above it — the trail cannot report a
revocation that did not happen.

Google login auditing is unchanged, and `GOOGLE_LOGIN_SUCCEEDED` stays a
separate action rather than folding into `LOGIN_SUCCEEDED` with a `method` in
`meta`: "which issuer let someone into my account, and when" is the question
asked after a Google account turns out to have been compromised, and it is worth
filtering for on its own.

---

## 7. AUTH-06 — alerting on the authentication security counter

`wasla_auth_security_events_total{event,outcome,reason}` was defined, emitted,
scraped and routed to a working Alertmanager, and no rule watched it. Four rules
now do.

| Alert | Selector | Threshold / window | Severity | Receiver |
|---|---|---|---|---|
| `RefreshTokenReplayDetected` | `{event="refresh", outcome="blocked", reason="replay"}` | `rate(…[5m]) > 0`, `for: 2m` | critical | `wasla-critical` |
| `PasswordLoginFailureSpike` | `{event="login", outcome="failure"}` | `rate(…[10m]) > 0.2`, `for: 15m` | warning | `wasla-warning` |
| `AuthenticationRateLimitSpike` | `{event="rate_limit", outcome="blocked"}`, `sum by (reason)` | `rate(…[10m]) > 0.2`, `for: 15m` | warning | `wasla-warning` |
| `EmailWebhookSignatureFailures` | `{event="email_webhook", outcome="blocked", reason="invalid_signature"}` | `rate(…[15m]) > 0.01`, `for: 15m` | warning | `wasla-warning` |

**Replay has no threshold, because one is the number that matters.** A refresh
token presented after it was spent is either a stolen token in use alongside the
real one or a client with broken rotation. The application has already torn the
token family down by the time this fires; what the alert adds is that a person
finds out. `for: 2m` is about scrape settling, not about doubting the event.

**Login failure is a rate, never a count.** `> 0.2` sustained for fifteen
minutes is roughly 180 refusals, which no honest traffic produces. It is summed
rather than split by `reason` on purpose: an attacker working a list produces a
mixture of `invalid_credentials` and `account_deleted`, and watching each
separately would let a spray sit under both thresholds.

**Rate limiting is split by `reason`** — three bounded values (`client`,
`account`, `account_reset`) that mean genuinely different things, and an
operator's first question is which. Alertmanager groups by `alertname` and
`component`, so the worst case is one notification carrying three lines rather
than three notifications.

### Alert storm and privacy controls

- `group_by: ["alertname", "component"]` — never a per-customer label, so a
  hundred workspaces hitting one defect is one notification.
- `repeat_interval: 4h` warning / `1h` critical. A reminder, not a reason to
  mute the channel.
- `ScrapeTargetDown` inhibits `component=~"…|security"`, so when the scraper
  cannot reach the application the one actionable alert is not buried under the
  alerts it caused.
- **No user id, email address or IP appears in any alert label.** The metric's
  own label set is `(event, outcome, reason)`, all bounded, and the registry's
  label guard refuses a UUID.
- The Slack webhook is read from `api_url_file`, a mounted secret. No webhook
  value appears in the repository or in this report.

### Test results

```
promtool check config /cfg/prometheus.yml     SUCCESS — 1 rule file, 12 rules
promtool test rules /cfg/tests/alerts_test.yml  SUCCESS
amtool check-config /cfg/alertmanager.yml     SUCCESS — 1 inhibit rule, 2 receivers
```

Each of the four rules has below-threshold, above-threshold and recovery cases
in `deploy/monitoring/tests/alerts_test.yml`, asserting alertname, severity,
component, summary and description.

---

## 8. AUTH-07 and AUTH-08 — documentation

### AUTH-07, Google onboarding

`POST /api/v1/workspaces` ships. Any verified account may call it and becomes
the owner of what it creates, which is the route that unblocks a Google-first
account. Two places still said such an account waits to be invited somewhere —
which is advice that strands the person who follows it:

- `AuthService._enrol`'s docstring: *"cannot open any workspace-scoped endpoint
  until it is invited somewhere"*
- `docs/AUTH.md`: *"has no workspace until it is invited to one"*

Both now describe the real flow: account created → `active_workspace: null` →
`POST /workspaces` → owner → `POST /auth/workspace` for a token carrying the new
`tid`. `docs/GOOGLE_OAUTH.md` was already correct and was left alone. No ADR was
touched — those are historical decision records.

The retired sentence is pinned in `test_documentation_truth.py`.

### AUTH-08, verification status

The `/auth/email/verification/verify` docstring said every rejection *"leaves as
the same 400"*. The refusal is a `ValidationError`, which
`register_exception_handlers` maps to **422** everywhere. A client branching on
the documented status would have read a wrong code as a transport failure.

The implementation is the source of truth and was not changed. The docstring now
says 422.

`test_the_route_documents_the_status_it_actually_returns` takes the status from
a live refusal rather than writing it down, so it fails if the route changes
*or* if the prose does, and additionally refuses any other client-error status
in the description. It caught its own first draft: the corrected docstring
explained the drift by naming 400, and a docstring is rendered into OpenAPI
where naming it is itself a claim. That history moved into a comment.

`MediaTypeError` carried the identical sentence about `ValidationError` being a
400. Same one-line correction.

---

## 9. AUTH-09 — the database keeps the rule

`email_verification_challenges` has enforced "at most one live challenge per
account" with a partial unique index since it was created.
`password_reset_tokens` rested entirely on `supersede_outstanding` being called
before `create`. Two sibling tables enforcing one rule at two strengths, and the
weaker one guarded a password.

| | |
|---|---|
| Migration | `0052_one_live_password_reset_token` (new; `0050` and `0051` untouched) |
| Index | `uq_password_reset_tokens_active` |
| Predicate | `UNIQUE (user_id) WHERE consumed_at IS NULL AND superseded_at IS NULL` |

**"Live" is the two null columns, not expiry.** An index predicate must be
immutable, so `expires_at > now()` cannot appear in one — and need not: an
expired token nothing superseded is still that account's outstanding token,
`supersede_outstanding` ends it with the rest, and `is_usable` refuses to spend
it. This matches the sibling index exactly, which is the point.

**The migration repairs before it constrains.** A deployment could already hold
two live tokens for an account and `CREATE UNIQUE INDEX` would fail on it with
no explanation, so the extras are superseded, newest kept — which is what the
application would have done had the supersede not been missed. **Nothing is
deleted**: a superseded row records that a reset link was issued and
invalidated, and that is worth keeping. On a database that never drifted it
updates nothing.

**The index alone would have made things worse, and that is the substantive half
of the change.** Two simultaneous reset requests both supersede nothing and both
insert, so the constraint would refuse one and turn an endpoint whose entire
contract is a single constant answer into a 500 — a fresh account-existence
oracle introduced while closing a database finding. `request` now takes
`pg_advisory_xact_lock` on the account across supersede-then-create, in its own
namespace, the shape `WorkspaceService.create` already uses. Four simultaneous
requests answer 202 four times and leave one live token.

### Migration verification (isolated database `wasla_remed52`)

```
fresh DB → alembic upgrade head      0001 … 0052   OK
alembic current                      0052 (head)
alembic downgrade 0051               OK  (drops the index; tokens left as they are)
alembic upgrade head                 OK  (re-applies)
alembic check                        No new upgrade operations detected.
```

Index as PostgreSQL reports it:

```sql
CREATE UNIQUE INDEX uq_password_reset_tokens_active
  ON public.password_reset_tokens USING btree (user_id)
  WHERE ((consumed_at IS NULL) AND (superseded_at IS NULL));
```

Direct probes against that database:

| Attempt | Result |
|---|---|
| first live token | inserted |
| second live token for the same account | **refused** by `uq_password_reset_tokens_active` |
| new token after the old one is consumed | inserted — consumed does not block |
| new token after the old one is superseded | inserted — superseded does not block |

No rows were deleted to make the migration pass.

**Downgrade policy.** Reversible, unlike the enum migrations either side of it,
because an index constrains future writes rather than being a vocabulary rows
are written in. Nothing is un-superseded on the way down: the repair ended
tokens the application had already stopped treating as live, and resurrecting
them would hand somebody back a second working reset link.

---

## 10. AUTH-10 — logout

**Unchanged by design.** This is not a defect and was not treated as one.

- `POST /auth/logout` spends the supplied refresh token. The already-issued
  access token remains usable until its expiry, at most 15 minutes.
- `POST /auth/logout-all`, password reset, disable, delete and refresh-replay
  teardown increment `token_version`, so their access tokens stop on the next
  request.
- Clients must clear both tokens after logout.

No access-token Redis denylist, per-access-token row, or server-side access
session table was added. `docs/AUTH.md:38` states the contract explicitly,
including the word *deliberately*, so no consumer is told that ordinary logout
immediately invalidates an already-stolen access JWT.

---

## 11. AUTH-11 — tenant enumeration indifference

`AuthService._resolve_workspace` refuses "no such workspace" and "you are not in
it" from one branch on purpose. Nothing held that down.

`test_a_workspace_without_a_membership_is_not_switchable` asserted the foreign
case is a 404 and stopped there — which passes unchanged against an
implementation that answers 404 for a foreign slug and something else for an
invented one. That is the same leak read backwards.

The new test asks both and compares the two answers *against each other* rather
than against a constant, on all three public routes that take a slug from the
client:

| Route | Foreign workspace | Non-existent workspace |
|---|---|---|
| `POST /api/v1/auth/workspace` | 404 `not_found` | identical |
| `POST /api/v1/auth/login` | 404 `not_found` | identical |
| `POST /api/v1/auth/refresh` | 404 `not_found` | identical |

Status code and full error envelope are compared, less `request_id`, which is a
per-request correlation handle and the one field that is supposed to differ. The
foreign workspace is owned by another real account rather than left
membership-less, because "you are not in it" is the branch under test.

Two non-vacuity controls: the refusal must be a 404 (two 200s would compare
equal and prove nothing), and a separate test switches into a workspace the
caller *does* belong to, without which an endpoint broken into refusing every
slug would satisfy the indifference assertion perfectly.

One test-level subtlety worth recording: a refused refresh still spends the
token it was given, so each refresh probe takes a fresh one. Reusing a single
token across both probes measured the replay family teardown rather than the
workspace branch.

---

## 12. Mutation results

Every mutation was applied to the working tree, run, and reverted immediately.
**No mutation remains in the final working tree** — verified by `git diff`
returning empty for each mutated file.

| # | Mutation | Expected detector | Killed? |
|---|---|---|---|
| 1 | `SET NX` → `EXISTS` then `SET` in `RefreshTokenStore.spend` | `test_contenders_spending_one_refresh_token_have_exactly_one_winner` | **Yes** — 8 winners instead of 1 |
| 2 | `OAuthFlowStore.spend` decides on the read payload rather than on `DEL`'s result | `test_contenders_spending_one_oauth_state_have_exactly_one_winner` | **Yes** — 8 winners instead of 1 |
| 3 | ignore the conditional consume result in password reset | `test_four_redemptions_of_one_reset_token_leave_one_usable_password` | **Yes** — `[200,200,200,200]` |
| 4 | ignore the conditional consume result in email verification | `test_five_submissions_of_one_verification_code_verify_once` | **Yes** — `[200,200,200,200,200]` |
| 5 | 403 for a foreign workspace, 404 for a missing one | `test_a_foreign_workspace_and_a_nonexistent_one_answer_identically` | **Yes** — 3 of 9 failed |
| 6 | remove the `RefreshTokenReplayDetected` rule | `promtool test rules` | **Yes** |
| 7 | insert two live password-reset rows for one account | `uq_password_reset_tokens_active` | **Yes** — `UniqueViolationError` |
| 8 | restore the stale "invited to one" sentence in `docs/AUTH.md` | `test_a_corrected_claim_has_not_come_back` | **Yes** |
| 9 | restore the stale "same 400" in the verification docstring | `test_the_route_documents_the_status_it_actually_returns` | **Yes** |

### Two results worth recording rather than summarising away

**The old unit suite is blind to mutation 1.** Under the check-then-write
`RefreshTokenStore.spend`, `tests/unit/test_token_store.py` passes 15/15 while
the new integration test fails. That is AUTH-01 demonstrated rather than
asserted: the gap was specifically atomicity, and it was invisible to the tests
that named it as their subject.

**Mutation 2 had to be corrected before it was a real mutation.** The audit
proposed replacing the atomic state spend with `GET` → `DEL`. Applied literally,
that does *not* break single use and the new test correctly passed it — because
`OAuthFlowStore.spend` branches on `deleted`, the return value of `DEL`, and
`DEL` is atomic on its own. Of two concurrent callers both reading the same
payload, only one removed the key.

So `MULTI`/`EXEC` is a round-trip saving and defence in depth here; it is not
where the guarantee lives. The faithful check-then-write mutation moves the
*decision* to the payload that was read, and that one is killed immediately. The
distinction matters for whoever maintains this next: the line to protect is
`if not deleted`, not the pipeline around it.

---

## 13. Concurrency results

| Path | Contenders | Winners | Losers | 500s | Duplicate rows |
|---|---|---|---|---|---|
| refresh token spend (Redis) | 8 | 1 | 7 | 0 | — |
| OAuth state spend (Redis) | 8 | 1 | 7 | 0 | — |
| password reset redemption | 4 | 1 | 3 | 0 | 0 |
| email verification | 5 | 1 | 4 | 0 | 0 |
| password reset *request* | 4 | 4 × 202 | — | 0 | 1 live token |

Password-reset *request* is the one path where every caller wins, correctly: the
endpoint's contract is a single constant answer, so four 202s with one live
token left behind is the right outcome, and it is what the advisory lock exists
to produce.

---

## 14. Quality gates

```
ruff check .            All checks passed!
black --check .         534 files would be left unchanged.
mypy app                Success: no issues found in 245 source files
pytest                  4075 passed, 76 skipped, 565 warnings in 602.08s   (exit 0)
alembic check           No new upgrade operations detected.

promtool check config   SUCCESS — 1 rule file, 12 rules
promtool test rules     SUCCESS
amtool check-config     SUCCESS — 1 inhibit rule, 2 receivers
```

**0 failures, 0 errors.** The previous clean baseline was 4044 passed / 76
skipped; this run is 4075 / 76. The 31 additional tests are this remediation's,
and **the skip count is unchanged** — no test was skipped to make a gate pass.

All 76 skips are environmental and pre-existing:

| Count | Reason |
|---|---|
| 62 | `No object store configured; set TEST_S3_ENDPOINT_URL` |
| 11 | `no OPENAI_API_KEY in the environment; real-provider tests are opt-in` |
| 3 | `schema parity is only meaningful against a migration-built database; set WASLA_TEST_SCHEMA=migrations` |

The schema-parity three are the declared CI skip from `bf1f5e6`; CI runs that
suite separately with `WASLA_TEST_SCHEMA=migrations`, and migration `0052` was
additionally verified against a migration-built database in §9.

A note on how this number was obtained, because the first attempt did not
produce it: `pyproject.toml` sets `addopts = "-q …"`, and passing `-q` again
stacks to `-qq`, which suppresses the summary line entirely. The first run
exited 0 — so there were no failures — but printed no totals, and the run was
repeated rather than have the counts inferred from progress percentages.

---

## 15. Remaining product decisions

These are product choices, not defects, and are not relabelled as such here:

- **Registration discloses that an address is taken** (409). Accepted rather
  than fixed, and argued in ADR-040: the attacker chooses the slug, so a unique
  slug makes a 409 mean "the address exists" whatever the wording says, while a
  merged message would leave a real person unable to tell which field was wrong.
  Bounded to 10 probes per minute per client address.
- **Logout does not invalidate an already-issued access token** for up to 15
  minutes (AUTH-10). Intentional, documented, and `logout-all` is the immediate
  remedy.
- **Failed logins against addresses with no account leave no audit row.**
  Deliberate — see §6 — with the cost covered by `PasswordLoginFailureSpike`.
- MFA/TOTP, passkeys, per-device session tables, additional OAuth providers,
  email-change, SSO/SAML/SCIM and an access-token denylist remain out of scope
  and were explicitly not built.

---

## 16. Remaining external verification

Genuine remaining items only. **Nothing below was executed in this session and
nothing below is claimed to have been.**

- **Alertmanager → Slack delivery.** The rules, the routing and the receiver
  configuration are validated by `promtool` and `amtool`. That a notification
  reaches the channel needs the production webhook secret and can only be
  confirmed in the deployed environment.
- **Prometheus actually scraping the deployed API.** Configuration is valid;
  the scrape target being up is a deployment fact.
- **`alembic upgrade head` against production data.** Verified on a fresh
  isolated database and on a seeded one. If production holds accounts with more
  than one live reset token, migration `0052` will supersede the extras rather
  than fail — the count of affected rows is worth reading from the migration log
  rather than assumed to be zero.

Browser, real Google OAuth, real Resend and the ngrok webhook flows were **not**
rerun. The changes in this session are confined to tests, alert rules, audit
rows, documentation and the reset-token index; the only runtime behaviour they
touch is the advisory lock inside `PasswordResetService.request`, whose external
contract — a constant 202 — is unchanged and is covered by the concurrency
tests above. The previous audit's 25/25 browser results are historical evidence,
not current execution.

---

## 17. Final verdict

### AUTH FINDINGS CLOSED WITH EXTERNAL DEPLOYMENT CHECKS

Ten of the eleven findings are closed with executable regression proof.
AUTH-10 was revalidated, confirmed not to be a defect, and left unchanged by
design — which is what the audit asked for.

Every applicable finding has a detector that was shown to fail against the
defect it guards, rather than merely asserted to cover it. Nine mutations were
applied and reverted; all nine were killed; `git diff` is empty.

The qualifier is §16 and nothing more: three facts about the deployed
environment — that Alertmanager's Slack receiver delivers with the production
secret, that Prometheus is scraping the live API, and how many rows migration
`0052` supersedes on production data. None can be established from a
workstation, and none is claimed here to have been.

Browser, Google, Resend and ngrok flows were **not** rerun and are **not**
claimed. The changes are confined to tests, alert rules, audit rows,
documentation and the reset-token index; the only runtime behaviour touched is
the advisory lock inside `PasswordResetService.request`, whose external
contract — a constant 202 — is unchanged and covered by the concurrency tests
in §13.

### Commits

| Commit | |
|---|---|
| `6e3ab2e` | `test(auth): make the atomic single-use guarantees executable` |
| `80e7842` | `feat(monitoring): alert on the authentication security events` |
| `676e630` | `test(auth): force the reset and verification consume races` |
| `c820be3` | `test(invitations): own the settings instead of reading the developer's .env` |
| `7e984e4` | `fix(audit): record password logins and invitation transitions` |
| `7042535` | `fix(auth): let PostgreSQL keep the one-live-reset-token rule` |
| `7f824d3` | `test(auth): pin that a foreign workspace looks like no workspace` |
| `24cb4ba` | `docs(auth): correct the Google onboarding and verification contracts` |

The first six predate this session and were revalidated against current code
rather than accepted on their commit messages. No prior commit was squashed,
amended or reset, and no unrelated work was touched.
