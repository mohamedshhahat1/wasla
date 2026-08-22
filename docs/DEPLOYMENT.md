# Deployment

**Status: In Progress** — local Docker, Compose, and CI are part of Phase 0; deployment automation is Planned.

Scope: containers, reverse proxy, CI/CD, and production operations. Runtime instructions for developers live in [../README.md](../README.md).

## Containers

### The worker

`docker compose up worker` locally; `command: ["worker"]` in production. One process runs the media, agent, ingestion and follow-up loops concurrently — all are I/O-bound, so they interleave rather than compete.

`WORKER_KINDS` selects which run: empty (the default) runs all four, or a comma-separated subset such as `media` or `ingestion,follow_up` to scale them apart across replicas — reading files is bandwidth-bound where inference is not. An unrecognised name fails at startup rather than being ignored, because a process that silently does nothing shows its symptom — work piling up in a queue — far from its cause.

The worker never applies migrations, whatever `RUN_MIGRATIONS` says: it scales to several replicas, and a schema change racing across them is exactly what the opt-in flag on the API exists to avoid. Run `migrate` as its own step first; the production compose does this with `service_completed_successfully`.

**The worker has no health check, deliberately.** The image's `HEALTHCHECK` curls the API's liveness endpoint, and this process serves no HTTP — so inheriting it reported a perfectly healthy worker as unhealthy for as long as it ran. Both compose files disable it explicitly (`healthcheck: disable: true`) rather than leave a check that always fails: a permanently red health column makes `docker ps` lie, hangs anything waiting on `service_healthy`, and teaches an operator to ignore the one signal that is supposed to mean something. Judge a worker by its logs — each loop announces `*.worker_started` — until a real probe exists, which needs the loops to publish a heartbeat.

SIGTERM asks each loop to stop, and each finishes the job in its hand before exiting. The production `stop_grace_period` is 60s, longer than the API's, because a worker mid-inference holds an outstanding HTTP call and an open transaction — killing it there dead-letters a job that was about to succeed.

One image, three entrypoint commands: `api` (default), `worker`, and `migrate`. Services: the FastAPI API, the worker, PostgreSQL with pgvector, Redis, and Nginx in production. Production requirements: non-root containers, health checks, environment-based configuration, graceful shutdown, restart policies, isolated networks, and persistent database volumes. Secrets are never baked into images.

### The media volume

The API and the worker share a volume at `MEDIA_STORAGE_PATH`: the worker downloads a customer's attachment and writes it there, and the API serves it back from there. Both compose files configure this.

**This is a single-host arrangement.** With the API and worker on different machines the volume is not shared, downloads silently become unreadable to the API, and the fix is the object-store implementation behind the `MediaStorage` interface rather than a configuration change ([ADR-023](../DECISIONS.md), [MEDIA.md](MEDIA.md)). Nothing sweeps the volume, so plan capacity for the full retention period.

## Compose

`docker-compose.yml` targets local development with reload and mounted source. `docker-compose.prod.yml` targets production with pinned images, no source mounts, and stricter resource and restart policies.

## Reverse proxy

An Nginx example covers reverse proxying, HTTPS termination structure, request size limits, security headers, client IP forwarding, proxy timeouts, and WebSocket upgrade support. TLS certificates are not included; production certificate issuance and renewal must be configured by the operator.

## CI/CD

Planned GitHub Actions workflows:

| Workflow | Responsibility | Status |
| --- | --- | --- |
| `ci.yml` | Install, format check, Ruff, MyPy, tests, migration validation, build check | In Progress |
| `security.yml` | Dependency and secret scanning | Planned |
| `deploy.yml` | Build image, push to registry, deploy | Planned |

CI is deterministic with controlled dependency versions. Deployment never proceeds when tests fail. All credentials come from GitHub Actions secrets.

## Operations

Migrations run as an explicit step before the new application version serves traffic; schema is never mutated manually. Readiness gates traffic on dependency availability while liveness stays independent of PostgreSQL. Health, logging, and observability details are in [../ARCHITECTURE.md](../ARCHITECTURE.md).
