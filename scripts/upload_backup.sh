#!/bin/sh
# Put one validated dump somewhere this host's failure cannot reach.
#
#   upload_backup.sh <artifact>
#
# **This is the step that makes a backup a backup.** A dump sitting next to the
# database it came from survives a dropped table and does not survive the disk,
# the host or the datacentre - and those are the failures a backup exists for.
# `backup_postgres.sh` therefore does not record a success until this has
# returned zero.
#
# One backend, deliberately. `aws s3` speaks to AWS, MinIO, Cloudflare R2,
# Wasabi, Backblaze B2 and Ceph through `BACKUP_S3_ENDPOINT_URL`, so a single
# implementation covers every object store a deployment is likely to pick
# without this repository picking one. A deployment that needs something else
# entirely - a second datacentre over rsync, a tape robot - replaces this file.
# That is a file boundary rather than a `BACKUP_UPLOAD_COMMAND` string that
# something would have to `eval`, and the difference is that nothing here ever
# hands attacker-influenced text to a shell.
#
# **Credentials never appear on a command line.** `aws` reads
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or an instance role) from the
# environment. Nothing here echoes them, and `ps` shows a bucket and a key.
set -eu

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) upload: $*"
}

fail() {
    log "FAILED: $*"
    exit 1
}

[ $# -ge 1 ] || fail "usage: upload_backup.sh <artifact>"
artifact="$1"
[ -f "${artifact}" ] || fail "no such artifact: ${artifact}"

destination="${BACKUP_DESTINATION:-none}"

# ------------------------------------------------------------------- none
#
# Refused rather than skipped. A deployment that has not chosen an off-host
# destination has not got backups, and the one thing this script must not do is
# let that look like success. `BACKUP_ALLOW_LOCAL_ONLY=yes` is the escape hatch
# for a laptop; production Compose does not set it and the runbook says why.
if [ "${destination}" = "none" ]; then
    if [ "${BACKUP_ALLOW_LOCAL_ONLY:-no}" = "yes" ]; then
        log "WARNING: no off-host destination configured; this artifact exists only on this host"
        exit 0
    fi
    fail "BACKUP_DESTINATION is not set. A dump on the same host as its database \
is not a backup. Set BACKUP_DESTINATION=s3 with a bucket, or set \
BACKUP_ALLOW_LOCAL_ONLY=yes if this is a development machine and losing it costs nothing."
fi

[ "${destination}" = "s3" ] || fail "unknown BACKUP_DESTINATION: ${destination}"

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required for BACKUP_DESTINATION=s3}"
prefix="${BACKUP_S3_PREFIX:-wasla}"
# Named so a test can point it at a stub. It is a program name invoked
# directly, never a string handed to a shell, so it is a seam rather than an
# injection point.
cli="${BACKUP_S3_CLI:-aws}"
command -v "${cli}" >/dev/null 2>&1 || fail "${cli} is not on PATH"

name="$(basename "${artifact}")"
key="${prefix}/${name}"
target="s3://${BACKUP_S3_BUCKET}/${key}"

set -- s3 cp "${artifact}" "${target}" --only-show-errors
[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "${BACKUP_S3_ENDPOINT_URL}"
# Server-side encryption at rest. Requested explicitly rather than assumed:
# a bucket policy may already enforce it, and asking for it here means a
# deployment that forgot the policy is still encrypted. Left unset for stores
# that reject the header (some S3-compatible ones do), which is why it is an
# option rather than a constant.
[ -n "${BACKUP_S3_SSE:-}" ] && set -- "$@" --sse "${BACKUP_S3_SSE}"

log "uploading ${name} to s3://${BACKUP_S3_BUCKET}/${prefix}/"
"${cli}" "$@" || fail "upload of ${name} did not complete"

# ------------------------------------------------------------ verification
#
# `cp` exiting zero says the client believed it finished. This asks the store
# what it actually holds, and compares the size - which is the cheapest check
# that distinguishes "uploaded" from "uploaded a zero-byte file because the
# pipe broke". Without it, a truncated remote copy would be indistinguishable
# from a good one until the day somebody needed it.
set -- s3api head-object --bucket "${BACKUP_S3_BUCKET}" --key "${key}" --query ContentLength --output text
[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "${BACKUP_S3_ENDPOINT_URL}"

remote_size="$("${cli}" "$@" 2>/dev/null || true)"
local_size="$(wc -c < "${artifact}" | tr -d ' ')"

[ -n "${remote_size}" ] || fail "the store does not report holding ${key}"
[ "${remote_size}" = "${local_size}" ] \
    || fail "remote copy of ${key} is ${remote_size} bytes; the local artifact is ${local_size}"

log "verified ${key} (${remote_size} bytes) at the destination"
