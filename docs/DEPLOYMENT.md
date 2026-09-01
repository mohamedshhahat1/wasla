# Deployment

**Status: Implemented** — CI, security scanning, image publishing to GHCR and a deploy workflow all exist. What has *not* happened is a real production deployment: every procedure here has been executed against local containers and real PostgreSQL, and none has been run against customer traffic. [RUNBOOK.md](RUNBOOK.md) says the same about operating it.

Scope: containers, reverse proxy, CI/CD, and production operations. Runtime instructions for developers live in [../README.md](../README.md).

## Containers

### The worker

`docker compose up worker` locally; `command: ["worker"]` in production. One process runs the media, agent, ingestion, follow-up and campaign loops concurrently — all are I/O-bound, so they interleave rather than compete.

`WORKER_KINDS` selects which run: empty (the default) runs all six, or a comma-separated subset such as `campaign` or `ingestion,follow_up` to scale them apart across replicas — moving bytes is bandwidth-bound where inference is not, and a workspace mid-broadcast is the case that most often wants a replica of its own. An unrecognised name fails at startup rather than being ignored, because a process that silently does nothing shows its symptom — work piling up in a queue — far from its cause.

**A kind no running container covers is a queue that grows silently.** The health check below asserts only the loops *that container* was told to run, so splitting them apart means checking that every kind is still covered somewhere.

The worker never applies migrations, whatever `RUN_MIGRATIONS` says: it scales to several replicas, and a schema change racing across them is exactly what the opt-in flag on the API exists to avoid. Run `migrate` as its own step first; the production compose does this with `service_completed_successfully`.

**The worker has a health check of its own** (`scripts/entrypoint.sh worker-health`), replacing the API liveness curl it used to inherit and could never answer. Each configured loop publishes a heartbeat to Redis with a 90-second expiry, refreshed every 30 seconds, and the probe exits non-zero unless *every* loop this container runs has beaten recently.

What it proves is that the process is up and its event loop is scheduling — the beat is an ordinary task, so a crash, a hang or a blocking call in async code stops it. What it does **not** prove is that a loop is making progress: a worker waiting on a query that never returns keeps beating. Judge that by queue depth ([RUNBOOK.md](RUNBOOK.md)), not by the health column.

SIGTERM asks each loop to stop, and each finishes the job in its hand before exiting. The production `stop_grace_period` is 60s, longer than the API's, because a worker mid-inference holds an outstanding HTTP call and an open transaction — killing it there dead-letters a job that was about to succeed.

One image, three entrypoint commands: `api` (default), `worker`, and `migrate`. Services: the FastAPI API, the worker, PostgreSQL with pgvector, Redis, and Nginx in production. Production requirements: non-root containers, health checks, environment-based configuration, graceful shutdown, restart policies, isolated networks, and persistent database volumes. Secrets are never baked into images.

### The media volume

The API and the worker share a volume at `MEDIA_STORAGE_PATH`: the worker downloads a customer's attachment and writes it there, and the API serves it back from there. Both compose files configure this.

**This is a single-host arrangement.** With the API and worker on different machines the volume is not shared, downloads silently become unreadable to the API, and the fix is the object-store implementation behind the `MediaStorage` interface rather than a configuration change ([ADR-023](../DECISIONS.md), [MEDIA.md](MEDIA.md)). Nothing sweeps the volume, so plan capacity for the full retention period.

## Compose

`docker-compose.yml` targets local development with reload and mounted source. `docker-compose.prod.yml` targets production with pinned images, no source mounts, and stricter resource and restart policies.

Every secret in the production file is required and interpolated from the deployment environment — compose fails to start rather than falling back to an insecure default. The settings added in phases 13 and 14 are wired through it explicitly, with defaults chosen so that omitting them is a *specific* outcome rather than a vague one:

