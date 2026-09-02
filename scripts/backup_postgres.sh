#!/bin/sh
# Take one PostgreSQL backup.
#
# `pg_dump` in the custom format (-Fc): compressed, and restorable selectively
# with `pg_restore`, which a plain SQL dump is not. The extension declarations
# pgvector needs come with it, so a restore into an empty database recreates
# `vector` and `pgcrypto` before the columns that use them.
#
# **Credentials never appear on a command line or in the output.** The password
# is read from PGPASSWORD, or from DATABASE_URL and then exported - so it is in
# this process's environment and nowhere else. `ps` on a shared host shows the
# host, the user and the database, and no secret. Nothing here echoes the URL.
#
# **Retention prunes only after a successful dump.** A failed run must never be
# able to delete the last good backup, which is the failure mode that turns one
# bad night into no recovery point at all.
#
# Where this runs: the deployment's PostgreSQL image carries `pg_dump` at the
# server's own version, so the intended invocation is through that image -
# `docker compose -f docker-compose.prod.yml run --rm backup` - driven by the
# host's cron or systemd timer. See docs/BACKUP.md.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/wasla}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

log() {
    # ISO-8601 UTC, matching the application's own log timestamps so an
    # operator correlating a restore against a request trace is not converting
    # between two clocks.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"
}

fail() {
    log "FAILED: $*"
    exit 1
}

# ---------------------------------------------------------------- connection
#
# Either the standard libpq variables, or DATABASE_URL - which is what the
# application itself is configured with, so a deployment does not have to
# describe its database twice and cannot describe it two different ways.
if [ -n "${DATABASE_URL:-}" ] && [ -z "${PGHOST:-}" ]; then
    # postgresql+asyncpg://user:password@host:port/name -> the parts libpq wants.
    # Parsed with sed rather than by sourcing anything, because this string
    # contains a password and must not reach a shell that could echo it.
    url="${DATABASE_URL#*://}"
    credentials="${url%%@*}"
    location="${url#*@}"
    PGUSER="${PGUSER:-$(printf '%s' "${credentials%%:*}")}"
    if [ "${credentials}" != "${credentials#*:}" ]; then
        PGPASSWORD="${PGPASSWORD:-$(printf '%s' "${credentials#*:}")}"
        export PGPASSWORD
    fi
    hostport="${location%%/*}"
    PGHOST="${PGHOST:-${hostport%%:*}}"
    case "${hostport}" in
        *:*) PGPORT="${PGPORT:-${hostport##*:}}" ;;
    esac
    database="${location#*/}"
    PGDATABASE="${PGDATABASE:-${database%%\?*}}"
    export PGUSER PGHOST PGDATABASE
    [ -n "${PGPORT:-}" ] && export PGPORT
fi

: "${PGHOST:?PGHOST or DATABASE_URL is required}"
: "${PGUSER:?PGUSER or DATABASE_URL is required}"
: "${PGDATABASE:?PGDATABASE or DATABASE_URL is required}"
PGPORT="${PGPORT:-5432}"
export PGPORT

command -v pg_dump >/dev/null 2>&1 || fail "pg_dump is not on PATH"

mkdir -p "${BACKUP_DIR}" || fail "cannot create ${BACKUP_DIR}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
# `.part` until it is complete, then renamed. A restore script pointed at this
# directory can therefore never pick up a dump that was still being written -
# including one from a run the host killed halfway through.
target="${BACKUP_DIR}/${PGDATABASE}-${stamp}.dump"
partial="${target}.part"

log "dumping ${PGDATABASE} from ${PGHOST}:${PGPORT} as ${PGUSER}"

# --no-password: never prompt. A missing credential must fail the run rather
# than hang a cron job on a prompt nobody will ever answer.
if ! pg_dump \
    --format=custom \
    --compress=6 \
    --no-password \
    --file="${partial}" \
    "${PGDATABASE}"; then
    rm -f "${partial}"
    fail "pg_dump exited non-zero; no artefact was kept"
fi

# An empty or truncated artefact is a failure that pg_dump does not always
# report, so the dump is read back before it is believed. `pg_restore --list`
# parses the archive's table of contents without touching a database.
if ! pg_restore --list "${partial}" >/dev/null 2>&1; then
    rm -f "${partial}"
    fail "the dump could not be read back; no artefact was kept"
fi

mv "${partial}" "${target}"
size="$(wc -c < "${target}" | tr -d ' ')"
log "wrote ${target} (${size} bytes)"

# --------------------------------------------------------------- retention
#
# Only reached on success, deliberately: see the header. `-mtime +N` is days,
# and the pattern is anchored to this database's dumps so a directory shared
# with anything else is left alone.
if [ "${BACKUP_RETENTION_DAYS}" -gt 0 ] 2>/dev/null; then
    removed="$(find "${BACKUP_DIR}" -maxdepth 1 -type f \
        -name "${PGDATABASE}-*.dump" \
        -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete | wc -l | tr -d ' ')"
    log "retention: kept ${BACKUP_RETENTION_DAYS} days, removed ${removed}"
fi

log "done"
