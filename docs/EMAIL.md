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
suite, startup requires `EMAIL_FROM` and `APP_PUBLIC_URL`; in production it
additionally refuses the fake provider and requires `APP_PUBLIC_URL` to be
HTTPS. A misconfigured deployment does not boot, rather than booting and
silently not sending password resets.

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

- **Billing emails are queued by nothing.** `INVOICE_ISSUED`,
  `TRIAL_EXPIRED` and `SUBSCRIPTION_CANCELLED` exist as templates with no
  caller; the invoice and subscription services do not enqueue. Wiring them
  needs `invoice:{invoice_id}` and `subscription:{subscription_id}:{state}`
  keys and a decision about which recipients in a workspace get billing mail.
- **Templates absent from the enum:** invitation accepted, membership
  revoked, role change, subscription started, trial *ending* (as opposed to
  expired), payment succeeded, payment failed, payment pending.
- **No email verification flow**, and this is a decision rather than an
  omission. Nothing in Wasla's authorization model grants anything on the
  basis of a verified address: workspace access comes from a membership row,
  platform authority from `platform_role`, and an invitation already proves
  control of the address by requiring a token delivered to it. Adding
  verification now would introduce an account state with no authorization
  meaning. It becomes necessary if self-service registration ever grants
  something before an invitation - at which point it belongs behind
  `POST /auth/email-verification/{request,confirm}` with the same hashed
  single-use token handling as reset.
- **Tests:** the provider, outbox, worker, password reset and webhook
  handling have unit coverage of the signature scheme and the event trust
  boundary. There are **no** integration tests against PostgreSQL, no Redis
  degradation tests for the reset limiter, no concurrent-worker test, no
  transaction-rollback test and no container-level HTTP test for the webhook
  route.
- **Real provider delivery has never been observed** from this repository.
  Everything above describes code, not a delivered message.