| Variable | Omitted means |
| --- | --- |
| `REDIS_PASSWORD` | **Compose refuses to start.** Required, not optional: Redis holds the spent-refresh-token denylist, so anything able to write to it can delete a key and turn a revoked token back into a live one — undoing a logout, a sign-out-everywhere (ADR-036) and the reuse teardown (ADR-039). It also holds the agent queue, so write access is enough to make a worker send messages of somebody's choosing. Put the same value in `REDIS_URL` as `redis://:PASSWORD@redis:6379/0` |
| `CREDENTIAL_ENCRYPTION_KEYS` | Every workspace sends through `META_ACCESS_TOKEN`; a workspace trying to store its own credential is refused rather than having it stored in the clear. **Connecting a number still works** — ownership is proven with the supplied token and the token is then discarded (ADR-037) |
| `DEFAULT_PLAN_CODE` | New workspaces start on `starter` |
| `RATE_LIMIT_*` | Limiting is on, at the application defaults |
| `MAX_REQUEST_BYTES`, `REQUEST_TIMEOUT_SECONDS` | 32 MB and 60 seconds |

`REDIS_PASSWORD` is consumed twice on purpose: as the server's `--requirepass` and as the credential inside `REDIS_URL`. The healthcheck authenticates too, so a mismatch fails the check rather than reporting a healthy server that refuses every real client.

`CREDENTIAL_ENCRYPTION_KEYS` **must be identical for the API and the worker.** The worker decrypts a workspace's credential in order to send with it, so a worker missing a key the API used cannot send for that workspace at all.

## Reverse proxy and TLS

`nginx/nginx.conf` covers reverse proxying, request size limits, security headers, client IP forwarding, proxy timeouts and WebSocket upgrade support. It has been validated with `nginx -t` and exercised end to end: the HTTP listener redirects, the proxy passes traffic, an oversized body is refused at the proxy, and `X-Forwarded-For` reaches the application.

**TLS is not included and cannot be.** Certificates are issued to a domain by a certificate authority; nothing in this repository knows the domain. A self-signed certificate shipped here would be worse than none — it would look like TLS while failing every client that checks.

What ships is the structure around it. The HTTP listener on port 80 serves the ACME challenge and redirects everything else to HTTPS. The proxy's own listener is on **8443 and speaks plain HTTP** until the commented TLS block is enabled, which is why the production compose publishes it on loopback only rather than as 443.

### Issuing the first certificate

The ordering matters, and getting it wrong produces a deadlock that looks like a broken proxy: a redirect to HTTPS cannot be verified while the certificate that would serve HTTPS does not exist. That is why the ACME location is matched with `^~` — it is served *before* the redirect.

