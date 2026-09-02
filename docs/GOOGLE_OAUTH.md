# Google sign-in

How somebody signs in to Wasla with a Google account, why the flow is shaped
the way it is, and what has and has not been proven about it.

> **Build state.** Every piece described here is written and committed. **None
> of it has been executed.** There was no Python interpreter, no PostgreSQL, no
> Redis, no Docker and no network available while it was written, so no test
> has run, no migration has been applied and no request has been served. The
> table at the end of this document says which claims are code and which are
> observations. Read it before deploying anything.

## What this is, and is not

It is the OAuth 2.0 authorization-code flow with PKCE, and OpenID Connect on
top of it, against Google as the issuer. A Google sign-in produces the same
session as a password sign-in: the same access token, the same refresh token,
the same claims, the same issuer, the same `token_version`, the same rotation
and reuse detection. There is exactly one session model in Wasla and this does
not add a second.

It is not a way to accept an identity assertion from a browser. Nothing in the
system trusts a client-supplied email address, a client-supplied `sub`, or a
client-supplied `email_verified`. An endpoint that took an address and returned
a session would not be authentication, and none exists.

---

## ADR-047 — Wasla is a confidential client, and uses PKCE anyway

**Decision.** The API is a confidential server-side client. It holds
`GOOGLE_CLIENT_SECRET`, it performs the token exchange itself, and the browser
never holds a credential. PKCE is used in addition to the client secret.

**Why confidential.** The exchange happens in a process we run, so a secret can
actually be kept. The alternative - a public client where the browser redeems
the code - means the browser holds tokens Google issued, and this application
has no reason to put a Google token in a browser.

**Why PKCE as well, when the secret already authenticates the client.** RFC
9700 recommends PKCE for every client type, and the reason is code injection
rather than client authentication. An authorization code that leaks - through a
referrer, a proxy log, a shared browser, a redirect the operating system
handled oddly - is redeemable by anybody holding the client secret, and the
client secret is not what a code leak compromises. With PKCE the code is only
redeemable alongside a verifier that never left this process. The cost is one
hash. There is no argument for skipping it.

**Rejected: accepting a Google ID token from the frontend.** It looks simpler
and it moves the trust boundary into the browser. The server would then have to
validate a token it did not request, with no nonce it chose, meaning any token
Google ever issued for this client id would be accepted - including one
obtained by a different application the user also signed into, if it shares an
audience. The nonce is what closes that, and a nonce only means something when
the party checking it is the party that generated it.

### The deviation: the callback is a POST, and the redirect URI is the frontend

The conventional shape is `GET /auth/google/callback` on the API, with Google
redirecting the browser straight there. **This implementation does not do that**
and the reason is structural rather than stylistic.

This API is cookieless. Every existing authentication route returns tokens in a
JSON response body, and the SPA holds them. A `GET` callback reached by
top-level browser navigation would therefore have to render a document
containing a refresh token. That document is not readable by the SPA that needs
the token, and it *is* readable by anything that can see the page - a
screenshot, a shoulder, a browser extension, a shared screen.

So: `GOOGLE_REDIRECT_URI` points at a frontend route. Google redirects the
browser there with `code` and `state` in the query string. The frontend posts
those two values to `POST /auth/google/callback`, which exchanges the code
server-side using the client secret and the PKCE verifier, and answers with the
same body `/auth/login` answers.

What this preserves: the redirect URI is still fixed configuration, still
exact-matched by Google, and still sent verbatim in the token exchange as RFC
6749 requires. The frontend never sees an ID token, an access token or the
client secret. The authorization code is worthless without the verifier.

**What this costs, stated plainly.** Without a cookie, the `state` cannot prove
that the browser finishing the flow is the browser that started it. It is
unpredictable, single-use, short-lived, server-side, and it carries the nonce
and the PKCE verifier - which closes code injection and replay. It did not close
"an attacker induces a victim's browser to complete an authorization the
attacker began".

