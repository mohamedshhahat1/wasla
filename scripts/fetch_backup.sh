#!/bin/sh
# Bring a backup back from off-host storage.
#
#   fetch_backup.sh <destination-directory> [artifact-name]
#
# With no name, the newest artifact under the configured prefix. That is the
# one a recovery wants, and making an operator paste a timestamp at the moment
# their database is gone is how the wrong backup gets restored.
#
# **This is the half of disaster recovery nobody tests.** Uploading is easy to
# believe in because it returns zero; the question that matters is whether the
# bytes come back on a host that has never seen them, and this is the script
# that answers it. `docs/BACKUP.md` records a drill that removes the local copy
# before running this, precisely so the answer is not "yes, from the copy that
# was already there".
#
# Credentials come from the environment, as in `upload_backup.sh`, and nothing
# here echoes them.
set -eu

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) fetch: $*"
}

fail() {
    log "FAILED: $*"
    exit 1
}

[ $# -ge 1 ] || fail "usage: fetch_backup.sh <destination-directory> [artifact-name]"
into="$1"
wanted="${2:-}"

destination="${BACKUP_DESTINATION:-none}"
[ "${destination}" = "s3" ] || fail "BACKUP_DESTINATION must be s3 to fetch; got ${destination}"

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"
prefix="${BACKUP_S3_PREFIX:-wasla}"
cli="${BACKUP_S3_CLI:-aws}"
command -v "${cli}" >/dev/null 2>&1 || fail "${cli} is not on PATH"

endpoint=""
[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && endpoint="--endpoint-url ${BACKUP_S3_ENDPOINT_URL}"

mkdir -p "${into}" || fail "cannot create ${into}"

if [ -z "${wanted}" ]; then
    # `ls` sorts lexicographically and the names are `<db>-<ISO-8601 basic>.dump`,
    # so the last line is the newest. That holds because the timestamp is
    # zero-padded and UTC - the same reason the backup script formats it that
    # way rather than however the host's locale would.
    # shellcheck disable=SC2086
    wanted="$("${cli}" ${endpoint} s3 ls "s3://${BACKUP_S3_BUCKET}/${prefix}/" \
        | awk '{print $4}' | grep '\.dump$' | sort | tail -1 || true)"
    [ -n "${wanted}" ] || fail "no .dump artifacts under s3://${BACKUP_S3_BUCKET}/${prefix}/"
    log "newest artifact is ${wanted}"
fi

target="${into}/${wanted}"
# shellcheck disable=SC2086
"${cli}" ${endpoint} s3 cp "s3://${BACKUP_S3_BUCKET}/${prefix}/${wanted}" "${target}" \
    --only-show-errors || fail "could not download ${wanted}"

# Read back before it is believed, exactly as the backup does before uploading.
# A download that produced an unreadable file is a failure to find out about
# now rather than halfway through a restore.
if command -v pg_restore >/dev/null 2>&1; then
    pg_restore --list "${target}" >/dev/null 2>&1 \
        || fail "the downloaded artifact is not a readable dump"
    log "verified ${wanted} is a readable dump"
fi

size="$(wc -c < "${target}" | tr -d ' ')"
log "wrote ${target} (${size} bytes)"
echo "${target}"