```bash
# 1. Bring up the stack. Port 80 must be reachable from the internet.
docker compose -f docker-compose.prod.yml up -d

# 2. Issue, using the webroot nginx already serves.
docker run --rm   -v /etc/letsencrypt:/etc/letsencrypt   -v ./certbot/www:/var/www/certbot   certbot/certbot certonly --webroot     --webroot-path=/var/www/certbot     -d api.example.com     --email ops@example.com --agree-tos --no-eff-email

# 3. Enable TLS: uncomment the `listen 443 ssl` block in nginx/nginx.conf,
#    set server_name and the certificate paths, uncomment the HSTS header,
#    and change the published port to "443:443".

# 4. Check the configuration parses BEFORE reloading. A bad path here means
#    nginx refuses to start, and it is currently the only thing serving.
docker compose -f docker-compose.prod.yml exec nginx nginx -t
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

### Renewal

Certbot certificates last 90 days. Renewal must be automatic and it must reload nginx — a renewed certificate on disk that nginx has not re-read is an expired certificate as far as every client is concerned.

```
0 3 * * * docker run --rm -v /etc/letsencrypt:/etc/letsencrypt     -v /opt/wasla/certbot/www:/var/www/certbot certbot/certbot renew --quiet &&     docker compose -f /opt/wasla/docker-compose.prod.yml exec -T nginx nginx -s reload
```

Set a calendar reminder for the first expiry regardless. Renewal automation failing silently is the standard way a site goes down on a Sunday.

### If TLS terminates upstream

A load balancer or ingress terminating TLS should leave the redirect in `nginx.conf` commented out and **must** set `X-Forwarded-Proto`. Without it the application believes every request arrived in plaintext. It must also set `X-Forwarded-For`: the authentication rate limiter counts by client address, and without it every user shares one identity and one budget ([ADR-032](../DECISIONS.md)).

## What the shipped Compose file passes to which process

`docker-compose.prod.yml` enumerates its environment explicitly rather than
forwarding a `.env`, so nothing reaches a container by accident. That
enumeration is now held against `Settings` by
`tests/integration/test_deployment_configuration.py`, which fails CI when a
setting a feature needs is not wired in ([ADR-062](../DECISIONS.md)) - the file
had gone five phases without Google, email or Paymob, and the stack came up
perfectly while none of them could be switched on.

Two rules shape it:

**Every feature setting is `${VAR:-}`, never `${VAR:?}`.** The infrastructure a
deployment cannot run without - the image, the database URL, `JWT_SECRET`,
`META_APP_SECRET` - stays mandatory at interpolation. A feature nobody enabled
is different: refusing to bring the stack up over an absent Google client secret
would make an optional integration compulsory. What refuses a *half* configured
feature is the application's own validator, which knows which combinations are
coherent.

**Each process gets only what it reads.** The table below groups the settings
the four optional integrations need; it is a summary, and the authoritative
list is `FEATURE_SETTINGS` in the drift guard, which expands each prefix
against `Settings.model_fields` rather than restating a count that would rot.
The guard enforces the dashes as well as the ticks - a secret injected into a
process that does not read it fails CI the same way a missing one does
([ADR-063](../DECISIONS.md)).

| Setting group | API | Worker | Why |
| --- | :-: | :-: | --- |
| `APP_PUBLIC_URL` | ✓ | ✓ | The API builds the Paymob callback URL from it; the worker builds emailed links |
| `EMAIL_ENABLED`, `EMAIL_PROVIDER`, `EMAIL_FROM`, `EMAIL_REPLY_TO` | ✓ | ✓ | The API writes outbox rows, the worker renders and sends |
| `RESEND_API_KEY` | — | ✓ | The API never sends. The credential lives in one container |
| `RESEND_WEBHOOK_SECRET` | ✓ | — | Only the API serves the delivery webhook, so only the API can verify one ([ADR-063](../DECISIONS.md)) |
| `EMAIL_VERIFICATION_*` | ✓ | — | Challenges are issued on the request path |
| `EMAIL_MAX_ATTEMPTS`, `EMAIL_WORKER_POLL_SECONDS` | — | ✓ | Delivery retries and the poll interval belong to the email worker |
| `GOOGLE_*` | ✓ | — | Nothing in the worker touches OIDC, so the client secret reaches exactly one container |
| `BILLING_PROVIDER`, `PAYMOB_*` | ✓ | ✓ | The API creates intentions and verifies callbacks; the worker collects renewals |
| `BILLING_PAST_DUE_DAYS`, `BILLING_SUSPEND_AFTER_DAYS` | ✓ | ✓ | The worker acts on them; the API carries them so the ordering rule is validated by whichever process starts first |

`docker-compose.yml` is not held to this: it forwards a developer's whole `.env`
through `env_file` and therefore cannot drift.

## Dunning

A renewal invoice that goes unpaid moves the workspace through two states, both
measured in days from the moment the invoice was **issued**
([ADR-061](../DECISIONS.md)):

```
ACTIVE ──BILLING_PAST_DUE_DAYS──▶ PAST_DUE ──BILLING_SUSPEND_AFTER_DAYS──▶ SUSPENDED
   ▲                                  │                                        │
   └────────────── a settled payment lifts either ───────────────────────────┘