**Superseded by ADR-066.** That gap is now closed, and the cookie this section
declined to introduce is the thing that closed it. What made it worth doing was
that the trade turned out to be smaller than described: the cookie carries a
random secret and nothing else, so it introduced no CSRF surface to decide about
- an API that authenticates by bearer token still has nothing a cookie alone can
authorize. See ADR-066 for the design, and "What the frontend has to do" below
for the one thing this changes for a client.

---

## ADR-048 — Identity lives in its own table, keyed on the Google subject

**Decision.** A `user_identities` table. Columns: `id`, `user_id`, `provider`,
`provider_subject`, `created_at`, `updated_at`, `last_login_at`. Unique on
`(provider, provider_subject)` and on `(user_id, provider)`. No provider
columns on `users`, and no attributes at all on this table.

The account's display fields - `users.full_name`, `users.avatar_url` - are not
an exception to that. They are the *account's* name and picture, which Google
happens to be the only current source of; nothing about them is Google-shaped,
and a second issuer would write the same two columns. See **Profile data**
below.

**Why the subject and nothing else.** Google's `sub` is stable for the lifetime
of the account and is the only claim documented as such. An email address is
not: people change them, corporate domains change hands, and a Workspace
administrator can reassign one to a different human being. A display name, a
picture and a username are not identifiers at all - which is exactly why they
are stored as decoration on the account and never consulted to decide who
somebody is. Keying on anything but the
subject means an address change silently orphans an account, or - far worse - an
address reassignment silently hands one over.

**Why a separate table rather than `users.google_sub`.** A column would model
"a user has at most one Google account, forever". A table models "a user has
some identities", which is what is actually true the moment a second provider
is added, and costs one join today. The `(user_id, provider)` constraint is what
keeps one account from accumulating two Google identities; `(provider,
provider_subject)` is what keeps one Google account from opening two Wasla
accounts. The second is the security-relevant one and it is also the race
backstop for concurrent first logins.

**Delete behaviour.** `ON DELETE CASCADE` from `users`. An identity has no
meaning without the account it opens, so a stranded row would be a row that
grants access to nothing while occupying a unique constraint that would block
the rightful owner from reconnecting.

**No Google credential is stored.** No ID token, no access token, no refresh
token, no authorization code. (The display claims are a different matter and
are covered under **Profile data**; none of them is a credential, and none is
on this table.) Wasla calls no Google API on a user's behalf, so a
stored token would be a credential held for no purpose - and the authorization
request asks for `access_type=online`, which means Google does not issue a
refresh token at all. That is deliberately structural: "do not store it" is a
rule somebody has to remember, "it is never issued" is not.

### Audit vocabulary

Five actions: `GOOGLE_LOGIN_SUCCEEDED`, `GOOGLE_LOGIN_FAILED`,
`GOOGLE_IDENTITY_LINKED`, `GOOGLE_IDENTITY_LINK_FAILED`,
`GOOGLE_IDENTITY_UNLINKED`.

Two deliberate departures from the obvious list.

**There is no `GOOGLE_LOGIN_STARTED`.** A row written when a browser is
redirected is a row written before anybody is identified, so there is no actor
to attribute it to - and an audit row an anonymous stranger can create on
demand is a way to flood a trail colleagues have to read. It is a log line
instead.

**`GOOGLE_LOGIN_FAILED` is written only when the refusal names a real account**
- a disabled account, or an address that already has one. Cryptographic
failures get log lines, for the same flooding reason: a forged token must not
be able to write to the audit trail.

**The provider subject is not in `meta`.** It is a stable identifier that
correlates a person across every service using the same Google account, the row
it would duplicate is already in `user_identities`, and audit logs are read by
people. `meta` carries the provider and a reason.

---

## ADR-049 — A matching email address never links anything

**Decision.** When a Google subject nobody has seen before presents a verified
address that already belongs to a Wasla account, the request is **refused**.
Not merged, not attached, not signed in, and the password is not touched. The
response says an account exists and that Google must be connected from account
settings after signing in normally.

