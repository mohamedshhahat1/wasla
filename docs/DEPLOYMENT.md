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
