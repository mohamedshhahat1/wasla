"""The step that turns a dump into a backup, and the state it writes down.

A validated dump in `BACKUP_DIR` survives a dropped table. It does not survive
the machine, and the machine is what a backup is for — so `backup_postgres.sh`
does not record a success until `upload_backup.sh` has put the artifact
somewhere else and read back what the store holds.

The tests here drive the shell with a **stub `aws`**, which is what makes them
runnable anywhere: no cloud, no credentials, no MinIO. What they prove is the
contract — refuses with no destination, fails the run when the upload fails,
advances the recorded success only when it does not, and never prints a
credential. That the contract holds against a *real* S3-compatible store is
proved by the drill in docs/BACKUP.md, which ran against MinIO, and the two
kinds of evidence are deliberately not conflated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
UPLOAD = ROOT / "scripts" / "upload_backup.sh"
FETCH = ROOT / "scripts" / "fetch_backup.sh"
BACKUP = ROOT / "scripts" / "backup_postgres.sh"

SHELL = shutil.which("sh") or shutil.which("bash")
needs_shell = pytest.mark.skipif(SHELL is None, reason="No POSIX shell available.")

SECRET = "drill-secret-must-never-be-printed"


def stub_aws(
    tmp_path: Path,
    *,
    fail_on: str = "",
    size: int | None = None,
    honours_sse: bool = True,
) -> Path:
    """A stand-in for the AWS CLI that records what it was asked to do.

    `fail_on` makes one subcommand exit non-zero, which is how the upload and
    verification failures are provoked without an unreachable network.

    `honours_sse=False` models the store this contract exists to catch: one
    that accepts `--sse`, answers 200, and holds the object in the clear. A
    flag passed is not a flag honoured, and only `head-object` can tell the
    difference.
    """
    store = tmp_path / "remote"
    store.mkdir(exist_ok=True)
    binary = tmp_path / "aws"
    reported = '$(cat "REMOTE/sse" 2>/dev/null)' if honours_sse else "None"
    binary.write_text(
        (
            "#!/bin/sh\n"
            "set -eu\n"
            'echo "$@" >> "REMOTE/calls.log"\n'
            'if [ "$1" = "FAILON" ]; then exit 3; fi\n'
            'if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then\n'
            '  cp "$3" "REMOTE/$(basename "$3")" 2>/dev/null || true\n'
            # What the request asked for, remembered so head-object can answer
            # it back the way a store that honours the header would.
            "  asked=None\n"
            "  while [ $# -gt 0 ]; do\n"
            '    if [ "$1" = "--sse" ]; then shift; asked="$1"; fi\n'
            "    shift\n"
            "  done\n"
            '  printf "%s" "$asked" > "REMOTE/sse"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "s3api" ]; then\n'
            "  held=SIZE\n"
            '  sse="REPORTED"\n'
            '  [ -n "$sse" ] || sse=None\n'
            '  printf "%s\\t%s\\n" "$held" "$sse"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        .replace("REPORTED", reported)
        .replace("REMOTE", str(store))
        .replace("FAILON", fail_on)
        .replace(
            "SIZE",
            (
                str(size)
                if size is not None
                else f'$(wc -c < "{store}/$(basename "$6")" 2>/dev/null | tr -d " ")'
            ),
        ),
        encoding="utf-8",
        newline="\n",
    )
    binary.chmod(0o755)
    return binary


def run(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert SHELL is not None
    return subprocess.run(  # noqa: S603 - a fixed script path, no shell string
        [SHELL, str(script), *arguments],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **(env or {})},
        timeout=120,
        check=False,
    )


def artifact(tmp_path: Path, name: str = "wasla-20260902T000000Z.dump") -> Path:
    path = tmp_path / name
    path.write_bytes(b"PGDMP" + b"x" * 4096)
    return path


def s3_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    return {
        "BACKUP_DESTINATION": "s3",
        "BACKUP_S3_BUCKET": "wasla-backups",
        "BACKUP_S3_PREFIX": "wasla",
        "BACKUP_S3_CLI": str(tmp_path / "aws"),
        # Required now, not optional: an off-host dump does not leave
        # unencrypted (ADR-090). Overridden by the tests that prove the guard.
        "BACKUP_S3_SSE": "AES256",
        "AWS_ACCESS_KEY_ID": "drill-key-id",
        "AWS_SECRET_ACCESS_KEY": SECRET,
        **overrides,
    }


# ------------------------------------------------------------ it is shipped


def test_the_uploader_and_the_fetcher_are_shipped_and_fail_fast() -> None:
    for script in (UPLOAD, FETCH):
        assert script.is_file()
        text = script.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh")
        assert "set -eu" in text


def test_no_shell_script_carries_a_carriage_return() -> None:
    """A `#!/bin/sh\\r` shebang is a container that will not start."""
    for script in (UPLOAD, FETCH, BACKUP):
        assert b"\r" not in script.read_bytes()


# ------------------------------------------------------ the off-host contract


@needs_shell
def test_a_backup_with_no_destination_is_refused(tmp_path: Path) -> None:
    """A dump on the same host as its database is not a backup."""
    result = run(UPLOAD, str(artifact(tmp_path)), env={"BACKUP_DESTINATION": "none"})

    assert result.returncode != 0
    assert "not a backup" in result.stdout + result.stderr


@needs_shell
def test_local_only_is_possible_but_has_to_be_asked_for(tmp_path: Path) -> None:
    """The escape hatch for a laptop, and it says what it is giving up."""
    result = run(
        UPLOAD,
        str(artifact(tmp_path)),
        env={"BACKUP_DESTINATION": "none", "BACKUP_ALLOW_LOCAL_ONLY": "yes"},
    )

    assert result.returncode == 0
    assert "only on this host" in result.stdout


@needs_shell
def test_a_successful_upload_verifies_what_the_store_holds(tmp_path: Path) -> None:
    stub_aws(tmp_path)
    source = artifact(tmp_path)

    result = run(UPLOAD, str(source), env=s3_env(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")
    assert "s3 cp" in calls
    # The verification is the point: `cp` exiting zero only says the client
    # believed it finished.
    assert "s3api head-object" in calls
    assert "verified" in result.stdout


@needs_shell
def test_an_upload_that_fails_fails_the_run(tmp_path: Path) -> None:
    stub_aws(tmp_path, fail_on="s3")
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert result.returncode != 0
    assert "not stored off this host" in result.stdout + result.stderr or "FAILED" in result.stdout


@needs_shell
def test_a_truncated_remote_copy_is_caught(tmp_path: Path) -> None:
    """The failure `cp` cannot see: it finished, and what landed is wrong."""
    stub_aws(tmp_path, size=12)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert result.returncode != 0
    assert "bytes" in result.stdout + result.stderr


@needs_shell
def test_a_store_that_does_not_hold_the_object_is_caught(tmp_path: Path) -> None:
    stub_aws(tmp_path, fail_on="s3api")
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert result.returncode != 0


@needs_shell
def test_the_uploader_never_prints_a_credential(tmp_path: Path) -> None:
    """`ps` shows a bucket and a key; the secret stays in the environment."""
    stub_aws(tmp_path)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    calls = (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")
    assert SECRET not in calls, "a credential reached the CLI's command line"
    assert "drill-key-id" not in calls


@needs_shell
def test_an_unknown_destination_is_refused(tmp_path: Path) -> None:
    result = run(UPLOAD, str(artifact(tmp_path)), env={"BACKUP_DESTINATION": "carrier-pigeon"})

    assert result.returncode != 0
    assert "unknown BACKUP_DESTINATION" in result.stdout + result.stderr


# ------------------------------------------------------ encryption at rest


@needs_shell
def test_a_production_backup_refuses_without_an_encryption_guarantee(tmp_path: Path) -> None:
    """The finding, closed. `BACKUP_S3_SSE` used to be optional.

    A dump is every conversation, phone number, lead and email address the
    platform holds, in one file, in the clear. A deployment that forgot the
    setting got a green run and a plaintext copy of its whole database in a
    bucket, with nothing anywhere saying so.
    """
    stub_aws(tmp_path)
    environment = s3_env(tmp_path)
    del environment["BACKUP_S3_SSE"]

    result = run(UPLOAD, str(artifact(tmp_path)), env=environment)

    assert result.returncode != 0
    assert "BACKUP_S3_SSE is not set" in result.stdout + result.stderr
    calls = tmp_path / "remote" / "calls.log"
    assert not calls.exists(), "the refusal came after something had already been uploaded"


@needs_shell
def test_a_local_only_development_backup_is_untouched_by_the_requirement(tmp_path: Path) -> None:
    """The escape hatch is one level up, and it still works.

    A laptop sets no destination and takes `BACKUP_ALLOW_LOCAL_ONLY=yes`, which
    returns before any upload - so there is no encryption question to answer,
    and no second environment system in the shell scripts to answer it with.
    """
    result = run(
        UPLOAD,
        str(artifact(tmp_path)),
        env={"BACKUP_DESTINATION": "none", "BACKUP_ALLOW_LOCAL_ONLY": "yes"},
    )

    assert result.returncode == 0
    assert "only on this host" in result.stdout


@needs_shell
@pytest.mark.parametrize("value", ["aes256", "AES-256", "yes", "true", "kms"])
def test_an_encryption_setting_the_s3_api_does_not_define_is_refused(
    tmp_path: Path, value: str
) -> None:
    """A setting that looks configured and encrypts nothing."""
    stub_aws(tmp_path)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path, BACKUP_S3_SSE=value))

    assert result.returncode != 0
    assert "must be AES256 or aws:kms" in result.stdout + result.stderr


@needs_shell
@pytest.mark.parametrize("algorithm", ["AES256", "aws:kms"])
def test_the_encryption_request_is_forwarded_to_the_store(tmp_path: Path, algorithm: str) -> None:
    stub_aws(tmp_path)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path, BACKUP_S3_SSE=algorithm))

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")
    assert f"--sse {algorithm}" in calls


@needs_shell
def test_a_named_kms_key_is_forwarded_and_a_missing_one_is_not_invented(tmp_path: Path) -> None:
    """Optional even for KMS: without one, S3 uses the bucket's default key."""
    stub_aws(tmp_path)
    named = run(
        UPLOAD,
        str(artifact(tmp_path)),
        env=s3_env(
            tmp_path,
            BACKUP_S3_SSE="aws:kms",
            BACKUP_S3_SSE_KMS_KEY_ID="alias/wasla-backups",
        ),
    )
    assert named.returncode == 0, named.stdout + named.stderr
    calls = (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")
    assert "--sse-kms-key-id alias/wasla-backups" in calls

    (tmp_path / "remote" / "calls.log").unlink()
    default = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path, BACKUP_S3_SSE="aws:kms"))
    assert default.returncode == 0, default.stdout + default.stderr
    assert "--sse-kms-key-id" not in (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")


@needs_shell
def test_a_store_that_ignores_the_request_does_not_produce_a_backup(tmp_path: Path) -> None:
    """A flag passed is not a flag honoured.

    The store took the object and answered 200. Only `head-object` can tell
    that what it holds is in the clear, which is why the verification reads the
    encryption alongside the size rather than trusting the exit code.
    """
    stub_aws(tmp_path, honours_sse=False)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert result.returncode != 0
    assert "reports encryption" in result.stdout + result.stderr


@needs_shell
def test_a_verified_upload_says_what_it_verified(tmp_path: Path) -> None:
    stub_aws(tmp_path)
    result = run(UPLOAD, str(artifact(tmp_path)), env=s3_env(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "encrypted AES256" in result.stdout
    calls = (tmp_path / "remote" / "calls.log").read_text(encoding="utf-8")
    assert "s3api head-object" in calls, "remote validation stopped happening"


@needs_shell
def test_an_unencrypted_upload_does_not_advance_the_recorded_success(tmp_path: Path) -> None:
    """Fail closed, and leave the last known good backup where it was.

    Stale-backup alerting is what turns this into somebody being told. A run
    that advanced its success on an unencrypted artifact would silence the one
    signal that says the deployment has no recovery point it can trust.
    """
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "outcome": "success",
                "written_at": "2026-08-01T00:00:00Z",
                "last_success_at": "2026-08-01T00:00:00Z",
                "last_success_artifact": "wasla-20260801T000000Z.dump",
                "last_success_bytes": 4101,
                "destination": "s3",
                "failures_total": 0,
                "failed_stage": "",
            }
        ),
        encoding="utf-8",
    )
    # The stubs below are defined further down the file; both are resolved at
    # call time. They are what lets this reach the *upload* stage, which is
    # where the encryption verdict is made.
    stub_postgres(tmp_path)
    stub_aws(tmp_path, honours_sse=False)

    result = run(BACKUP, env=backup_env(tmp_path, status))

    assert result.returncode != 0
    after = read_status(status)
    assert after["outcome"] == "failure"
    assert after["last_success_at"] == "2026-08-01T00:00:00Z"
    assert after["last_success_artifact"] == "wasla-20260801T000000Z.dump"
    # The byte count travels with the artifact it describes. Resetting it to
    # zero would read as "the last good backup was empty", which is false and
    # frightening in exactly the situation somebody is reading this file.
    assert after["last_success_bytes"] == 4101
    assert after["failed_stage"] == "upload"