**Why refusing is the only safe answer.** Silently signing them in treats
control of a mailbox today as proof of who opened an account under that address
previously. Those are different claims. Addresses get reassigned - most commonly
when an employee leaves and their Workspace address is reissued - and the person
holding it now may have no relationship at all to the account.

**The enumeration question, and how the ordering answers it.** Naming the
collision does disclose that an account exists. That is acceptable *because of
where the check sits*: `email_verified` is validated **before any account
lookup happens**. Anybody who reaches the lookup has cryptographically proven
that Google considers them the owner of that mailbox, so they are being told
something they could already have learned by asking for a password reset.
Reverse the two checks and the endpoint becomes a directory: create a Google
account claiming any address, submit a callback, read the answer. The order is
the control, and it is asserted in the service's module docstring so that
somebody refactoring it sees why before moving it.

**Linking is bound to the account, not the address.** `POST
/auth/identities/google/authorize` requires a session and writes the caller's
user id into the server-side flow record. The eventual link attaches the
identity to *that* account. There is no request field a caller can set to
redirect it, and the token's email address is never consulted.

**A Google account already connected elsewhere is not moved.** Moving it would
let anybody who can reach a Google account walk it off the Wasla account it
currently opens and lock out the rightful owner. The attempt is audited. The
message does not say whose account holds it.

**Unlinking.** Refused when it would leave the account with no way in: no
password hash and no other identity. A person who signed up with Google and
never set a password would otherwise be able to lock themselves out with one
button. The route out is `POST /auth/password/set` (ADR-057), which is what
that refusal tells the person to use: a session is the proof, because an account
that has never had a password has nothing else to prove with.

This corrects what ADR-049 originally claimed. The stated recovery was "an
ordinary password reset", and that does not work: `PasswordResetService.request`
returns without issuing a token for an account whose `hashed_password` is
`None`, deliberately, so that a reset cannot bypass the invitation flow's own
proof. Until `/auth/password/set` existed, a Google-first account had no
supported way to acquire a password at all - and the one unsupported way, an
invitation redeemed with a chosen password, was the account-takeover path
ADR-057 closes. An account **with** a password can always unlink.

---

## ADR-050 — Google's `email_verified` is trusted, because it buys nothing

**Decision.** A validated `email_verified: true` sets `users.email_verified_at`.

**The analysis that makes this safe, and it is not about Google.** It is about
what the column does. `app/db/models/user.py` records that `email_verified_at`
**grants nothing**: no route reads it, no permission depends on it, and
authentication does not consult it. It is an account-integrity fact. So the
question is not "is Google's claim strong enough to authorize something" - it
authorizes nothing - but "is it strong enough to record as true". A claim inside
a token whose signature, issuer, audience, expiry and nonce have all been
verified is stronger evidence than a six-digit code emailed to the same address,
which is what the alternative would require.

**When it does not apply.** On the **link** path the column is set only if the
validated Google address equals the account's current address. Google proving
that somebody owns `other@example.com` says nothing about whether this account
owns the one it is registered under, and stamping it from a mismatched claim
would be verifying the wrong mailbox.

**The trigger to reopen this.** If any future route makes a decision based on
`email_verified_at`, this decision must be re-examined before that route ships.
The premise here is "the column grants nothing", and the day that stops being
true is the day this becomes a grant of access on a third party's assertion.

**Never from the frontend.** `email_verified` is read only from a validated ID
token, and the comparison is `is True` rather than a truthiness test - the
string `"false"` is truthy in Python, and a non-conforming issuer sending one
would otherwise read as verified.

---

## ADR-051 — State and nonce live in Redis, and refuse when it is gone

**Decision.** One Redis record per authorization attempt, holding the nonce, the
PKCE verifier, the flow kind and - for a link - the initiating account. Keyed by
a 256-bit `state`. Ten-minute expiry. Spent with `GET` and `DEL` inside
`MULTI`/`EXEC`. **A Redis outage refuses the flow.**

**Why one record.** Three separate stores would be three chances to validate one
value and forget another. Here they arrive as one object or not at all, so there
is no code path that checks the state and skips the nonce.

