#!/bin/sh
# Container entrypoint.
#
# Migrations are opt-in via RUN_MIGRATIONS so that a deployment runs them once
# as an explicit step instead of racing across replicas.
set -eu

command="${1:-api}"

case "${command}" in
  api)
    if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
      echo "entrypoint: applying database migrations"
      alembic upgrade head
    fi
    # Reload is a development convenience and is opt-in through the
    # environment rather than through the command, because overriding the
    # command bypasses this branch entirely - migrations included. That is
    # exactly the bug this flag exists to make impossible.
    if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
      exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --reload
    fi
    exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
    ;;
  worker)
    # Never runs migrations, whatever RUN_MIGRATIONS says. Workers scale to
    # several replicas, and a schema change racing across them is precisely
    # what the opt-in flag on the API exists to avoid.
    exec python -m app.workers.runner
    ;;
  worker-health)
    # The worker serves no HTTP, so its health check is a command rather than a
    # request: it asks Redis whether every loop this container is configured to
    # run has beaten recently. Exits non-zero when one has not.
    exec python -m app.workers.health
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  queues)
    # The operator's view of the queues: depths, dead-letter records, and the
    # replay path back out of one. Deliberately a command rather than an HTTP
    # endpoint, because replaying an agent job sends somebody's customer a
    # message (ADR-071).
    shift || true
    exec python -m app.workers.queues "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