# -------------------------------------------------------------- the fetcher


@needs_shell
def test_the_fetcher_refuses_without_a_destination(tmp_path: Path) -> None:
    result = run(FETCH, str(tmp_path / "into"), env={"BACKUP_DESTINATION": "none"})

    assert result.returncode != 0


@needs_shell
def test_the_fetcher_never_prints_a_credential(tmp_path: Path) -> None:
    stub_aws(tmp_path, fail_on="s3")
    result = run(FETCH, str(tmp_path / "into"), "some.dump", env=s3_env(tmp_path))

    assert SECRET not in result.stdout + result.stderr


# -------------------------------------------------------- the status contract


def read_status(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


@needs_shell
def test_a_dump_that_never_uploads_does_not_advance_the_last_success(tmp_path: Path) -> None:
    """The false-healthy signal this whole design exists to avoid.

    Alerting on "the newest file in the backup directory" would call this
    deployment fine. Its dumps are succeeding and none of them has left the
    host for a week.
    """
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "outcome": "success",
                "written_at": "2026-08-01T00:00:00Z",
                "last_success_at": "2026-08-01T00:00:00Z",
                "last_success_artifact": "wasla-20260801T000000Z.dump",
                "last_success_bytes": 4101,
                "destination": "s3",
                "failures_total": 0,
                "failed_stage": "",
            }
        ),
        encoding="utf-8",
    )
    stub_aws(tmp_path, fail_on="s3")

    result = run(
        BACKUP,
        env={
            **s3_env(tmp_path),
            "PGHOST": "127.0.0.1",
            "PGPORT": "1",
            "PGUSER": "nobody",
            "PGDATABASE": "wasla",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "BACKUP_STATUS_PATH": str(status),
            "BACKUP_SCRIPT_DIR": str(ROOT / "scripts"),
        },
    )

    assert result.returncode != 0
    after = read_status(status)
    assert after["outcome"] == "failure"
    assert after["last_success_at"] == "2026-08-01T00:00:00Z", "a failed run advanced the success"
    assert after["failures_total"] == 1
    assert after["failed_stage"], "a failure must say which stage it failed at"