**Why `GET` + `DEL` atomically.** Read, validate, then delete is a race whose
losing branch is a replayed authorization: two callbacks both read "valid" and
both proceed. In one transaction, exactly one sees the delete return 1, and
whether the key was still there *is* the answer - the same shape
`RefreshTokenStore.spend` uses under ADR-039. `GETDEL` would be one round trip
instead of two but requires Redis 6.2, which this code cannot assume.

**Why this fails closed when the rate limiter does not.** ADR-040 lets a limiter
degrade to a process-local window, because refusing all traffic during an
infrastructure outage is worse than allowing some. That argument does not carry
over. A process-local state store breaks the moment uvicorn runs more than one
worker: the callback lands on a process that never issued the state, so "not
found" would have to mean "accept anyway" for the feature to work at all. That
is not degraded capacity, it is a disabled replay control - and ADR-040 itself
says security controls must not fail open. Google sign-in becomes unavailable;
password login is untouched.

**The flow kind is checked.** A flow begun as a login cannot be completed at the
linking endpoint, or the reverse. Without it the two endpoints would share a
state namespace and an attacker could start whichever flow has the weaker checks
and finish it at the other.

---

## The flow, end to end

### Signing in

1. `POST /auth/google/authorize` — unauthenticated. Mints `state`, `nonce` and a
   PKCE verifier; stores them; returns Google's authorization URL. A `POST`
   because it writes server state, and a state-writing `GET` is something a link
   preview will fetch on its own.
2. The browser goes to Google. Google redirects to `GOOGLE_REDIRECT_URI` - a
   frontend route - with `code` and `state`.
3. `POST /auth/google/callback` with `{code, state}`, **sent with the binding
   cookie** the first request set.
   1. Spend the state. **Before the exchange**, so a replay never reaches the
      network.
   2. Check the browser binding: the cookie must hash to the digest stored with
      the state. Also **before the exchange**, so a callback from the wrong
      browser costs no outbound request and spends no authorization code
      (ADR-066).
   3. Exchange the code for an ID token, server-side, with the client secret and
      the verifier.
   4. Validate the ID token: signature against Google's published JWKS,
      algorithm `RS256` from a literal allowlist, issuer in the two spellings
      Google uses, audience equal to our client id, expiry with 30 seconds of
      leeway, required claims present, nonce compared with `compare_digest`,
      non-empty subject, non-empty email. Claims are additionally bounded
      against the columns that will hold them: an over-long `sub` or `email` is
      refused because shortening an identifier merges identities, and an
      over-long `name` is shortened because nothing is authorized by one
      (ADR-065).
   5. Resolve: **identity exists** → load the user, enforce status, open a
      session, stamp `last_login_at`. **No identity and `email_verified` false**
      → refuse. **No identity and the address is taken** → refuse with linking
      guidance. **No identity and the address is unknown** → create a user with
      no password hash, create the identity, record verification, open a
      session.

Once an identity row exists the email claim is never consulted again. A Google
account whose address changes keeps working; one that acquires somebody else's
address gains nothing.

### Connecting Google to an existing account

1. `POST /auth/identities/google/authorize` — **authenticated**. The caller's
   user id goes into the flow record.
2. Google, as above.
3. `POST /auth/identities/google/link` — authenticated. Validates the token,
   checks the flow was started by *this* account, refuses if the Google account
   is connected anywhere, refuses if this account already has one, then inserts.

### Disconnecting

`DELETE /auth/identities/google` — authenticated. Refused when it would leave the
account unreachable.

---

## Outbound requests to Google

| Purpose | Endpoint | Timeout | Body cap |
| --- | --- | --- | --- |
| Token exchange | `https://oauth2.googleapis.com/token` | 10s | 64 KiB |
| Signing keys | `https://www.googleapis.com/oauth2/v3/certs` | 5s | 64 KiB |

