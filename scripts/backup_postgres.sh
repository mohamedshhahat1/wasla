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
# **Retention prunes only after a successful run.** A failed run must never be
# able to delete the last good backup, which is the failure mode that turns one
# bad night into no recovery point at all.
#
# **A run succeeds when the dump is off this host, not when pg_dump exits.** A
# validated dump in BACKUP_DIR is a staging artifact: it survives a dropped
# table and it does not survive the machine, and the machine is what a backup
# is for. So the sequence is dump, validate, upload, verify at the destination,
# and only then advance the recorded last success. Anything that stops before
# the end leaves the previous success where it was, which is what makes a
# staleness alert mean something (ADR-075).
#
# Where this runs: the deployment's PostgreSQL image carries `pg_dump` at the
# server's own version, so the intended invocation is through that image -
# `docker compose -f docker-compose.prod.yml run --rm backup` - driven by the
# host's cron or systemd timer. See docs/BACKUP.md.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/wasla}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
# Where the machine-readable outcome is written. Read by the API to publish
# `wasla_backup_*`, and by anything else that wants to know whether this
# deployment still has a recovery point. Contains no credential and no customer
# data - see `_write_status`.
BACKUP_STATUS_PATH="${BACKUP_STATUS_PATH:-${BACKUP_DIR}/status.json}"
# Where the uploader lives. Overridable so the drill can run the scripts from a
# checkout rather than from the image's copy.
BACKUP_SCRIPT_DIR="${BACKUP_SCRIPT_DIR:-$(dirname "$0")}"

log() {
    # ISO-8601 UTC, matching the application's own log timestamps so an
    # operator correlating a restore against a request trace is not converting
    # between two clocks.
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"
}

# Set as the run progresses, so a failure can say which stage it failed at
# without the caller having to infer it from the message.
stage="start"

_status_field() {
    # One field out of the existing status file, without a JSON parser: the
    # image has neither `jq` nor Python, and the file is written by this script
    # so its shape is known. Absent file or absent field prints nothing.
    [ -f "${BACKUP_STATUS_PATH}" ] || return 0
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
        "${BACKUP_STATUS_PATH}" | head -1
}

_status_number() {
    [ -f "${BACKUP_STATUS_PATH}" ] || return 0
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" \
        "${BACKUP_STATUS_PATH}" | head -1
}

_write_status() {
    # $1 outcome, $2 artifact name, $3 size, $4 destination
    #
    # **Nothing here is a secret and nothing here is customer data.** A
    # filename, a byte count, a destination kind and two timestamps. The
    # bucket, the endpoint and every credential are deliberately absent: this
    # file is mounted read-only into the API so it can be published as a
    # metric, and a status file is exactly the sort of thing that ends up in a
    # support ticket.
    outcome="$1"
    artifact="${2:-}"
    size="${3:-0}"
    where="${4:-none}"
    written_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    previous_success="$(_status_field last_success_at)"
    previous_artifact="$(_status_field last_success_artifact)"
    previous_failures="$(_status_number failures_total)"
    [ -n "${previous_failures}" ] || previous_failures=0

    if [ "${outcome}" = "success" ]; then
        success_at="${written_at}"
        success_artifact="${artifact}"
        failures="${previous_failures}"
        failed_stage=""
    else
        # The previous success is carried forward untouched. That is the whole
        # point of the file: a failed run must not make the deployment look
        # like it has never had a backup, and must not make it look like it
        # just had one either.
        success_at="${previous_success}"
        success_artifact="${previous_artifact}"
        failures=$((previous_failures + 1))
        failed_stage="${stage}"
    fi

    mkdir -p "$(dirname "${BACKUP_STATUS_PATH}")" 2>/dev/null || true
    cat > "${BACKUP_STATUS_PATH}.part" <<STATUS
{
  "outcome": "${outcome}",
  "written_at": "${written_at}",
  "last_success_at": "${success_at}",
  "last_success_artifact": "${success_artifact}",
  "last_success_bytes": ${size},
  "destination": "${where}",
  "failures_total": ${failures},
  "failed_stage": "${failed_stage}"
}
STATUS
    # Renamed rather than written in place, so a reader never sees half a file.
    mv "${BACKUP_STATUS_PATH}.part" "${BACKUP_STATUS_PATH}"
}

fail() {
    log "FAILED: $*"
    _write_status failure "" 0 "${BACKUP_DESTINATION:-none}"
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

stage="dump"
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
stage="validate"
if ! pg_restore --list "${partial}" >/dev/null 2>&1; then
    rm -f "${partial}"
    fail "the dump could not be read back; no artefact was kept"
fi

mv "${partial}" "${target}"
size="$(wc -c < "${target}" | tr -d ' ')"
log "staged ${target} (${size} bytes)"

# ------------------------------------------------------------------ upload
#
# The step that turns a staging artifact into a backup. Anything but zero here
# fails the run, and the recorded last success stays where it was - so a
# deployment whose object store has been unreachable for two days looks two
# days stale, which is exactly what it is.
stage="upload"
uploader="${BACKUP_SCRIPT_DIR}/upload_backup.sh"
[ -f "${uploader}" ] || fail "no uploader at ${uploader}"
sh "${uploader}" "${target}" || fail "the dump was not stored off this host"

# --------------------------------------------------------------- retention
#
# Only reached once the artifact is durable, deliberately: see the header.
# `-mtime +N` is days, and the pattern is anchored to this database's dumps so
# a directory shared with anything else is left alone.
#
# **This prunes the local staging copy only.** What the object store keeps is
# the object store's business - a lifecycle rule there outlives this host and
# cannot be undone by a bug in a shell script, which is the right place for a
# retention policy that a recovery depends on (docs/BACKUP.md).
stage="retention"
if [ "${BACKUP_RETENTION_DAYS}" -gt 0 ] 2>/dev/null; then
    removed="$(find "${BACKUP_DIR}" -maxdepth 1 -type f \
        -name "${PGDATABASE}-*.dump" \
        -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete | wc -l | tr -d ' ')"
    log "retention: kept ${BACKUP_RETENTION_DAYS} days of local staging, removed ${removed}"
fi

stage="done"
_write_status success "$(basename "${target}")" "${size}" "${BACKUP_DESTINATION:-none}"
log "done: ${PGDATABASE} is backed up to ${BACKUP_DESTINATION:-none}"