@needs_shell
def test_a_failed_run_records_no_credential_in_the_status(tmp_path: Path) -> None:
    """This file is mounted into the API and ends up in support tickets."""
    status = tmp_path / "status.json"
    stub_aws(tmp_path, fail_on="s3")

    run(
        BACKUP,
        env={
            **s3_env(tmp_path),
            "PGHOST": "127.0.0.1",
            "PGPORT": "1",
            "PGUSER": "nobody",
            "PGDATABASE": "wasla",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "BACKUP_STATUS_PATH": str(status),
            "BACKUP_SCRIPT_DIR": str(ROOT / "scripts"),
        },
    )

    body = status.read_text(encoding="utf-8")
    assert SECRET not in body
    assert "drill-key-id" not in body
    assert "wasla-backups" not in body, "the bucket name is not the API's business"


def stub_postgres(tmp_path: Path) -> Path:
    """Stand-ins for `pg_dump` and `pg_restore`, so the script's own flow is testable.

    The tests above can only reach the stages before the dump, because a real
    `pg_dump` needs a real PostgreSQL and the runner may have neither. What
    that left untested is the most important sequence in the script: dump
    succeeds, upload fails, run fails, recorded success does not move. Two
    stubs on PATH make that reachable anywhere.

    This does not claim to test PostgreSQL. It tests the shell around it, and
    the drill in docs/BACKUP.md tests the rest.
    """
    binaries = tmp_path / "pgbin"
    binaries.mkdir(exist_ok=True)
    dump = binaries / "pg_dump"
    dump.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in --file=*) printf "PGDMP fake dump" > "${arg#--file=}" ;; esac\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    restore = binaries / "pg_restore"
    restore.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    for binary in (dump, restore):
        binary.chmod(0o755)
    return binaries