```

`PAST_DUE` still serves: a failed card is a conversation to have, not a
disconnection. `SUSPENDED` does not - the paid plan stops resolving and the
workspace falls back to `DEFAULT_PLAN_CODE`, so it keeps a usable free tier
rather than being locked out. Nothing is deleted, no further invoice is raised
and no saved card is charged.

Paying the outstanding invoice restores service. A **cancelled** or **expired**
subscription is not revived by a payment, deliberately: those are decisions
somebody made.

Defaults are 7 days and 30. The second must be strictly greater than the first
or the application refuses to start, in every environment - suspending a
workspace in the same sweep that first tells it anything is the opposite of a
grace period.

## Payments

Off unless `BILLING_PROVIDER=paymob`. With it set, all of
`PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_HMAC_SECRET`,
`PAYMOB_INTEGRATION_IDS` and `APP_PUBLIC_URL` are required and the application
refuses to start without them — in staging as well as production, because a
deployment taking real payments with no HMAC secret answers 503 to every
callback while transactions complete at Paymob.

**1. Get the credentials.** Paymob dashboard → Settings → Account Info for the
secret and public keys, and the HMAC secret. Integration ids are per payment
method under Payment Integrations. Test and live keys are different values and
must be used with matching integration ids; the base URL is the same either way.

The keys carry their mode in them — `sk_test_…` / `sk_live_…` and
`pk_test_…` / `pk_live_…` — and the application refuses to start if the two
disagree. That pairing is worth understanding rather than working around: a
live secret key with a test public key creates a *real* intention and sends the
customer to a *test* payment page, so nothing is ever collected and every
callback is for money that does not exist. Both halves look perfectly valid on
their own, which is why it is checked at boot. Test keys in production are
refused for the mirror-image reason.

**2. Register the callback URL.** Set the integration's *transaction processed*
callback to:

```
https://<your-host>/api/v1/webhooks/paymob
```

It must be reachable from the internet and must **not** sit behind the proxy's
auth or IP allowlist — it authenticates itself with an HMAC, and a proxy that
blocks it turns every payment into a charge nobody records. Like the other two
webhooks it is exempt from rate limiting: a 429 there loses a payment
notification.

**3. Set the response callback** (where the customer's browser lands) to a page
in your frontend. It is for showing a result and is not trusted for anything.

**4. Test before going live.** With test keys, the cards Paymob publishes work
against the same URLs. Confirm a `payment_events` row appears with
`outcome = applied` and the invoice moves to `paid`.

Worth doing in the same sitting, because each exercises a path nothing else
does:

- **Send the same callback twice** (Paymob's webhook testing tool, or replay
  the request). The second must be `duplicate` and `invoices.amount_paid` must
  not move.
- **Refund the test payment** through `POST /billing/payments/{id}/refund`.
  The response is `202` and says `refund_pending`; the payment only becomes
  `refunded` when Paymob's callback arrives, which is the thing this step is
  really testing.
- **Leave a renewal unpaid** past `GRACE_DAYS` if you can move the clock, and
  confirm the subscription becomes `past_due` while still serving.

**5. Recurring card debits are not part of this.** Wasla renews on its own
calendar and emails an invoice; the customer pays it through a checkout. There
is no card on file, so nothing is charged without somebody choosing to pay.
Automatic debits would need a MOTO integration id enabled by Paymob for the
merchant and an API key for their older auth-token flow — see
[docs/BILLING.md](BILLING.md) for why that is a decision rather than an
oversight.

## Email sending domain

Email is off unless `EMAIL_ENABLED=true`, and turning it on without the DNS
below produces a deployment that boots, queues rows, and has every one of
them permanently rejected. Do the DNS first.

**1. Verify the sending domain with Resend.** In the Resend dashboard, add
the domain that `EMAIL_FROM` sits on and publish the records it gives you:

- **DKIM** — a `TXT` (or `CNAME`, depending on what Resend issues) on a
  selector subdomain. This is what signs the mail; without it, delivery to
  most large mailbox providers fails or lands in spam.
- **SPF** — a `TXT` on the sending domain authorising Resend's senders. If
  the domain already has an SPF record, **merge** into it. Two SPF records on
  one domain is a permanent error, not two policies.
- **MX** — only if Resend asks for one on the sending subdomain.

Wait for the dashboard to report the domain verified before enabling email.

**2. Publish a DMARC policy.** A `TXT` at `_dmarc.<domain>`. Start at
`p=none` with a reporting address, read the reports for a week or two, and
only then tighten to `p=quarantine` and `p=reject`. Going straight to
`p=reject` before SPF and DKIM are confirmed aligned bounces your own mail.

**3. Prefer a subdomain for transactional mail** — `mail.example.com` or
similar. Sender reputation is per-domain, so keeping product mail off the
apex protects everything else that domain sends.

### Email verification settings

Two knobs, both optional and both bounded (ADR-043). They are validated by the
settings field rather than only in production, so an out-of-range value refuses
to boot in staging and on a developer's laptop too.

| Variable | Default | Bounds | What a wrong value costs |
| --- | --- | --- | --- |
| `EMAIL_VERIFICATION_TTL_SECONDS` | `600` | 60-3600 | Too low and codes expire while somebody opens their mail client. Too high and a six-digit code becomes a long-lived password with twenty bits of entropy |
| `EMAIL_VERIFICATION_MAX_ATTEMPTS` | `5` | 1-10 | Too low and one mistype burns the challenge. Too high and a million-value keyspace starts being worth searching |

Values outside the bounds are **refused, not clamped**: a deployment that
silently corrected them is a deployment whose operator believes something false
about how long a code lives.

## Google sign-in

Off by default, and off means the five Google routes answer `404` — a feature
nobody configured does not exist in this deployment. Turning it on makes the
other three mandatory at startup, because half-configured Google sign-in is a
button that always fails with the only error message on Google's domain.

| Variable | Secret | Purpose |
| --- | --- | --- |
| `GOOGLE_ENABLED` | no | `false` by default. `true` makes the three below mandatory |
| `GOOGLE_CLIENT_ID` | no | From Google Cloud Console → Credentials → OAuth 2.0 Client ID, type *Web application*. Startup refuses a value not ending in `.apps.googleusercontent.com`, which catches the secret being pasted into this field |
| `GOOGLE_CLIENT_SECRET` | **yes** | Used in exactly one place: the server-to-server code exchange |
| `GOOGLE_REDIRECT_URI` | no | A **frontend** route, not an API one. Google exact-matches it against the console — scheme, host, port and path, trailing slash included. Must be `https` in production, because a single-use authorization code arrives in its query string |

`GOOGLE_CLIENT_SECRET` belongs to the API process only: never a Docker build
argument, never in an image layer, never in a frontend bundle, returned by no
endpoint including `/health`, and never logged. Google permits two secrets
briefly, so rotate by adding the new one, deploying, then deleting the old one
in the console.

**Google sign-in requires Redis.** The state, nonce and PKCE verifier for an
in-flight authorization live there and nowhere else, so a Redis outage makes
Google sign-in *unavailable* rather than unverified (ADR-051). Password login is
unaffected. This is deliberately the opposite of the rate limiter's fail-open
posture: a degraded limiter still slows an attacker, whereas a process-local
approximation of a single-use replay control is not weaker but absent.

Verify a deployment by requesting `POST /api/v1/auth/google/authorize`. A `404`
means the feature is off or half-configured; a `200` carrying an
`authorization_url` on `accounts.google.com` means the configuration loaded.

Verification needs no DNS or provider configuration of its own — it sends
through the same outbox, worker and Resend adapter as everything else. With
`EMAIL_ENABLED=false` no code is ever delivered, and the endpoints still answer
`202`, so a deployment that leaves email off has verification that nobody can
complete. That is the same no-op the password reset has, and for the same
reason.

**4. Configure the delivery webhook.** In Resend, add an endpoint pointing at
`https://<your-host>/api/v1/webhooks/email` and subscribe it to at least
`email.delivered`, `email.bounced` and `email.complained`. Copy the signing
secret it issues — it starts with `whsec_` — into `RESEND_WEBHOOK_SECRET`.