Both are module constants. Both go through `build_guarded_client`, which
resolves the host, refuses any answer that is not globally routable, and pins
the connection to the address it judged while preserving `Host` and SNI so
certificate verification still binds to the name - the DNS-rebinding defence
`app/core/net.py` was written for. Both read incrementally against the cap
rather than trusting `Content-Length`.

**No discovery document.** It would be a third dependency whose only output is
two URLs that have not changed in a decade, and it would put the key document's
location under the control of whatever the discovery response said.

**Userinfo is not called.** The ID token already carries `sub`, `email`,
`email_verified`, `name` and `picture`, inside a signature. Userinfo carries the same
things without one, requires an extra round trip on the critical path of every
login, and would add a failure mode. There is no question it could answer that
the ID token has not already answered better.

**Google's error bodies are never returned to a caller and never logged.** They
are provider internals, they sometimes echo the request that produced them, and
a caller who can read them learns about this deployment's configuration.

---

## Profile data

Five claims are read from a verified token. Two of them decide things; three
are decoration, and the difference is the whole subject of this section.

| Claim | Where it goes | On a later login |
|---|---|---|
| `sub` | `user_identities.provider_subject` | Never changes; it is the key |
| `email_verified` | Gate on enrolment only | Re-checked, never stored |
| `email` | `users.email`, at enrolment only | **Ignored** |
| `name` | `users.full_name` | Followed |
| `picture` | `users.avatar_url` | Followed |

`profile` has always been in the requested scope, so `name` and `picture` cost
nothing extra to read - `picture` was simply being discarded.

### The display claims follow Google

`name` and `picture` are refreshed on every login and on every link, so a
renamed or re-photographed Google account is reflected here rather than frozen
at whatever it said the first time. Nothing is authorized by either, so
following the issuer costs nothing.

A claim that is absent leaves the stored value alone. Google omitting a field
is not the same statement as somebody clearing it, and treating the two alike
would blank a perfectly good avatar every time a token arrived without one.

The consequence, stated plainly: a name edited inside Wasla is overwritten at
the next Google login. That is the trade this direction makes, and it is the
right one while Google is the only source - the alternative is an account whose
displayed name disagrees with every other Google surface the person uses. It
would have to be revisited the day Wasla offers profile editing of its own.

### The address does not

**`users.email` is written once, at enrolment, and never again.** This is a
security property, not an omission.

Once an identity row exists the email claim is not consulted at all: a Google
account whose address changes keeps working, and a Google account that acquires
somebody else's address gains nothing (ADR-049). Refreshing the address would
hand over exactly what that is designed to prevent - control of a Google
account would become the power to move a Wasla account onto any address Google
would attest to, and every password reset thereafter would follow it.

`tests/unit/test_google_profile.py` and
`tests/integration/test_google_profile.py` both assert the address standing
still *while the display claims move*, so the omission cannot be misread as the
refresh failing to run.

### The picture URL is validated before storage

`picture` is the only claim whose value is handed straight to a browser, and it
is validated where it enters rather than escaped where it is rendered. Only
`https`, with a host, within the column length survives; anything else becomes
no picture.

The reason it needs a rule at all is that **a signature is not a safety
guarantee**. Google signs what the account says, so a `javascript:` or
`data:` value arrives perfectly signed, and an ID token is not a promise that
every claim inside it is safe to render. Refusing at the boundary means the
column cannot hold a dangerous value for *any* consumer to render - including
one written later that forgets to escape.

A rejected URL is never a rejected login. Nothing is authorized by an avatar,
and refusing a person entry to a product they pay for because of their profile
photograph would be absurd. `tests/unit/test_google_oidc.py` covers the hostile
shapes: `javascript:`, `data:`, `vbscript:`, `file:`, plain `http`,
scheme-relative, host-less, over-length, and the exact boundary.

---

## What is never written down

Not in the database, not in an audit row, not in a log line, not in a response
body: the client secret, an authorization code, an ID token, an access token, a
Google refresh token (none is issued), the nonce, the state, and the PKCE
verifier. Rejection reasons are logged as short categories - `wrong_nonce`,
`bad_signature`, `unknown_key_id` - which is the difference between a
diagnosable outage and a mystery, and none of them is token material.

