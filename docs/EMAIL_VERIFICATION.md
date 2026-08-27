# Email verification

Proving that the person holding an account can read mail at the address on
it. A six-digit code, sent through the existing outbox, verified against the
account that requested it.

This document is the security design. It was written before the flow existed,
so that the answers below constrained the implementation rather than
describing it afterwards. Where implementation showed an answer to be wrong,
the answer has been corrected here rather than left standing - three were,
and they are called out in [Build state](#build-state).

**Status: implemented, unexecuted.** Schema, crypto, repository, service,
template and endpoints exist. Configuration settings, registration
integration and tests do not. Nothing in this phase has been run. See
[Build state](#build-state).

## What this is, and is not

**Verification proves** that at the moment of verification, somebody able to
read mail delivered to that address also held a valid session for that
account. That is all.

**It does not prove:**

| Not proved | Why it matters |
| --- | --- |
| That the address is still controlled by that person | Mailboxes are lost, resold and taken over. A timestamp records a past fact, not a present one |
| That the person is who they say they are | An address is not an identity document |
| That the account is safe to trust | Verification is not a reputation signal |
| A second authentication factor | The code travels to an address that password reset also trusts. Treating it as MFA would be circular - see [Not MFA](#not-mfa) |
| Anything about a workspace | Verification is a property of a global account, not of a membership |

### The twenty questions

**1. What does verification prove?** See above: inbox control at a moment in
time, by somebody already authenticated as that account.

**2. What does it not prove?** The table above. Above all it is not a factor
and not an identity claim.

**3. Which application actions require a verified email?** **None.** This is
the most important answer in the document. No route checks
`email_verified_at`, no permission depends on it, no entitlement reads it.
Wasla's authorization model is membership plus role plus entitlement:
workspace access comes from a `memberships` row, platform authority from
`users.platform_role`, and plan limits from a subscription. A verified
address is not an input to any of those, and adding it as one would be
inventing a product rule that nobody has asked for.

> Email verification is currently an account-integrity primitive and does not
> independently grant or revoke authorization.

This is verifiable rather than aspirational: `email_verified_at` is written in
exactly one place, `EmailVerificationService.confirm`, and read in exactly
one, the same service's short-circuit for an already-verified address. No
dependency in `app/api/dependencies.py` consults it.

**4. What remains available before verification?** Everything. Registration,
login, workspace creation, invitations, billing, every API route. An
unverified account is a fully functional account.

**5. What happens when a user changes their email?** There is no
email-change flow in this repository today. When one is added it must set
`email_verified_at = NULL`. It does not need to remember to invalidate
outstanding challenges: each challenge stores the address it was issued for,
and both `is_usable` and the consuming UPDATE compare that to the account's
current address, so a code sent to the old mailbox cannot verify a new one.
The binding is enforced by the data rather than by the diligence of whoever
writes that flow.

**6. What happens when an OTP expires?** It stops verifying. `expires_at` is
checked on every attempt and again inside the consuming UPDATE, so a
challenge that lapses mid-request does not verify. Nothing deletes the row;
an expired challenge is inert, and the next send supersedes it.

**7. What happens when a new OTP is requested?** Every live challenge for
that account is superseded in the same transaction that creates the new one.
At most one code is ever valid, guaranteed by a partial unique index rather
than by service logic.

**8. How is brute force prevented?** Three independent bounds. The code space
is a million values; `attempts` is capped at 5 per challenge, after which the
challenge is dead even for the correct code; and the verify endpoint is rate
limited to ten attempts per fifteen minutes per account. An attempt is
counted *before* the code is compared, so concurrent guesses cannot slip
between a read and an increment. Argon2 also makes each guess cost real CPU,
which matters for an offline attack on a stolen database.

**9. How is enumeration prevented?** The send endpoint is **authenticated**,
which removes the enumeration surface rather than mitigating it: the target
is taken from the session, never from the request body, so there is no
address to probe. An unauthenticated variant would have to answer
identically for unknown, unverified and already-verified addresses; not
having one is simpler and strictly safer. See
[Why the send endpoint is authenticated](#why-the-send-endpoint-is-authenticated).

**10. How are tokens protected at rest?** Argon2id, salted per row. Never
the plaintext code. See [Storage](#storage).

**11. What is the replay behaviour?** `consumed_at` is written by a single
conditional UPDATE, so a code verifies exactly once however many requests
carry it. A replay finds no live challenge and fails.

**12. What is the Redis outage behaviour?** Both policies carry
`local_fallback=True`, following ADR-040: these sit in front of a credential,
so an outage must not mean unlimited attempts. The fallback is a
process-local counter - weaker than a shared one, and enormously stronger
than nothing. Fail-closed was rejected for the reason ADR-040 already gives:
it makes the flow unusable whenever the cache is down, and it is
attacker-triggerable by anyone who can degrade Redis.

**13. What happens if Resend is unavailable?** Nothing, from the caller's
perspective. The send endpoint writes an outbox row in its own transaction
and returns 202. The worker retries with backoff. This is why the request
never touches the provider (ADR-042).

**14. What happens if the email worker crashes?** The challenge already
exists and stays valid until it expires. The outbox row is recovered by the
existing stuck-row sweep and re-sent. Delivery is at-least-once, so a person
may receive the same code twice - harmless, since it is one challenge.

**15. What is audited?** Three actions in the closed `AuditAction`
vocabulary: `EMAIL_VERIFICATION_REQUESTED`, `EMAIL_VERIFIED`, and
`EMAIL_VERIFICATION_FAILED`. The failure action carries a `reason` category
in its metadata - `malformed`, `no_active_challenge`, `expired`,
`attempts_exhausted`, `address_changed`, `wrong_code`, `lost_race` - rather
than being split into an action per reason, because they are all answers to
the same question and a burst of them against one account is the signal worth
alerting on whatever the reason says. The request action records how many
challenges it superseded. Never the code, and never a hash of one.

**Every refusal is committed before it is raised**, and this is the single
most important implementation detail in the feature. `confirm` raises on
failure, and an exception unwinds the request's transaction - so a failure
recorded the ordinary way is discarded by the very refusal that records it.
That is not a missing log line. `attempts` is what ends a challenge, so a
counter that rolls back is an attempt cap that **does not exist**: seven wrong
codes against a running container left `attempts` at zero, and guessing was
bounded only by the rate limit.

`_reject` therefore records the entry, commits, and lets the caller raise.
Everything written by that point is meant to survive - the increment and the
entry - and `email_verified_at` is set only on the success path, which no
failure follows, so there is nothing half-finished being made durable. It is
the arrangement `AuthService._tear_down_after_reuse` already uses for a
consequence that must outlive the request triggering it.

A refused rate limit goes through the same path, as `EMAIL_VERIFICATION_FAILED`
with a `rate_limited` reason.

**16. What may appear in logs?** `user_id`, `challenge_id`, the number of
challenges superseded, and an outcome category. **Never** the code, and not
the address either - the account id identifies the account.

**17. What happens to old challenges?** Superseded, then left. They are
small, they carry no usable secret once dead, and adding a cleanup job for
them would be inventing operational work this repository does not otherwise
have. If retention ever matters they can be swept on age.

**18. Does verification affect workspace membership?** No. Membership is a
row in `memberships`; nothing in this feature reads or writes it. The
endpoints deliberately sit outside the workspace router group so that they do
not even resolve a membership - see
[Why these routes are not workspace-scoped](#why-these-routes-are-not-workspace-scoped).

**19. Does verification affect authentication?** No. Login, refresh, logout
and `token_version` are untouched. An unverified account signs in normally.
Verifying does not mint, revoke or alter any token, and the verify response
contains a timestamp rather than a credential.

**20. Does verification affect password reset?** No, and deliberately not.
Reset already treats delivery to the address on file as proof of control -
that is the whole mechanism - so gating reset on prior verification would
lock out exactly the people who most need it while adding no security.

## Not MFA

A code emailed to the address that can already reset the password is not a
second factor. If reset can take over the account by mail, then mail is the
first factor's equal, and requiring both proves nothing extra.

So this flow deliberately does not: gate login, mint or invalidate tokens,
touch `token_version`, or offer any passwordless path. There is no endpoint
that exchanges a code for a session. The only state it writes is one
timestamp.

This is structural rather than merely intended: `TokenClaims` carries no
verification field, so verification cannot reach an authorization decision
without somebody deliberately adding it to the token.

## Why the send endpoint is authenticated

The target address is read from the session, never from the request. This
kills three problems at once rather than mitigating them:

- **Enumeration** - there is no address parameter to probe, so no response
  or timing can distinguish a registered address from an unknown one.
- **Mail relay** - nobody can make this platform send to an address of their
  choosing. The recipient comes from trusted database state.
- **Cross-account sends** - there is no target user id to tamper with, so
  one account cannot trigger mail for another.

An unauthenticated send would be needed only if verification gated something
before first login. It gates nothing, so it is not needed.

The request body of the verify route sets `extra="forbid"`, so a client that
sends `user_id` or `email` beside its code receives a 422 rather than having
the field silently ignored. A quietly dropped parameter is how somebody comes
to believe they can verify another account.

## Why these routes are not workspace-scoped

The router is registered in `UNLIMITED_ROUTERS`, which sounds like an absence
of protection and is not. Both routes require a session, and both carry
per-account rate limits applied inside the service.

What they must not carry is the workspace limit, because that guard's
signature resolves `ActiveWorkspaceDep` - a selected tenant in the access
token and an active membership. Neither has anything to do with owning an
inbox, and a person may legitimately lack both: newly registered, workspace
not yet chosen, or membership withdrawn. Attaching it would make proving your
own email address fail for reasons unrelated to email.

This is the same defect the comment above `MIXED_ROUTERS` records having
already shipped once, when a router-level guard pulled the authentication
chain onto `/invitations/accept` and broke onboarding in production while the
tests passed.

## Storage

**Argon2id, not SHA-256.** This diverges from every other secret in the
repository and the reason is entropy. `hash_reset_token` documents SHA-256 as
correct because a 256-bit token has nothing to brute-force. A six-digit code
has about twenty bits: a million candidates, which is microseconds of
SHA-256. A leaked database would disclose every live code. Argon2 makes the
same attack cost real time per guess.

Two consequences, both acceptable:

- **Lookup cannot be by hash.** Argon2 salts each row, so the same code
  hashes differently every time. The challenge is found by account and the
  submitted code is verified against it - which is the right shape anyway,
  because verification acts on the authenticated caller's own account.
- **Verification costs CPU.** Bounded by the attempt cap, the rate limit, and
  a length bound on the request body so Argon2 is never reachable with
  arbitrary input.

Comparison is Argon2's own verify, which is constant-time with respect to the
stored value. No hand-rolled comparison, no custom scheme.

A submitted code is accepted only if it is exactly six **ASCII** digits. The
ASCII part is deliberate: `"١٢٣٤٥٦".isdigit()` is `True` in Python and
`int()` parses it, so a check written as `.isdigit()` alone would accept
Arabic-Indic numerals in a product whose users type Arabic, hash them,
compare against a verifier built from Western digits, and fail - burning an
attempt and looking, to somebody holding the right code, exactly like a
broken system.

## The email-change binding

A challenge bound only by `user_id` outlives an email change. That is a real
bypass: request a code at an address you control, change the account's
address to a stranger's, submit the code, and the account now claims a
verified address its owner never proved. It would mark somebody else's
mailbox as verified-by-you.

So the challenge stores the address it was issued for, and verification
requires it to still match the account. An `email_change_count` on `users`
with a copy on the challenge was the alternative, and was rejected: it works
only if every future writer of an email-change path remembers to bump it.
There is no such path today, so the invariant would be resting entirely on
the care of code nobody has written yet. Storing the address needs no
cooperation.

The cost is one duplicated address in a short-lived row. Not a new category
of data - it is already in `users.email` and in `email_messages.recipient`.

## Concurrency

| Race | How it is settled |
| --- | --- |
| Two sends at once | Both supersede, both insert; the partial unique index lets one row live. The loser's IntegrityError is the correct outcome |
| Two verifies with the correct code | `UPDATE ... WHERE consumed_at IS NULL RETURNING` - one wins, the loser is refused (the ADR-039 shape) |
| Verify racing a send | The new challenge supersedes the old; a code for a superseded challenge fails |
| Wrong guess racing a correct one | The consuming UPDATE re-reads `attempts` against the cap, so a concurrent guess that exhausts the challenge defeats an already-validated correct code |
| Attempt counting | `attempts = attempts + 1` evaluated by the database, so concurrent guesses cannot all read the same count and write the same increment |

The Argon2 comparison necessarily happens in Python, between reading the
challenge and writing to it. The consuming UPDATE therefore trusts nothing
the read established and re-asserts every precondition - unconsumed,
unsuperseded, unexpired, under the cap, same address - inside its WHERE
clause. That single statement is the security boundary of the feature.

## Rate limiting

Two separate policies, because they defend different things: sending is an
abuse and cost control, verifying is an anti-guessing control.

| Policy | Budget |
| --- | --- |
| `auth:verification_send` | 3 per 15 minutes |
| `auth:verification_attempt` | 10 per 15 minutes |

Both are keyed by **account**, not by client address. Keying by address would
let an attacker rotate source addresses to buy attempts, and keying by
account is only safe because neither policy locks anything - they refuse
requests for a window, and the attempt cap that does end a challenge is
per-challenge, so a stranger cannot burn an account's ability to verify by
spending its budget. Requesting a fresh code is always available.

Both set `local_fallback=True` per ADR-040.

Malformed input is not counted against the challenge's attempt budget. A typo
is not a guess, and letting bad formatting burn the budget would let a broken
client lock somebody out of verifying their own address. It still costs the
time a real check costs, so it cannot be distinguished by timing.

## Observability

Events emitted: `email_verification.requested`,
`email_verification.verified`, `email_verification.failed`, and
`email_verification.already_verified`.

Carrying `user_id`, `challenge_id`, the count of superseded challenges, and a
reason category on failure. There is no `.sent`, `.rate_limited` or
`.superseded` event - supersession is a field on the requested event, and a
rate-limited request is logged by the limiter.

**Never logged:** the code, in any form - not truncated, not hashed, not in
an exception message, not in audit metadata, not in a trace. Not the
submitted value either, since a near-miss narrows the keyspace. The template
renders the code at send time from the outbox context, and the outbox clears
context on terminal transition, so the plaintext code's persisted lifetime is
bounded by delivery.

That last point is a real weakening of the outbox's usual guarantee and is
stated plainly rather than buried: **the plaintext code sits in
`email_messages.context` until the row reaches a terminal state.** It is
unavoidable given that the worker, not the request, renders the message; the
same is already true of reset and invitation tokens (ADR-042). It is bounded
by delivery, never logged, and exposed by no endpoint.

## Lifetime and configuration

A code lives ten minutes by default. The service refuses a lifetime below one
minute or above one hour at construction, rather than clamping it: silently
correcting configuration is how an operator comes to believe a code lives for
a day when it lives for an hour.

Two settings, both read by the service on every request:

| Variable | Default | Bounds |
| --- | --- | --- |
| `EMAIL_VERIFICATION_TTL_SECONDS` | 600 | 60-3600 |
| `EMAIL_VERIFICATION_MAX_ATTEMPTS` | 5 | 1-10 |

The bounds are enforced twice, deliberately. `Settings` refuses an out-of-range
value in **every** environment, not only production, because an unusable
lifetime is unsafe on a laptop too - so a misconfigured deployment fails to
start. The service checks again at construction, because it is constructible
without settings and a bound only one of two doors checks is a bound with a
door around it.

The rate-limit numbers are **not** configurable: three sends and ten attempts
per fifteen minutes per account, as module constants beside
`RESET_TOKEN_TTL_MINUTES`, which is a constant for the same reason. Nothing
about a deployment makes one of these right and another wrong.

## Build state

Honest accounting, corrected again after the phase was finished and run.

**Built and executed:** the `email_verified_at` column and
`is_email_verified` property; the `email_verification_challenges` table with
its partial unique index, user index and cascade; migrations 0028 and 0029;
the crypto helpers in `app/core/security.py`; the
`EmailVerificationRepository` with its atomic consume, atomic attempt
increment and supersede; the `EmailVerificationService`; the
`EMAIL_VERIFICATION` template; the three audit actions; the two rate-limit
policies; the two endpoints; both settings; the registration integration;
`email_verified_at` on `GET /auth/me`; and the audit row for a throttled
attempt.

**Defects found by actually running it**, all fixed and each now pinned by a
test:

- The router carried no `route_class=CommittingRoute` - the only router in
  `app/api/v1/` without one. Verification answered `200` with a timestamp
  while the write was discarded on the way out.
- The consuming UPDATE compared `attempts < max` while the caller had already
  counted the attempt being judged, so the correct code on the last permitted
  try was refused and recorded as a lost race.
- `_dead_reason` reported `attempts_exhausted` for any challenge with a single
  failed attempt, so an address change read as brute force in the trail.
- The TTL and attempt-cap settings did not exist, so the documented knobs were
  inert.
- Registration queued nothing.
- **Every failed attempt rolled back.** The increment and the audit entry were
  staged on the request's session and discarded by the exception that reported
  the failure, so the per-challenge attempt cap - one of the three bounds this
  document rests on - did not exist in a deployment. Found by driving a
  container over HTTP: seven wrong codes, `attempts` still zero. The
  session-scoped tests could not see it, because they drive the service on a
  session nobody rolls back.

**Tests.** `tests/unit/test_verification_codes.py` (30) covers generation,
leading zeroes, the CSPRNG, Argon2 storage and the ASCII-only parser.
`tests/integration/test_email_verification.py` (35) covers the flow, expiry
boundaries, the attempt ceiling on both sides, replay, supersession, the
partial unique index under a real IntegrityError, the address binding, two
sessions racing one consume, and configuration refusal.
`tests/integration/test_email_verification_endpoints.py` (29) covers
authentication, cross-account reach, indistinguishable rejections, malformed
HTTP, both rate limits, Redis degradation, the registration integration, a
canary code that must not reach a log or the trail, and the route-class walk.

**Verified over real HTTP**, against a container and then a local server, both
on a migration-built PostgreSQL with Redis: registration queues a challenge and
its mail atomically; only an Argon2 hash reaches the row; a wrong code is
refused; the correct code verifies and the write is *durable*; a replay is
refused; attempts accumulate to the ceiling and stop; the correct code is
refused after exhaustion; a resend supersedes the old code and the new one
works; and no issued code appears anywhere in the server log.

**Still not verified.** No code has been delivered by Resend. No
`RESEND_API_KEY` was available in this environment, so every claim about
delivery is a claim about the outbox row and the rendered message, not about
mail that arrived. The first production send has to walk the checklist in
`docs/EMAIL.md`.
