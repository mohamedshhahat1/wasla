#!/bin/sh
# Point-in-time recovery drill, on throwaway containers (DB-009).
#
#   sh scripts/pitr_drill.sh
#
# Proves the mechanism docs/BACKUP.md "Point-in-time recovery" describes -
# continuous WAL archiving plus a base backup, replayed to a chosen moment -
# against the same PostgreSQL image production runs, with the schema at the
# repository's migration head. It proves nothing about a deployment's archive:
# where the WAL goes, whether it is off-host, how long it is kept. Those are
# the deployment's, and docs/BACKUP.md says so.
#
# Synthetic data only. Everything it creates is named wasla-pitr-drill-* and
# removed at the end, pass or fail. Needs docker, and alembic on PATH for the
# migration step.
set -eu

IMAGE="pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
PREFIX="wasla-pitr-drill"
PORT="${PITR_DRILL_PORT:-55499}"
PASSWORD="pitr-drill-synthetic"
export MSYS_NO_PATHCONV=1

log() { echo "$(date -u +%H:%M:%S) pitr-drill: $*"; }
fail() { log "FAILED: $*"; exit 1; }

cleanup() {
    docker rm -f "${PREFIX}-source" "${PREFIX}-recovered" >/dev/null 2>&1 || true
    docker volume rm -f "${PREFIX}-archive" "${PREFIX}-base" "${PREFIX}-recovered" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

sql() {
    docker exec -e PGPASSWORD="${PASSWORD}" "$1" \
        psql -U drill -d wasla -v ON_ERROR_STOP=1 -qAt -c "$2"
}

wait_ready() {
    for _ in $(seq 1 60); do
        if docker exec "$1" pg_isready -U drill -d wasla >/dev/null 2>&1 \
            && sql "$1" "SELECT NOT pg_is_in_recovery()" 2>/dev/null | grep -q t; then
            return 0
        fi
        sleep 1
    done
    docker logs "$1" 2>&1 | tail -30
    fail "$1 did not become ready"
}

# ------------------------------------------------------------ the source
log "starting a source server with WAL archiving on"
docker volume create "${PREFIX}-archive" >/dev/null
docker run -d --name "${PREFIX}-source" \
    -e POSTGRES_USER=drill -e POSTGRES_PASSWORD="${PASSWORD}" -e POSTGRES_DB=wasla \
    -v "${PREFIX}-archive:/archive" -p "127.0.0.1:${PORT}:5432" \
    "${IMAGE}" \
    -c wal_level=replica -c archive_mode=on -c archive_timeout=60 \
    -c "archive_command=test ! -f /archive/%f && cp %p /archive/%f" >/dev/null
docker exec "${PREFIX}-source" chown postgres /archive
wait_ready "${PREFIX}-source"

sql "${PREFIX}-source" "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto"
log "migrating the source to head"
MIGRATION_DATABASE_URL="postgresql+asyncpg://drill:${PASSWORD}@127.0.0.1:${PORT}/wasla" \
    ENVIRONMENT=test alembic upgrade head >/dev/null 2>&1 || fail "alembic upgrade head failed"
head="$(sql "${PREFIX}-source" "SELECT version_num FROM alembic_version")"
log "source at migration head ${head}"

tenant() {
    sql "${PREFIX}-source" "INSERT INTO tenants (id, name, slug, status) VALUES (gen_random_uuid(), '$1', '$1', 'active')"
}

tenant pitr-a
log "data A written; taking a base backup"
docker volume create "${PREFIX}-base" >/dev/null
docker exec "${PREFIX}-source" sh -c "mkdir -p /base && chown postgres /base"
docker run --rm --network "container:${PREFIX}-source" -e PGPASSWORD="${PASSWORD}" \
    -v "${PREFIX}-base:/base" "${IMAGE}" \
    pg_basebackup -h 127.0.0.1 -U drill -D /base/data -X stream -c fast >/dev/null

tenant pitr-b
target="$(sql "${PREFIX}-source" "SELECT clock_timestamp()")"
log "data B written; recovery target is ${target}"
sleep 2

tenant pitr-c
sql "${PREFIX}-source" "DELETE FROM tenants WHERE slug = 'pitr-a'"
log "after the target: data C written and data A deleted"
sql "${PREFIX}-source" "SELECT pg_switch_wal()" >/dev/null
for _ in $(seq 1 30); do
    archived="$(sql "${PREFIX}-source" "SELECT last_archived_wal IS NOT NULL AND last_archived_wal >= pg_walfile_name(pg_current_wal_lsn() - 1) FROM pg_stat_archiver")"
    [ "${archived}" = "t" ] && break
    sleep 1
done
[ "${archived}" = "t" ] || fail "the WAL containing C was never archived"
log "WAL through C archived: $(docker exec "${PREFIX}-source" sh -c 'ls /archive | wc -l') segment(s)"

# ------------------------------------------------ disaster, then recovery
docker rm -f "${PREFIX}-source" >/dev/null
log "source server destroyed; recovering the base backup to the target"
docker volume create "${PREFIX}-recovered" >/dev/null
docker run --rm -v "${PREFIX}-base:/base" -v "${PREFIX}-recovered:/recovered" "${IMAGE}" sh -c "
    cp -a /base/data/. /recovered/ &&
    touch /recovered/recovery.signal &&
    printf '%s\n' \"restore_command = 'cp /archive/%f %p'\" \
                  \"recovery_target_time = '${target}'\" \
                  \"recovery_target_action = 'promote'\" >> /recovered/postgresql.auto.conf &&
    chown -R postgres /recovered && chmod 700 /recovered"
docker run -d --name "${PREFIX}-recovered" \
    -e POSTGRES_USER=drill -e POSTGRES_PASSWORD="${PASSWORD}" -e POSTGRES_DB=wasla \
    -e PGDATA=/var/lib/postgresql/data \
    -v "${PREFIX}-recovered:/var/lib/postgresql/data" -v "${PREFIX}-archive:/archive:ro" \
    "${IMAGE}" >/dev/null
wait_ready "${PREFIX}-recovered"

# ----------------------------------------------------------------- verify
tenants="$(sql "${PREFIX}-recovered" "SELECT string_agg(slug, ',' ORDER BY slug) FROM tenants")"
recovered_head="$(sql "${PREFIX}-recovered" "SELECT version_num FROM alembic_version")"
log "recovered tenants: ${tenants}; migration head ${recovered_head}"
[ "${tenants}" = "pitr-a,pitr-b" ] || fail "expected A and B and not C, found ${tenants}"
[ "${recovered_head}" = "${head}" ] || fail "head ${recovered_head} is not ${head}"
unenforced="$(sql "${PREFIX}-recovered" "SELECT count(*) FROM pg_constraint WHERE NOT convalidated")"
[ "${unenforced}" = "0" ] || fail "${unenforced} constraint(s) not validated after recovery"
log "PASS: A present, B present, C absent, A's later deletion not replayed"
