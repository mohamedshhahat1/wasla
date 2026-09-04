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

# ------------------------------------------------------- encryption at rest
#
# **Required for anything that leaves this host.** A dump is every conversation,
# every phone number, every lead and every email address the platform has ever
# seen, in one file, in the clear. Workspace WhatsApp credentials stay encrypted
# inside it - the AES key ring lives in the environment rather than in the
# database - and nothing else does. So the same sentence that makes an off-host
# destination mandatory makes encryption mandatory: a copy that survives this
# host has to survive it encrypted (ADR-090).
#
# This was optional, and optional is what the audit found. The failure it
# leaves is silent by construction: a deployment that forgot the setting gets a
# green run, a verified remote artifact and a plaintext copy of its whole
# database sitting in a bucket, with nothing anywhere saying so.
#
# **Explicit SSE rather than a bucket rule**, and the choice is about what can
# be checked. `get-bucket-encryption` is a bucket-level API that several
# S3-compatible stores this script is meant to serve do not implement, it needs
# a permission the backup credential does not otherwise want, and it describes
# the bucket's policy rather than the object that was just written. Asking for
# encryption on the request and reading it back off the object is a smaller
# contract, is answered by every store that can honour it at all, and is
# verified below against the artifact that actually exists.
#
# **The escape hatch is one level up.** A laptop sets no destination and takes
# `BACKUP_ALLOW_LOCAL_ONLY=yes`, which returned above without uploading
# anything - so there is no environment detection here, and none is needed:
# nothing leaves the host, so nothing has to be encrypted on the way out.
sse="${BACKUP_S3_SSE:-}"
[ -n "${sse}" ] || fail "BACKUP_S3_SSE is not set. A database dump leaving this host \
carries every conversation, phone number and lead the platform holds, so it does not \
leave unencrypted. Set BACKUP_S3_SSE=AES256 for the store's own keys, or aws:kms with \
BACKUP_S3_SSE_KMS_KEY_ID for a key you control."
case "${sse}" in
    AES256|aws:kms) ;;
    *) fail "BACKUP_S3_SSE must be AES256 or aws:kms; got '${sse}'. Those are the two \
values the S3 API defines, and anything else is a setting that looks configured and \
encrypts nothing." ;;
esac

name="$(basename "${artifact}")"
key="${prefix}/${name}"
target="s3://${BACKUP_S3_BUCKET}/${key}"

set -- s3 cp "${artifact}" "${target}" --only-show-errors
[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "${BACKUP_S3_ENDPOINT_URL}"
set -- "$@" --sse "${sse}"
# Optional even for `aws:kms`: without one, S3 uses the bucket's default managed
# key, which is still a key. Forwarded when a deployment names its own, because
# a key the customer controls is the reason to choose KMS over AES256 at all.
[ -n "${BACKUP_S3_SSE_KMS_KEY_ID:-}" ] \
    && set -- "$@" --sse-kms-key-id "${BACKUP_S3_SSE_KMS_KEY_ID}"

log "uploading ${name} to s3://${BACKUP_S3_BUCKET}/${prefix}/"
"${cli}" "$@" || fail "upload of ${name} did not complete"

# ------------------------------------------------------------ verification
#
# `cp` exiting zero says the client believed it finished. This asks the store
# what it actually holds, and compares two things against it.
#
# **The size**, which is the cheapest check that distinguishes "uploaded" from
# "uploaded a zero-byte file because the pipe broke". Without it, a truncated
# remote copy would be indistinguishable from a good one until the day somebody
# needed it.
#
# **The encryption**, because a flag passed is not a flag honoured. A store that
# ignored `--sse` and answered 200 would leave the dump in the clear with a
# green run behind it, which is the exact failure this whole section exists to
# make impossible. One `head-object` answers both questions, so the guarantee
# costs nothing that the size check was not already paying.
#
# What is verified is the *algorithm* the object reports. The KMS key id is
# not compared: the store answers with a full ARN whatever an operator
# configured - a bare id, an alias - so a comparison would fail on correct
# configurations and prove nothing about the wrong ones. That the object is
# encrypted under KMS is the property this can check, and it is the one that
# matters here.
set -- s3api head-object --bucket "${BACKUP_S3_BUCKET}" --key "${key}" \
    --query "[ContentLength,ServerSideEncryption]" --output text
[ -n "${BACKUP_S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "${BACKUP_S3_ENDPOINT_URL}"

metadata="$("${cli}" "$@" 2>/dev/null || true)"
# Tab-separated, which is what `--output text` emits for a list query. `cut`
# splits on tabs by default, and an absent field renders as the string `None`.
remote_size="$(printf '%s' "${metadata}" | cut -f1)"
remote_sse="$(printf '%s' "${metadata}" | cut -f2)"
local_size="$(wc -c < "${artifact}" | tr -d ' ')"

[ -n "${remote_size}" ] || fail "the store does not report holding ${key}"
[ "${remote_size}" = "${local_size}" ] \
    || fail "remote copy of ${key} is ${remote_size} bytes; the local artifact is ${local_size}"
[ "${remote_sse}" = "${sse}" ] \
    || fail "remote copy of ${key} reports encryption '${remote_sse}', not '${sse}'. \
The upload asked for it and the store did not confirm it, so this artifact is not \
treated as a backup."

log "verified ${key} (${remote_size} bytes, encrypted ${remote_sse}) at the destination"
