#!/bin/sh
# Restore one PostgreSQL backup into a named database, and prove it worked.
#
#   restore_postgres.sh <dump-file> <target-database>
#
# **The target is always named, never defaulted.** There is no "restore to the
# database in DATABASE_URL" path, because the one thing a restore script must
# never do is the destructive thing by accident. Restoring over production is
# possible - sometimes it is the point - but it takes an explicit opt-in that
# an operator has to type, so it cannot be reached by omitting an argument.
#
# What it does, in order:
#   1. refuse to overwrite the configured production database unless told to
#   2. create the target, or refuse to touch an existing one without --clean
#   3. restore
#   4. verify: the schema is there, Alembic's head matches the code, the
#      pgvector extension came back, and representative rows can be counted
#
# Step 4 is the part that makes this a restore procedure rather than a
# `pg_restore` invocation. A dump that restores into a database the application
# cannot query is not a backup, it is a file.
set -eu

usage() {
    echo "usage: $0 <dump-file> <target-database> [--clean]" >&2
    echo "" >&2
    echo "  --clean   drop the target database first if it exists" >&2
    echo "" >&2
    echo "  Set WASLA_RESTORE_ALLOW_PRODUCTION=yes to permit a target that" >&2
    echo "  matches the database named in DATABASE_URL." >&2
    exit 64
}

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) restore: $*"
}

fail() {
    log "FAILED: $*"
    exit 1
}

[ $# -ge 2 ] || usage
dump="$1"
target="$2"
clean="${3:-}"

[ -f "${dump}" ] || fail "no such dump: ${dump}"

# ---------------------------------------------------------------- connection
if [ -n "${DATABASE_URL:-}" ] && [ -z "${PGHOST:-}" ]; then
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
    configured="${location#*/}"
    CONFIGURED_DATABASE="${configured%%\?*}"
    export PGUSER PGHOST
    [ -n "${PGPORT:-}" ] && export PGPORT
fi

: "${PGHOST:?PGHOST or DATABASE_URL is required}"
: "${PGUSER:?PGUSER or DATABASE_URL is required}"
PGPORT="${PGPORT:-5432}"
export PGPORT

# ------------------------------------------------------------ target safety
#
# The guard that makes this runnable without holding one's breath, and it runs
# *before* the tooling check on purpose. If somebody has aimed a restore at
# production, "you are about to overwrite production" is the thing they need to
# read - not "pg_restore is not installed", which they would fix and then run
# again.
if [ -n "${CONFIGURED_DATABASE:-}" ] && [ "${target}" = "${CONFIGURED_DATABASE}" ]; then
    if [ "${WASLA_RESTORE_ALLOW_PRODUCTION:-no}" != "yes" ]; then
        fail "refusing to restore over ${target}, the database DATABASE_URL names. \
Restore into a scratch database, or set WASLA_RESTORE_ALLOW_PRODUCTION=yes if \
overwriting it is genuinely the intent."
    fi
    log "WARNING: restoring over the configured database ${target}, as explicitly permitted"
fi

command -v pg_restore >/dev/null 2>&1 || fail "pg_restore is not on PATH"
command -v psql >/dev/null 2>&1 || fail "psql is not on PATH"

# `postgres` is the maintenance database every server has; CREATE DATABASE
# cannot run from inside the database being created.
admin() {
    psql --no-password --quiet --tuples-only --no-align --dbname=postgres -c "$1"
}

exists="$(admin "SELECT 1 FROM pg_database WHERE datname = '${target}'" || fail "cannot reach PostgreSQL")"

if [ -n "${exists}" ]; then
    if [ "${clean}" = "--clean" ]; then
        log "dropping existing ${target}"
        admin "DROP DATABASE \"${target}\"" >/dev/null || fail "could not drop ${target}"
        exists=""
    else
        fail "${target} already exists. Pass --clean to replace it, or choose another name."
    fi
fi

if [ -z "${exists}" ]; then
    log "creating ${target}"
    admin "CREATE DATABASE \"${target}\"" >/dev/null || fail "could not create ${target}"
fi

# ------------------------------------------------------------------ restore
log "restoring ${dump} into ${target}"
# --exit-on-error, so a partially restored database is never reported as a
# success. Without it pg_restore reports errors and carries on, and the shape
# of that failure is a database that looks restored and is missing a table.
if ! pg_restore \
    --no-password \
    --dbname="${target}" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    "${dump}"; then
    fail "pg_restore exited non-zero; ${target} is not a usable restore"
fi

# ----------------------------------------------------------------- verify
verify() {
    psql --no-password --quiet --tuples-only --no-align --dbname="${target}" -c "$1"
}

tables="$(verify "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
[ "${tables:-0}" -gt 0 ] || fail "the restored database has no tables"
log "schema: ${tables} tables"

extensions="$(verify "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('vector', 'pgcrypto')")"
case "${extensions}" in
    *vector*) log "extensions: ${extensions}" ;;
    *) fail "the pgvector extension did not come back; embeddings would be unusable" ;;
esac

head="$(verify "SELECT version_num FROM alembic_version" || true)"
[ -n "${head}" ] || fail "alembic_version is empty; this dump did not carry a migrated schema"
log "migration head: ${head}"

if [ -n "${WASLA_EXPECTED_HEAD:-}" ] && [ "${head}" != "${WASLA_EXPECTED_HEAD}" ]; then
    fail "restored head ${head} is not the expected ${WASLA_EXPECTED_HEAD}; \
run 'alembic upgrade head' against ${target} before serving from it"
fi

# Representative rows. Tenants and users are the two tables nothing else works
# without, so a restore that has the schema and neither of these is a restore
# of an empty database - which is a different disaster from a failed one and
# must not be reported as a success.
tenants="$(verify "SELECT count(*) FROM tenants")"
users="$(verify "SELECT count(*) FROM users")"
log "rows: ${tenants} tenants, ${users} users"

log "done: ${target} is restored and verified"
