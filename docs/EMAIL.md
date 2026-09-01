# Email

How Wasla sends transactional email, and what each layer is allowed to do.
The architectural decision is ADR-042 in `DECISIONS.md`; this is the working
document for people changing or operating the subsystem.

**Status: partially wired.** The infrastructure is complete and several
callers are not. See [What is not built](#what-is-not-built) before assuming
a given message gets sent.

## The rule

No application or domain code calls a provider. Nothing outside
`app/integrations/email/` imports `resend` or knows the name Resend.

A service that needs to send email queues a row:

```python
await self._outbox.enqueue(
    template=EmailTemplate.PASSWORD_RESET,
    recipient=user.email,
    idempotency_key=f"password-reset:{token.id}",
    context={"token": raw_token},
)
```

That is the whole surface. There is no `send_email(to, subject, body)`
anywhere, deliberately: an authenticated capability to mail an arbitrary
address arbitrary text is a spam relay with a login page.

## Layers

| Layer | Module | Responsibility |
| --- | --- | --- |
| Provider port | `integrations/email/base.py` | `EmailProvider.send()`, the `EmailMessage` value object, validation |
| Resend adapter | `integrations/email/resend.py` | HTTPS to `api.resend.com`, error classification |
| Fake adapter | `integrations/email/fake.py` | Deterministic, records what it was asked to send |
| Selection | `integrations/email/__init__.py` | `build_email_provider(settings)` |
| Outbox | `services/email_service.py` | `EmailOutbox.enqueue()` - the only entry point for domain code |
| Templates | `services/email_templates.py` | Closed vocabulary, text + HTML, all escaping |
| Storage | `db/models/email.py`, `repositories/email_repository.py` | Rows, claiming, state transitions, suppression |
| Delivery | `workers/email_worker.py` | Claim, send, classify, retry, back off |
| Feedback | `api/v1/email_webhooks.py`, `services/email_event_service.py` | Verified provider events |

Swapping providers is a new adapter plus one branch in
`build_email_provider`. No service, template or migration changes.

There is no Resend SDK dependency. The adapter is `httpx` against a
documented JSON endpoint - one fewer package with publish access to our
build (ADR-017) and no transitive surface for something that only needs to
POST one object.

## The outbox

Email is never sent inside an HTTP request. `enqueue` inserts into
`email_messages` on **the caller's session**, so the message and the thing
it describes commit together:

- A rolled-back invitation sends nothing.
- A provider outage does not fail the request that queued the mail.
- A request never waits on `api.resend.com`.

What the row holds is minimal on purpose. Not rendered HTML - a structured
`context` the worker renders at send time, so a leaked database dump yields
an address and a template name rather than the finished message. **No
secrets belong in an outbox row.**

The one deliberate exception is a reset or invitation token, which is the
point of those two messages. It is a short-lived single-use capability, not
a stored credential, and `mark_sent`/`mark_failed` clear `context` on every
terminal transition so it does not outlive its delivery. A token still
sitting in a pending row is a token whose email has not gone out yet.

Indexes: `(status, available_at)` for the claim query, `tenant_id` for
workspace lookups, `provider_message_id` for webhook resolution, and a
unique constraint on `idempotency_key`.

## Delivery: at-least-once

The worker is `WORKER_KINDS=email`, one kind among the existing set
(ADR-022), not a second worker system.

Claiming is `SELECT ... FOR UPDATE SKIP LOCKED`, so several worker instances
never claim the same row. Transient failures retry with exponential backoff
plus 25% jitter, from 30s to a 1h ceiling, up to `EMAIL_MAX_ATTEMPTS`.
Permanent failures stop immediately - a rejected address is not improved by
asking again. Rows stuck in `sending` for more than ten minutes, which is
what a killed worker leaves behind, are recovered.

**This is at-least-once, and it is not exactly-once.** A worker that dies
after the provider accepts a message but before the status is written will
re-send it. That window cannot be closed without a distributed transaction
across PostgreSQL and an HTTP API, so it is not closed. The cost is a
duplicate notice; the alternative - marking sent before sending - loses
messages silently, which for a password reset means somebody locked out with
a row claiming their email went out. **Duplicate email is the acceptable
failure. Lost email is not.**

**The window is one message, not one sweep.** A sweep runs in two phases.
Recovery and the claim share one transaction that marks a batch `sending`
with the attempt counted, and it commits *before* anything reaches the
provider; each message is then delivered in a transaction of its own. An
earlier version committed the whole sweep at the end, which meant a worker
killed on the fiftieth message re-sent the forty-nine before it - duplicate
mail measured in batches rather than in ones. The cost of the split is a
transaction per message, which is the right trade at transactional-email
volume.

Provider-side idempotency reduces the window but does not own the guarantee:
the adapter sends the row's key as an `Idempotency-Key` header, so a re-send
inside the provider's dedupe window is collapsed on their side. Database
idempotency exists regardless, because a provider's behaviour is not ours to
depend on.

## Idempotency keys

Every business email has a deterministic key, unique in the database. A
repeated logical event cannot produce a second message.

| Event | Key |
| --- | --- |
| Workspace invitation | `invitation:{invitation_id}` |
| Password reset requested | `password-reset:{token_id}` |
| Password changed / reset completed | `security-password_changed:{user_id}:{token_version}` |
| Sessions revoked | `security-sessions_revoked:{user_id}:{token_version}` |
| Account disabled | `security-account_disabled:{user_id}:{token_version}` |
| Account re-enabled | `security-account_enabled:{user_id}:{token_version}` |

Security notices key on `token_version`, which rises on every one of those
acts. That makes each occurrence distinct while a retried request that
re-runs the same bump stays one message.

## Templates

Every template is code in `email_templates.py`. There is no
template-by-name lookup from outside the enum and no user-supplied template.

- Subjects are **constants**. A subject travels in a header, and the way to
  guarantee nothing caller-influenced reaches a header is for no variable to.
- Variables are HTML-escaped into the HTML body, verbatim only into text.
- Links are built from `APP_PUBLIC_URL` plus a path literal, with the token
  URL-encoded into the query. **No variable is ever a URL**, so no emailed
  link can be redirected by a template variable. That is the open-redirect
  defence: there is no caller-supplied `next` or `redirect_to` anywhere.
- An incomplete context raises rather than rendering around the hole. The
  worker records that as a permanent failure of the row.

No unsubscribe link, deliberately. These are transactional - a password
change notice is not something to opt out of. The day a marketing email
exists it must be a separate system with its own consent record; adding one
here would silently convert a consent-free channel into a marketing one.

## Password reset threat model

| Concern | Defence |
| --- | --- |
| Account enumeration | `POST /auth/password-reset/request` always answers 202 with the same body, whether or not the address exists |
| Timing oracle | The work either side of the branch is equivalent; no early return distinguishes a known address |
| Token theft from the database | Only a SHA-256 hash is stored; the raw token exists in the response to nobody and in the email only |
| Token in logs | Never logged. Logs carry `email_message_id`, template and user id |
| Token in an API response | Never returned. The endpoint returns a message, not a token |
| Replay / double use | Consumed atomically; a spent token is invalid |
| Indefinite extension | Issuing a new token supersedes outstanding ones for that user |
| Old sessions surviving a reset | `token_version` bumps, invalidating every access and refresh token (ADR-036) |
| Resetting another user's password | The token is bound to a user id; the request body cannot name one |
| Open redirect | The link is built from `APP_PUBLIC_URL` and a path literal |
| Reset spam | Rate limited per client address and per account, following ADR-040 |

Rate limiting must not become the enumeration oracle the uniform response
closes. Limits are applied so that a limited request is indistinguishable
from an unlimited one in what it reveals about the address.

Redis degradation follows the existing policy (ADR-032): an outage allows
requests through rather than refusing them. Losing the limiter degrades to
unmetered resets, not to a platform nobody can sign into.

## Email verification threat model

The flow lives in `docs/EMAIL_VERIFICATION.md` and is decided by ADR-043. What
belongs here is the part that is about *email*: it is the only template whose
secret is meant to be read and retyped rather than clicked.

| Concern | Defence |
| --- | --- |
| Account enumeration | There is no unauthenticated send endpoint. Neither route takes an address or an account id; the recipient is the session's own row |
| Mailing a stranger | Same reason. A caller cannot name a recipient, so the endpoint cannot be used as a relay |
| Code theft from the database | Only an Argon2 verifier is stored, salted per row. Six digits is a million candidates, so SHA-256 would be a list rather than a hash |
| Code in logs | Never logged. Logs carry `user_id`, `challenge_id`, a supersede count and an outcome category |
| Code in an API response | Never returned, by either route, and not by registration |
| Code in a URL | There is no link. A code in a URL is a code in browser history, a `Referer` header and a proxy log |
| Code in the subject | Subjects are constants. A subject shows on a lock screen |
| Brute force | Three bounds: the keyspace, a per-challenge attempt cap, and a per-account rate limit. The attempt is counted before the code is compared |
| Replay / double use | Consumed by one conditional UPDATE; a spent challenge is invalid |
| Two valid codes at once | A partial unique index on live challenges, so the database refuses a second |
| Verifying somebody else's address | The challenge is found by account, and the request schema forbids extra fields |
| Surviving an email change | Each challenge records the address it was issued for, and both the check and the consuming UPDATE compare it to the current one |
| Code lingering in the outbox | The context carries it so the worker can render the message, and terminal transitions clear it - the reset link's arrangement, unchanged |

Redis degradation follows ADR-040 rather than ADR-032: both policies stand in
front of a guessable secret, so both carry the process-local fallback. An
outage weakens the limit and never removes it.

## Webhook trust boundary

`POST /api/v1/webhooks/email`, unauthenticated - a provider cannot hold a
credential of ours - and verified with the Svix HMAC over the exact bytes
received. Signed content is `{svix-id}.{svix-timestamp}.{body}`; the key is
the bytes `whsec_...` base64-decodes to.

**A verified delivery proves the request came from the provider. It does not
make the payload true.** Concretely:

| Proves | Does not prove |
| --- | --- |
| The request was signed with our secret | That any address in it is real or ours |
| It was signed within the last 5 minutes | That the message it names is ours |
| The bytes were not altered | That an `opened` event means a person read anything |

So the only field read from the body is `data.email_id`, resolved against
`email_messages.provider_message_id`. **An id matching no row is dropped.**
Suppression then uses the recipient on *our own row*, never an address from
the payload - which is what makes a forged bounce naming a stranger's
mailbox inert: no row of ours ever addressed it.

The response is `{"status": "accepted"}` for everything it cannot act on.
A provider retries a non-2xx and eventually disables the endpoint, so an
error for a payload that will never become valid turns one unrecognised
event into a delivery-reporting outage. The body is identical whether the
event applied, named an unknown id, or was a type we drop - a differentiated
response would be an oracle for which message ids exist.

Replay is handled by construction rather than by a seen-id table: every
transition is idempotent, and the 5-minute timestamp window bounds how long
a captured request is usable at all.

With `RESEND_WEBHOOK_SECRET` unset the endpoint returns 503 to everything,
in **every** environment. Unlike the WhatsApp webhook there is no
development bypass: exercising the send path locally needs the fake
provider, not this route, so a mode that accepts unsigned traffic would buy
nothing.

## Bounces and complaints

`email_suppressions` is a delivery-health record and nothing else. A
permanent bounce or a complaint adds the address; the worker skips a
suppressed recipient.

It is **kept entirely apart from authentication state**. Suppression never
touches `is_active`, never bumps `token_version` and never denies a sign-in.
An unreachable mailbox says nothing about the person who owns it, and an
account that could be disabled by bouncing its mail would be an account
anybody could disable.

Only permanent bounces suppress. A transient one - full mailbox, greylist -
is logged and left alone; refusing to write to that address again would turn
a temporary condition into a permanent one, and the address in question is
where somebody's password reset goes.

Opens and clicks are dropped, not stored. Neither is evidence a person read
anything, and stored signals eventually get treated as proof.

## Observability

Structured events: `email.queued`, `email.sent`, `email.retry`,
`email.failed`, `email.suppressed`, `email.event_recorded`,
`email.webhook_invalid_signature`, `email.webhook_unconfigured`.

They carry `email_message_id`, `template`, `provider_message_id`,
`tenant_id`, `user_id`, `attempt` and an error category.

**Never logged:** the API key, the webhook secret, a reset or invitation
token, a password, a JWT, an encrypted credential, or a rendered body.
Recipient addresses are not logged - the row id identifies the message, and
an address in a log is PII in a system with a different retention policy
from the database.

## Configuration

| Variable | Notes |
| --- | --- |
| `EMAIL_ENABLED` | Off by default. Off means `enqueue` is a no-op |
| `EMAIL_PROVIDER` | `resend` or `fake` |
| `RESEND_API_KEY` | Required when the provider is `resend`. **The worker needs it; the API does not** |
| `RESEND_WEBHOOK_SECRET` | Unset means the webhook refuses every delivery |
| `EMAIL_FROM` | Must be on a verified sending domain |
| `EMAIL_REPLY_TO` | Optional |
| `APP_PUBLIC_URL` | The origin every emailed link is built from |
| `EMAIL_MAX_ATTEMPTS` | Default 8 |
| `EMAIL_WORKER_POLL_SECONDS` | Default 10 |

Configuration **fails closed**. With `EMAIL_ENABLED=true` outside the test
suite, startup requires:

- `EMAIL_FROM`, and that it is a bare address — `no-reply@example.com`, not
  `Wasla <no-reply@example.com>`. A sender the provider rejects is a
  *permanent* failure on every row, so a typo in this one value silently
  discards every email the deployment ever queues.
- `APP_PUBLIC_URL`, on an `http`/`https` scheme with a host and no query or
  fragment. The scheme is an allowlist rather than a prefix check because
  whatever this holds is prefixed onto every link a recipient clicks, and
  `javascript:` is a link too.

In production it additionally refuses the fake provider and requires
`APP_PUBLIC_URL` to be HTTPS.

`RESEND_WEBHOOK_SECRET` is required in production too, but by the **API
process** rather than by `Settings` — `require_delivery_verification`, called
from `create_app`. The requirement is the same strength and the reason is
least privilege: this validator runs in every process, so demanding the secret
here obliged the worker to carry one it never reads ([ADR-063](../DECISIONS.md)).
Without it the delivery endpoint answers 503 to every bounce, so no suppression
is ever recorded and the platform keeps writing to dead mailboxes until the
sending domain is the thing that fails.

A misconfigured deployment does not boot, rather than booting and silently
not sending password resets.

**`EMAIL_ENABLED=false` is a real operational state, not a safe default to
leave in production.** Enqueue becomes a no-op, so password reset accepts the
request, answers 202 and does nothing at all. That is deliberate — a
deployment that has not configured a sender should not accumulate rows — but
it means turning email off silently disables account recovery.

The key is never a Docker build argument and is not in any image layer. It
is never in a frontend bundle, and no endpoint - `/health` included -
returns it.

## Operating

Queue depth and the oldest pending message are the two numbers worth an
alert:

```sql
SELECT status, count(*), min(created_at) AS oldest
FROM email_messages
WHERE status IN ('pending', 'sending')
GROUP BY status;
```

A growing `pending` count with a stable `sent` count means the worker is not
running or the provider is rejecting everything - check `email.failed`
events for the error category. Rows in `sending` older than ten minutes are
recovered automatically; if they persist, no worker is alive.

Retrying a `failed` row is deliberately not an API operation. It would be a
way to make the platform re-send mail on demand, and the reason a row failed
is usually that the address does not work.

## What is not built

Honest list. Do not read the sections above as a claim that every message
is wired.

- **Templates absent from the enum:** invitation accepted, membership
  revoked, role change, subscription started, trial *ending* (as opposed to
  expired), payment succeeded, payment failed, payment pending. Each needs a
  domain event that does not exist yet; none was invented to give a template
  a caller.
- ~~No email verification flow~~ - **built** (ADR-043). Six-digit codes
  under `POST /auth/email/verification/{send,verify}`, with the same
  hash-only, single-use, superseded-on-reissue handling as reset, plus an
  attempt cap the reset token does not need. It still grants nothing:
  workspace access comes from a membership row and platform authority from
  `platform_role`, exactly as before. See `docs/EMAIL_VERIFICATION.md`.
- **Redis degradation of the reset rate limiter is untested.** The limiter
  itself degrades rather than disappears (ADR-040) and is covered there; what
  is not covered is that path specifically through the reset endpoints.
- ~~Real provider delivery has never been observed~~ - **observed on
  2026-08-27.** One `EMAIL_VERIFICATION` message was handed to Resend with a
  real `RESEND_API_KEY`, from `onboarding@resend.dev` to a real mailbox. The
  provider accepted it, the row moved to `sent` with a
  `provider_message_id`, Resend's own API reported the message `delivered`,
  the six digits in the delivered body matched the challenge that issued
  them, and that code then verified the account over HTTP. The send path is
  no longer a claim about code.

  Three things were confirmed by that send rather than argued: the outbox
  `context` was `{}` afterwards, so the plaintext left the database on the
  terminal transition exactly as this document says; the API key appeared in
  no log line the worker wrote; and the code appeared in none either.
- **The delivery webhook has still never fired.** No event has arrived from
  Resend's infrastructure, because that needs a publicly reachable URL this
  environment does not have. Suppression, bounce and complaint handling
  remain exercised only against synthesised, correctly-signed payloads -
  `delivered` above was read from Resend's API, not received from it. The
  *Operating* checklist still has to be walked for that half.