The route is unauthenticated by necessity and defends itself with the Svix
HMAC, so it must be reachable from the internet and must **not** be behind
the proxy's auth or IP allowlist. It is exempt from rate limiting and from
the request timeout for the same reason the Meta webhook is: a non-2xx makes
a provider retry and eventually disable the endpoint.

**5. Split the credentials by process.** `RESEND_API_KEY` is needed by the
**worker** only — the API never talks to a provider. `RESEND_WEBHOOK_SECRET`
is needed by the **API** only. Giving each container only the one it uses
means a compromised API container holds no sending credential.

**6. Run the email worker.** It is one kind among the existing set, so
`WORKER_KINDS` must include `email` (the default set includes it). A
deployment with email enabled and no email worker queues rows nobody
delivers.

Verify before announcing anything: register a throwaway workspace, request a
password reset for it, and confirm the message arrives, that its link opens
your `APP_PUBLIC_URL`, and that the row reaches `delivered` — which only
happens if the webhook is wired correctly.

## CI/CD

| Workflow | Responsibility | Status |
| --- | --- | --- |
| `ci.yml` | Ruff, Black, MyPy, the full suite with coverage, application start-up, migrations up/down/up, `alembic check`, image build and container liveness | Implemented |
| `security.yml` | Dependency audit (pip-audit), secret scan over full history (gitleaks), container scan (Trivy) | Implemented |
| `deploy.yml` | Build, scan, publish to GHCR, deploy, verify readiness | Implemented |