def backup_env(tmp_path: Path, status: Path) -> dict[str, str]:
    return {
        **s3_env(tmp_path),
        "PATH": f"{tmp_path / 'pgbin'}{os.pathsep}{os.environ.get('PATH', '')}",
        "PGHOST": "127.0.0.1",
        "PGPORT": "5432",
        "PGUSER": "wasla",
        "PGDATABASE": "wasla",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_PATH": str(status),
        "BACKUP_SCRIPT_DIR": str(ROOT / "scripts"),
        "BACKUP_RETENTION_DAYS": "14",
    }


@needs_shell
def test_a_dump_alone_is_not_a_success(tmp_path: Path) -> None:
    """The distinction the whole design rests on.

    `pg_dump` worked. The artifact is valid. It never left the host, so this
    deployment has no recovery point and the run must say so.
    """
    stub_postgres(tmp_path)
    stub_aws(tmp_path, fail_on="s3")
    status = tmp_path / "status.json"

    result = run(BACKUP, env=backup_env(tmp_path, status))

    assert result.returncode != 0, "a dump that never left the host reported success"
    recorded = read_status(status)
    assert recorded["outcome"] == "failure"
    assert recorded["failed_stage"] == "upload"
    assert not recorded["last_success_at"], "an unshipped dump advanced the last success"


