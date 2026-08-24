# Google Sign-In

**Build state: design only. Nothing described below is implemented yet.** This
document is committed ahead of the code so the decisions can be argued with on
their own terms instead of being reverse-engineered from an implementation. Each
subsequent commit moves items from "designed" to "built", and the final section
is the honest ledger of which is which.

---

## What this is, and is not

This is a second way to *prove who you are*. It is not a second session system,
not a second token format, and not a second authorization model.

After Google has been believed, the code path rejoins password login at exactly
the point where password login stops caring how the person was identified: the
same `AuthenticatedSession`, minted by the same `AuthService`, carrying the same
claims, the same issuer, the same `token_version`, and the same refresh
rotation and reuse detection. A Google session and a password session are
indistinguishable downstream, which is the property that keeps every existing
authorization dependency, tenant isolation check and membership rule applicable
without modification. Anything that had to be taught about Google would be a
place where Google could bypass it.

It is also not multi-factor authentication and not email verification. It
replaces the password step, nothing more.

---

## ADR-043: Wasla is a confidential OIDC client, and uses PKCE anyway

**Decision.** Authorization Code Flow, server-side, with a client secret held
only by the API - a confidential client. PKCE is used on top of it.

**Why confidential.** The secret lives in `GOOGLE_CLIENT_SECRET`, is read by the
API process, and is sent only in the direct server-to-server token exchange. It
is never in a response body, never in a frontend bundle, never in a redirect
URL. There is a server that can keep a secret, so the client is confidential;
claiming otherwise would mean giving up client authentication for nothing.

**Why PKCE regardless.** PKCE is not *required* for a confidential client, and a
reading of the specification alone would skip it. It is implemented because it
defends against a different attack than client authentication does:
authorization-code injection, where an attacker who obtains a code by some other
means redeems it in a flow they control. The client secret does not stop that -
the attacker is talking to the same client. The code verifier does, because the
verifier is generated per flow and never leaves the server. RFC 9700 recommends
PKCE for all clients for this reason, and it costs one random string.

**Rejected: accepting an ID token from the frontend.** A `POST` carrying a
Google ID token would still need full cryptographic validation, so it is not
insecure by construction - but it moves the authorization code into the browser,
loses client authentication, and makes the API's trust boundary the frontend's
correctness. It also has no way to bind a nonce it did not issue.

**Rejected outright: `POST /auth/google {"email": ..., "name": ...}`.** This is
not authentication. It is a request to be issued a session for an arbitrary
address. It is recorded here only so that nobody later mistakes it for a
simplification.

### The deviation: the callback is a POST, and the redirect URI is the frontend

The brief asked for `GET /auth/google/callback` as the registered redirect URI.
This implementation does something different and the difference is deliberate.

This API is cookieless. It authenticates with bearer tokens and returns them in
response bodies. If Google redirected a top-level browser navigation straight to
an API endpoint, that endpoint's only way to deliver a session would be to
render a document containing an access token and a refresh token - visible on
screen, unreadable by the single-page application that actually needs it, and
present in whatever screenshots and screen recordings follow.

So: `GOOGLE_REDIRECT_URI` points at a frontend route. Google redirects the
browser there. The frontend reads `code` and `state` out of its own URL and
posts them to `POST /auth/google/callback`, which is a `fetch` whose response
body never becomes a rendered document.

Nothing security-relevant moves to the browser. The authorization code is still
exchanged server-side, still authenticated with the client secret, still bound to
the PKCE verifier that only the server holds. The browser sees a single-use code
that is worthless without that verifier. The redirect URI is still fixed
configuration, still exact-matched by Google against its registered value, and
still sent unchanged in the token exchange as RFC 6749 requires.

The alternative that keeps a `GET` callback is to set an `HttpOnly` cookie, or
to redirect onward with a one-time handoff code. Both are defensible. Both
introduce machinery this codebase does not currently have, which is a larger
change than this feature should make unilaterally.

---

## ADR-044: Identity lives in its own table, keyed by issuer subject

**Decision.** A `user_identities` table holding `(user_id, provider,
provider_subject, created_at, updated_at, last_login_at)`. No Google columns on
`users`.

**The key is `sub`, and only `sub`.** Google's subject identifier is stable for
the life of the account and is the only field in the token that is. The email
address can be renamed. The display name changes on a whim. The picture URL
rotates. A hosted-domain account can be moved between domains. Keying on any of
those means that an attribute change silently becomes an identity change, which
in the best case locks someone out and in the worst case hands their account to
whoever inherits the old address.

**Two constraints carry the policy.**

- `(provider, provider_subject)` unique. The same Google account can never be
  two Wasla accounts. This is also what makes the concurrent first-login race
  safe: two callbacks for one subject both see no identity, both insert, one
  commits, and the loser is told by PostgreSQL rather than by luck.