---

## Rate limiting and degradation

All five routes carry a per-client-address limit on its own policy name
(`auth:google`), sharing the `RATE_LIMIT_AUTH_PER_MINUTE` budget without sharing
the counter. The address comes from `client_identity`, which believes a
forwarding header only when the peer is a configured trusted proxy, prefers
`X-Real-IP`, and otherwise walks `X-Forwarded-For` **from the right** - because
nginx appends, so `[0]` is caller-chosen.

Separate bucket rather than shared: an attacker gets two buckets instead of one,
in exchange for a burst of failed callbacks not consuming the budget somebody
signing in with a password from the same address needs. Acceptable because both
are per-address anyway and the control that actually stops credential stuffing
is the per-account limit inside `AuthService.login`.

Under a Redis outage the **limiter** degrades to a process-local window
(ADR-040) and the **flow store** refuses (ADR-051). The asymmetry is deliberate
and argued above.

---

## Failure modes

| What happened | What the caller sees |
| --- | --- |
| Feature not configured | 404. A feature nobody enabled is not here, rather than temporarily unwell |
| Bad, replayed, expired or cross-kind state | 401, one message |
| Google refused the code | 401, one message |
| Forged, expired, wrong-audience, wrong-issuer or wrong-nonce token | 401, one message |
| Google's keys unreachable | 503. Not 401 - a good account was not refused |
| Redis unreachable | 503 |
| Account disabled | 403, audited |
| Verified Google address already has an account | 409 with linking guidance, audited |
| Concurrent first login, loser | 409, retryable |
| Google account already connected elsewhere | 409, audited, not moved |
| Unlink would strand the account | 403 |

---

## Operating it

### Google Cloud setup

1. APIs & Services → Credentials → Create credentials → OAuth client ID → Web
   application.
2. Authorised redirect URI: exactly the value of `GOOGLE_REDIRECT_URI`. Google
   matches it as a literal string - trailing slashes and scheme matter.
3. Configure the consent screen. Only `openid`, `email` and `profile` are
   requested, all non-sensitive, so no verification review is required.
4. Copy the client id and secret into the deployment's environment.

### Configuration

