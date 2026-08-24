# Email verification

Proving that the person holding an account can read mail at the address on
it. A six-digit code, sent through the existing outbox, verified against the
account that requested it.

This document is the security design. It is written before the flow exists so
that the answers below constrain the implementation rather than describing it
afterwards.

**Status: schema and crypto only.** The service, endpoints, template and
tests are not built yet. See [Build state](#build-state).

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

**4. What remains available before verification?** Everything. Registration,
login, workspace creation, invitations, billing, every API route. An
unverified account is a fully functional account.

**5. What happens when a user changes their email?** There is no
email-change flow in this repository today. When one is added it must set
`email_verified_at = NULL`. It does not need to remember to invalidate
outstanding challenges: each challenge stores the address it was issued for,
and verification compares that to the account's current address, so a code
sent to the old mailbox cannot verify a new one. The binding is enforced by
the data rather than by the diligence of whoever writes that flow.

**6. What happens when an OTP expires?** It stops verifying. `expires_at` is
checked on every attempt. Nothing deletes the row; an expired challenge is
inert, and the next send supersedes it.

**7. What happens when a new OTP is requested?** Every live challenge for
that account is superseded in the same transaction that creates the new one.
At most one code is ever valid, guaranteed by a partial unique index rather
than by service logic.

**8. How is brute force prevented?** Three independent bounds. The code space
is a million values; `attempts` is capped at 5 per challenge, after which the
challenge is dead even for the correct code; and the verify endpoint is rate
limited. Argon2 also makes each guess cost real CPU, which matters for an
offline attack on a stolen database.

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
and returns. The worker retries with backoff. This is why the request never
touches the provider (ADR-042).

**14. What happens if the email worker crashes?** The challenge already
exists and stays valid until it expires. The outbox row is recovered by the
existing stuck-row sweep and re-sent. Delivery is at-least-once, so a person
may receive the same code twice - harmless, since it is one challenge.

**15. What is audited?** Requested, succeeded, failed, and
rate-limited - plus supersession implicitly, since a request supersedes.
Never the code.

**16. What may appear in logs?** `user_id`, `challenge_id`, attempt number,
outcome category. **Never** the code, and not the address either - the
account id identifies the account.

**17. What happens to old challenges?** Superseded, then left. They are
small, they carry no usable secret once dead, and adding a cleanup job for
them would be inventing operational work this repository does not otherwise
have. If retention ever matters they can be swept on age.

**18. Does verification affect workspace membership?** No. Membership is a
row in `memberships`; nothing in this feature reads or writes it.

**19. Does verification affect authentication?** No. Login, refresh, logout
and `token_version` are untouched. An unverified account signs in normally.
Verifying does not mint, revoke or alter any token.

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
- **Verification costs CPU.** Bounded by the attempt cap and the rate limit.

Comparison is Argon2's own verify, which is constant-time with respect to the
stored value. No hand-rolled comparison, no custom scheme.

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
| Attempt counting | A conditional UPDATE, so concurrent wrong guesses cannot both read the same count and write the same increment |

## Rate limiting

Two separate policies, because they defend different things: sending is an
abuse and cost control, verifying is an anti-guessing control.

Both are keyed by **account**, not by client address. Keying by address would
let an attacker rotate source addresses to buy attempts, and keying a lockout
by account is only safe because neither policy locks anything - they refuse
requests for a window, and the attempt cap that does end a challenge is
per-challenge, so a stranger cannot burn an account's ability to verify by
spending its budget. Requesting a fresh code is always available.

Both set `local_fallback=True` per ADR-040.

## Observability

Events: `email_verification.requested`, `.sent`, `.verified`, `.failed`,
`.rate_limited`, `.superseded`.

Carrying `user_id`, `challenge_id`, `attempt`, and an outcome category.

**Never logged:** the code, in any form - not truncated, not hashed, not in
an exception message, not in audit metadata, not in a trace. The template
renders it at send time from the outbox context, and the outbox clears
context on terminal transition, so the plaintext code's persisted lifetime is
bounded by delivery.

That last point is a real weakening of the outbox's usual guarantee and is
stated plainly rather than buried: **the plaintext code sits in
`email_messages.context` until the row reaches a terminal state.** It is
unavoidable given that the worker, not the request, renders the message; the
same is already true of reset and invitation tokens (ADR-042). It is bounded
by delivery, never logged, and exposed by no endpoint.

## Build state

Honest accounting.

**Built:** the `email_verified_at` column, the
`email_verification_challenges` table with its constraints and indexes,
migration 0028, and the OTP generation and hashing helpers.

**Not built:** the service, the repository, the two endpoints, the
`EMAIL_VERIFICATION` template, the configuration settings, the audit actions,
the rate-limit policies, registration integration, and every test.

**Not verified:** nothing in this phase has been executed, linted,
type-checked or migrated. No test has run. No code has been emailed.