- `(user_id, provider)` unique. One issuer per account. This settles what
  "unlink Google" means - there is exactly one row - and makes duplicate linking
  a database error rather than a code path.

**Multiple identities per user: supported by shape, bounded by policy.** The
table can hold several rows per user, one per provider, so adding a second
issuer later is a new enum label and no schema change. What is deliberately
*not* supported is two Google accounts on one Wasla user. It is a real use case
and a rare one, and allowing it makes unlinking ambiguous for everybody in order
to serve it. Revisit if users actually ask.

**Stored: nothing that could be replayed.** No id token, no access token, no
Google refresh token. There is no feature that calls a Google API on a user's
behalf, so there is nothing to store them *for* - and an unused credential in a
database is pure liability. The provider's copy of the email address is also
absent: it is a second copy of personal data that goes stale on rename, and its
presence would invite the exact mistake this design exists to prevent.

---

## ADR-045: A matching email address never links an account

**Decision.** When a validated Google token presents an email that already
belongs to a Wasla account, and no identity row connects them, authentication is
**refused**. No silent attach, no silent login, no merge, no password change.
Linking requires an authenticated request from the account itself.

**Why this is not paranoia.** "Google says this person controls
`user@example.com`" and "this person controls the Wasla account registered as
`user@example.com`" are different statements. They come apart whenever an
address has changed hands - a former employee's corporate address reissued to a
new hire, a lapsed domain re-registered, a recycled address at a consumer
provider. Treating the first as proof of the second means that whoever holds the
mailbox *today* inherits the Wasla account of whoever held it *before*, without
ever seeing a password.

**The enumeration question, answered rather than waved at.** The refusal has to
say something useful, and "an account already exists with this address" is
literally an existence oracle. It is nonetheless the right response, under one
condition: the caller has just proved to Google that they read mail at that
address. Someone who controls the mailbox can already obtain that account
through password reset. Disclosing existence to them reveals nothing they could
not otherwise get, and withholding it produces a support ticket instead of a
resolution.

That argument depends entirely on mailbox control being proved, which leads
directly to the next decision.

**When `email_verified` is false, nothing is disclosed and nothing is created.**
The address is refused with a message about Google, not about Wasla, and the
check happens *before* any lookup - so the response is identical whether or not
an account exists. An unverified claim proves nothing about a mailbox, so it may
neither create an account nor reveal one.

**Once an identity exists, the email claim is not consulted at all.** Subsequent
logins resolve on `sub`. A user who renames their Gmail keeps signing in; a
renamed address does not reopen the collision question.

---

## ADR-046: Google's verified email is trusted, because the column it writes grants nothing

**Decision.** A cryptographically validated `email_verified: true`, for an
address matching the Wasla account's own, sets `users.email_verified_at` if it
is currently `NULL`. It never clears it and never overwrites an earlier one.

**Why this is safe here specifically.** `app/db/models/user.py` is explicit that
this column grants nothing: no route reads it, no permission depends on it,
authentication does not consult it. It is an account-integrity fact. So the
worst case of trusting Google wrongly is a wrong fact in a support tool, not an
escalation - and that asymmetry is the whole basis of this decision. Were the
column ever to become an authorization input, this ADR must be reopened before
that change lands, not after.

**Why the claim is worth something.** After full OIDC validation the claim
arrives inside a token signed by Google, issued to this client, for this nonce.
For a consumer account, `email_verified: true` means Google owns the mailbox.
For a hosted domain it means the domain's administrator asserts it - and that
administrator controls the mailbox anyway, so nothing is gained by disbelieving
them. Either way the assertion is at least as strong as Wasla's own six-digit
code, which proves only that somebody read one message.

**Bounds, because a claim about the wrong address proves nothing.** The
verification is recorded only when the validated Google address equals the
user's current Wasla address. Linking a Google account under a different address
sets nothing. This matters because the same model file warns that a future email
change must reset the column to `NULL`; recording a verification sourced from a
different address would defeat that.

**Source of record.** The verification source is recorded in the audit trail,
not in a new column. A `verification_source` column would be a schema change to
store something asked about roughly never, and the audit log is where "how did
this become true" already lives.

**Never from the frontend.** No request body may assert `email_verified`. The
only path to this column via Google is a token that passed signature, issuer,
audience, expiry and nonce validation.

---

## ADR-047: State and nonce are Redis-only, and a Redis outage refuses the callback

**Decision.** Every authorization attempt stores a flow record in Redis under an
unpredictable `state`, holding the nonce, the PKCE verifier, the flow kind, and
for a link flow the authenticated user id. The callback consumes it atomically.
If Redis is unavailable, the callback fails closed.