@needs_shell
def test_a_run_that_reaches_the_destination_is_a_success(tmp_path: Path) -> None:
    """The other half: everything worked, so the recovery point moves."""
    stub_postgres(tmp_path)
    stub_aws(tmp_path)
    status = tmp_path / "status.json"

    result = run(BACKUP, env=backup_env(tmp_path, status))

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = read_status(status)
    assert recorded["outcome"] == "success"
    assert recorded["last_success_at"], "a complete run recorded no success"
    assert recorded["last_success_artifact"].endswith(".dump")
    assert recorded["destination"] == "s3"


@needs_shell
def test_a_failed_upload_leaves_the_local_artifact_for_a_manual_recovery(tmp_path: Path) -> None:
    """The dump is still the best thing available; deleting it would be worse."""
    stub_postgres(tmp_path)
    stub_aws(tmp_path, fail_on="s3")
    status = tmp_path / "status.json"

    run(BACKUP, env=backup_env(tmp_path, status))

    staged = list((tmp_path / "backups").glob("*.dump"))
    assert staged, "the failed run threw away the only copy it had"


@needs_shell
def test_a_failed_run_never_prunes_the_last_good_backup(tmp_path: Path) -> None:
    """Retention runs after the upload, so a failure cannot reach it."""
    stub_postgres(tmp_path)
    stub_aws(tmp_path, fail_on="s3")
    status = tmp_path / "status.json"
    backups = tmp_path / "backups"
    backups.mkdir()
    survivor = backups / "wasla-20260101T000000Z.dump"
    survivor.write_bytes(b"PGDMP the last good one")
    old = 1_700_000_000
    os.utime(survivor, (old, old))

    run(BACKUP, env=backup_env(tmp_path, status))

    assert survivor.exists(), "a failed run deleted the last good backup"
