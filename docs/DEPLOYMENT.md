# Deployment

**Status: In Progress** — local Docker, Compose, and CI are part of Phase 0; deployment automation is Planned.

Scope: containers, reverse proxy, CI/CD, and production operations. Runtime instructions for developers live in [../README.md](../README.md).

## Containers

Planned images and services: the FastAPI API, background workers, PostgreSQL with pgvector, Redis, and optionally Nginx. Production requirements: non-root containers, health checks, environment-based configuration, graceful shutdown, restart policies, isolated networks, and persistent database volumes. Secrets are never baked into images.

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