**Single use is one operation.** `GET` then `DEL` inside a `MULTI`/`EXEC`, so of
two concurrent callbacks presenting the same state exactly one sees the delete
succeed. Reading, validating and then deleting would be a race whose losing
branch is a replayed authorization. This is the same reasoning as
`RefreshTokenStore.spend` under ADR-039: whether the key was still there *is*
the answer. `GETDEL` would be one round trip instead of two but requires Redis
6.2, and the deployment's server version is not something this code can assume.

**Why not the local fallback.** ADR-040 lets a rate limiter fall back to a
process-local window when Redis is down, because refusing traffic on an
infrastructure outage is worse than allowing it. That reasoning does not
transfer. A process-local state store breaks the moment uvicorn runs more than
one worker: the callback lands on a process that never issued the state, and
"not found" would have to mean "accept anyway" for the flow to work at all. That
is not degraded capacity, it is a disabled CSRF and replay control. ADR-040
itself says security controls must not fail open, so state and nonce live in
Redis alone and a Redis outage means Google sign-in is unavailable. Password
login is unaffected.

**The residual gap, stated plainly.** State is unpredictable, single-use,
short-lived and server-side, and the PKCE verifier and nonce are bound to it -
which closes code injection and replay. What it does not do, for a login flow,
is prove that the browser finishing the flow is the browser that started it.
That binding needs a cookie, and this API has no cookie infrastructure.
Introducing one touches `SameSite`, domains and CSRF posture for every other
endpoint, which is a larger change than this feature should make on its own.

The practical consequence is narrower than it sounds, because the callback
returns tokens in a `fetch` response body and sets nothing in the browser. The
classic OAuth CSRF - victim silently signed into the attacker's account by a
forced callback - depends on the callback establishing browser state, which this
one does not. For the **link** flow the binding is strong regardless: the flow
record holds the user id of the caller who created it, so a link can only ever
attach to that account.

---

## The flow, end to end

### Login

1. `POST /auth/google/authorize`. Unauthenticated. Generates `state`, `nonce`
   and a PKCE verifier, stores the flow in Redis with a short TTL, returns the
   Google authorization URL. `POST` rather than `GET` because it writes server
   state, and a state-creating `GET` is prefetchable.
2. The browser goes to Google. Google authenticates the person and redirects to
   the configured frontend redirect URI with `code` and `state`.
3. `POST /auth/google/callback` with `{code, state}`. The flow record is
   consumed atomically. The code is exchanged server-side for an ID token. The
   ID token is validated in full. Only then is any claim in it believed.
4. Resolution, in this order:
   - identity exists for `(google, sub)` -> load that user, enforce account
     status, open a session, stamp `last_login_at`.
   - no identity, `email_verified` false -> refuse.
   - no identity, address belongs to an existing account -> refuse, and say
     that Google must be linked from inside that account (ADR-045).
   - no identity, address unknown -> create the user with no password hash,
     create the identity, record the email verification, open a session.
5. The session is the ordinary one. Access token, refresh token, current
   `token_version`, `active_workspace` if the person belongs to one.

### Linking an existing account

1. `POST /auth/identities/google/authorize`. Authenticated. Same as above, but
   the flow record carries the caller's user id and is marked as a link flow.
2. `POST /auth/identities/google/link` with `{code, state}`. Authenticated. The
   caller must be the user the flow was issued to - a flow started by one
   account cannot be finished by another, and a login flow cannot be finished as
   a link.
3. The identity is attached to *that* user. Never to whichever user matches the
   email address. If the Google subject already belongs to someone else the
   attempt is refused and audited, and the response says only that the Google
   account is unavailable for linking.

### Unlinking

`DELETE /auth/identities/google`. Authenticated. Refused if it would leave the
account with no way to sign in - which is exactly the case where the user has no
password hash and this is their only identity. The recovery path for a
Google-only account that wants to stop using Google is: request a password
reset, set a password, then unlink.

---

## Outbound requests to Google

Two, both to fixed literal endpoints, neither influenced by request input.

| Purpose | Endpoint | When |
| --- | --- | --- |
| Token exchange | `https://oauth2.googleapis.com/token` | Once per callback |
| Signing keys | `https://www.googleapis.com/oauth2/v3/certs` | On cache miss or unknown `kid` |

Both go through `build_guarded_client` from `app/core/net.py`, which resolves the
host once, refuses any answer that is not globally routable, and pins the
connection to the address it judged while preserving the `Host` header and SNI so
certificate verification still binds to the name. That closes DNS rebinding,
which the module's docstring records as having been reproduced against an earlier
version of itself.

No discovery document is fetched. OIDC discovery would be a third network
dependency whose only output is two URLs that have not changed in a decade, and
it would put the JWKS URI under the control of whatever the discovery response
said. The endpoints are constants. **The client can never supply a JWKS URL.**