| Variable | Notes |
| --- | --- |
| `GOOGLE_ENABLED` | Default `false`. The feature answers 404 while it is off |
| `GOOGLE_CLIENT_ID` | Must end `.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Must differ from the client id |
| `GOOGLE_REDIRECT_URI` | Absolute, no fragment, HTTPS in production |

With `GOOGLE_ENABLED=true` and any of these missing or malformed, startup fails
in production. Half-configured authentication is worse than none, because it
looks available.

### Secret handling and rotation

The secret exists only in the deployment environment. It is never in a response,
a log, a client bundle or the database. To rotate: add a second client secret in
Google Cloud, deploy the new value, then delete the old one. Google allows both
briefly, so this needs no downtime. `.env.example` carries placeholders only.

### Google's key rotation

Automatic and needs no operator action. Keys are cached for an hour, refetched
when a `kid` is unknown, and refresh attempts are capped at one a minute so a
stream of forged key ids cannot make this process hammer Google. During a
rotation a small number of logins may fail for up to a minute.

### Google outage

Token exchange or key fetch failures answer 503, not 401. Cached keys keep
working for up to 24 hours; past that they are refused, because a key old enough
to have been withdrawn without our hearing about it is not a fallback. Password
login is unaffected throughout.

### Recovery if Google access is lost

A user who loses their Google account and has a password signs in normally and
can unlink.

A user with **no** password needs a live session to set one, because
`POST /auth/password/set` proves control by the session and a password reset is
declined for an account with no password hash. So the order matters: set a
password *before* losing access to Google, which is what the unlink refusal
prompts. Somebody who has already lost Google access and never set a password
cannot self-serve, and support has to re-establish the identity - that gap is
real and is recorded in TASKS.md rather than papered over.

---

## Build state

Written, and now executed. The branch that produced this feature was merged onto
the billing line, where the suite, the linters and the migrations were run for
the first time.

| Piece | Written | Executed |
| --- | --- | --- |
| `user_identities` model and migration `0033` | yes | yes |
| Audit actions and migration `0034` | yes | yes |
| `users.avatar_url` and migration `0035` | yes | yes |
| Configuration and fail-closed validation | yes | yes |
| ID token verifier and JWKS key ring | yes | yes |
| Flow store, PKCE, Google client | yes | yes |
| Identity repository | yes | yes |
| `AuthService.authenticate_federated` | yes | yes |
| `GoogleAuthService`, including profile refresh | yes | yes |
| Five endpoints, registered and rate limited | yes | yes |
| Unit and integration tests | yes | yes |

What has run: Ruff, Black and MyPy clean; the whole suite green; `alembic
upgrade head`, `downgrade base`, re-`upgrade` and `check` against a real
PostgreSQL, with a single head and no model drift. The five routes are resolved
from the live dependency graph by `tests/integration/test_route_authorization.py`,
which is what proves the two open ones are the two that were meant.

The merge also corrected two things this branch had shipped red: thirteen lint
failures under the target line's ruff configuration, and a rotation test that
asserted the opposite of the documented `JWKS_MIN_REFRESH_SECONDS` bound.

What is still **not** done, and must be before this is deployed:

- **No real Google authentication has been performed.** The cryptography is
  exercised with controlled fixtures - real RSA keys, real signatures - and that
  is a genuine test of the verifier. It is not a test of Google. Nobody has yet
  clicked "Sign in with Google" against a real client id.
- No HTTP-level tests exist for the identity endpoints. The service paths are
  covered against a real database in `tests/integration/test_google_profile.py`
  - first login, repeat login, linking, and the address that must not move - but
  the routes themselves are proved only for their guards and their shape, not by
  driving the five of them over a socket.
- Some service paths remain untested at any level: the collision refusal, the
  disabled account, unlinking the last sign-in method, and the audit rows each
  of those writes.
- No container carrying this code has been built or booted.
- `docs/RUNBOOK.md` has no Google entry. There is no operational procedure yet
  for "Google sign-in is failing" beyond what **Failure modes** above says.

ADR-047 to ADR-051 are recorded in `DECISIONS.md`, and `docs/API.md`,
`docs/AUTH.md`, `docs/AUTHORIZATION.md`, `docs/DEPLOYMENT.md`,
`docs/SECURITY.md` and `README.md` are updated.

---

## What the frontend has to do

Two requirements, both introduced by ADR-066 and neither visible in the request
bodies.

**Send credentials on all four routes.** `authorize` sets the binding cookie and
the callback reads it, so a `fetch` that omits `credentials` will authorize
successfully and then be refused at the callback - the failure looks like a bad
state, because it deliberately cannot be told apart from one.

```js
await fetch(`${API}/auth/google/authorize`, { method: "POST", credentials: "include" });
// ... Google redirects back to the frontend route with `code` and `state`
await fetch(`${API}/auth/google/callback`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ code, state }),
});
```

The same applies to `POST /auth/identities/google/authorize` and
`POST /auth/identities/google/link`, which additionally need the `Authorization`
header they already needed.

**Deploy the frontend and the API same-site.** The cookie is `SameSite=Lax`, so
a browser withholds it from a genuinely cross-site request. `app.example.com`
and `api.example.com` are same-site and work; `app.example.com` and
`wasla-api.io` are not and will not. This is the topology `GOOGLE_REDIRECT_URI`
already assumed, and `CORS_ORIGINS` must name the frontend origin explicitly -
the middleware runs with `allow_credentials=True`, and a wildcard is refused in
production.

One consequence worth knowing before it is reported as a bug: a browser with two
Google sign-ins pending at once can complete either one, but completing one
clears the binding, so the other must be restarted. Starting sign-in again is
enough; there is nothing to clear by hand.