### How a release reaches production

```
push to main → ci.yml (quality, tests, migrations, image)
                  │  conclusion == success
                  ▼
            deploy.yml publish  → build → push to ghcr.io → Trivy scan
                  │  image addressed by digest
                  ▼
            deploy.yml deploy   → migrate → up --wait → /health/ready
```

**Deployment is gated on CI rather than repeating it.** `deploy.yml` triggers on `workflow_run` and refuses any conclusion other than success, which is "do not deploy if tests fail" expressed as a dependency instead of a second copy of the test job that could drift from the first.

**It checks out the commit CI verified**, not the branch head. Between CI finishing and deployment starting, `main` may have moved, and publishing the newer commit would ship something no test ever saw.

**Images are addressed by digest.** `sha-<commit>` names exactly one build and never moves; `main` and `latest` exist for people. The deploy step passes a digest, so what is deployed is bit-for-bit what was built and scanned.

**The image is scanned after it is pushed, not before.** A failing scan must not leave `main` with nothing published — the image exists, the finding is visible, and the deploy job refuses to ship it. Blocking the push instead would mean a critical CVE in a base image also destroys the ability to roll forward with a fix.

### What deployment needs configured

Publishing needs nothing: `GITHUB_TOKEN` is the registry login, so there is no separate credential to rotate or leak. Deployment needs these, and the job **fails loudly** when they are absent rather than reporting a success that touched no server:

| Secret | Purpose |
| --- | --- |
| `DEPLOY_HOST` | Host to deploy to |
| `DEPLOY_USER` | SSH user |
| `DEPLOY_SSH_KEY` | Private key for that user |
| `DEPLOY_KNOWN_HOSTS` | The host's public key, pinned — trusting an unknown host on first connection is how a deploy ends up talking to somebody else's server |

| Variable | Purpose |
| --- | --- |
| `DEPLOY_PATH` | Where the compose file lives on the host (default `/opt/wasla`) |
| `WASLA_PUBLIC_URL` | Shown on the GitHub environment |

The `production` environment is where an approval gate belongs. Everything else the stack needs — database credentials, `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEYS` — lives on the deployment host, not in GitHub: the workflow ships a compose file and an image reference, never a secret.

### Rolling back

Deploy the previous digest. The procedure, and why a rollback must not run migrations, is in [RUNBOOK.md](RUNBOOK.md).

## Operations

Migrations run as an explicit step before the new application version serves traffic; schema is never mutated manually. Readiness gates traffic on dependency availability while liveness stays independent of PostgreSQL. Health, logging, and observability details are in [../ARCHITECTURE.md](../ARCHITECTURE.md).

**[RUNBOOK.md](RUNBOOK.md) is the operational document**: what to check first, what each symptom means, how to roll back, how to rotate each secret, and — stated explicitly — what it cannot tell you, including that there is no backup system yet.