**Userinfo is not called.** The ID token already carries `sub`, `email`,
`email_verified` and `name`, and it carries them inside a signature. The
userinfo endpoint returns the same fields with the authenticity of an HTTPS
connection and an access token instead - weaker evidence, one more request, one
more failure mode, one more timeout. There is no field this feature needs that
only userinfo has. If one ever appears, the ID token remains authoritative for
identity and userinfo may supplement it for profile decoration only.

Responses are read with a byte cap. An unbounded read from a host that has
stopped behaving is a memory exhaustion vector, and "it is Google" is not an
argument that survives a hijacked resolver.

Google's error bodies are never returned to the caller. They are provider
internals, they occasionally contain the request that produced them, and a
caller who can read them learns about the client configuration.

---

## What is never written down

Not logged, not audited, not stored in any table:

- the client secret
- authorization codes
- ID tokens, in whole or in part
- Google access tokens and Google refresh tokens
- `state`, `nonce`, and the PKCE verifier

The audit trail records the action, the account, and a reason category. It does
not record the provider subject: it is a stable cross-service identifier, the
row it would duplicate already exists in `user_identities`, and an audit log is
read by people. A log line may name a `kid` or an error class, never a token.

---

## Rate limiting and degradation

Authorization initiation, callbacks, and linking are limited per client identity
using `client_identity` from `app/api/rate_limits.py`, which believes a
forwarding header only when the immediate peer is a configured trusted proxy and
then walks `X-Forwarded-For` from the *right*. `X-Forwarded-For[0]` is chosen by
the caller; the module's docstring records that trusting it once made
authentication rate limiting inert in production.

These are credential-adjacent limits, so they use the ADR-040 local fallback:
when Redis is unavailable, counting continues in-process rather than stopping.
That is weaker than a shared counter and stronger than nothing.

Note the deliberate asymmetry with ADR-047. A rate limiter that loses Redis
degrades to a local approximation, because its job is to slow an attacker down
and refusing all traffic would be the worse failure. The state store that loses
Redis refuses, because its job is to make a replay impossible and a local
approximation of it is not weaker but absent.

---

## Failure modes

| Situation | Behaviour |
| --- | --- |
| Google configuration incomplete | Startup fails in production; endpoints answer 404 when disabled |
| Redis unavailable | Google sign-in unavailable; password login unaffected |
| JWKS unreachable, cache warm and fresh | Cached keys used |
| JWKS unreachable, cache stale or empty | Authentication refused |
| Unknown `kid` | One bounded refresh, then refused |
| Google outage on token exchange | Refused with a generic message; no partial account created |
| Account disabled | Refused, audited, no tokens issued, account not reactivated |
| Address belongs to another account | Refused with linking guidance (ADR-045) |
| Subject already linked elsewhere | Link refused and audited; the existing link is not moved |

---

## Operating it

**Google Cloud setup.** Create an OAuth 2.0 Client ID of type *Web application*.
Register the exact redirect URI, including scheme, host, port and path - Google
exact-matches it, so a trailing slash is a different URI. Request the `openid`,
`email` and `profile` scopes and nothing else: a scope that is not requested is
a scope that cannot be abused.

**Client secret handling.** `GOOGLE_CLIENT_SECRET` belongs to the API process
only. Never a Docker build argument, never in an image layer, never in a
frontend bundle, returned by no endpoint including `/health`. Rotating it is a
configuration change and a restart; Google permits two secrets briefly, so
rotate by adding, deploying, then removing.

**Key rotation.** Google rotates signing keys without notice. Nothing here
pins a key. An unknown `kid` triggers one bounded refresh, and a rotation is
therefore invisible apart from a single cache miss. A `kid` that is still
unknown after refresh is refused - it is either a forgery or a key Google has
not published.

**Google outage.** Sign-in fails; nothing else does. Password login, refresh,
and every workspace endpoint are unaffected, because no request path other than
these endpoints talks to Google.

**Recovery when Google access is lost** - account deleted, domain moved, or the
organisation drops Google. If the user has a password, they sign in with it and
unlink. If they do not, the path is password reset to the verified address, then
unlink. If the address itself is gone, this becomes a support operation against
the database, and that is the honest answer: an account whose only credential is
an issuer nobody controls any more cannot be recovered by self-service.

---

## Build state

Design committed. **No implementation exists at this commit.**

| Piece | State |
| --- | --- |
| ADR-043 through ADR-047 | Written |
| `user_identities` table and `IdentityProvider` | Built (migration 0030) |
| Audit vocabulary | Not built |
| Configuration and validation | Not built |
| OIDC validation and JWKS cache | Not built |
| State/nonce/PKCE store | Not built |
| Endpoints | Not built |
| Linking and unlinking | Not built |
| Rate limits | Not built |
| Tests | Not built |

Nothing in this repository has been executed for this feature. No linter, type
checker, test run, or migration has been observed against it.
