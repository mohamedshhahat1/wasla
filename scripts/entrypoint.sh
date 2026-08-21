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
    exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac
