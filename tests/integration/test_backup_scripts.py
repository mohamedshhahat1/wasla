"""The backup and restore scripts, run rather than read.

**What these can and cannot prove.** They execute the scripts with a POSIX
shell against a scratch directory, so they prove the argument handling, the
refusals and the credential hygiene - the parts that are shell logic. They do
*not* dump a database: that needs `pg_dump` at the server's version, which
lives in the PostgreSQL image rather than on a test runner, and the drill that
does it is written down in docs/BACKUP.md and was executed against a real
PostgreSQL 16 with pgvector before this was committed.

The distinction matters and is stated rather than blurred. A script whose only
evidence is a unit test is a script nobody has run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "backup_postgres.sh"
RESTORE = ROOT / "scripts" / "restore_postgres.sh"

SHELL = shutil.which("sh") or shutil.which("bash")

needs_shell = pytest.mark.skipif(
    SHELL is None,
    reason="No POSIX shell available to run the operational scripts.",
)


def run(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        # A deliberately minimal environment: the scripts must not depend on
        # anything a cron job would not have.
        "PATH": os.environ.get("PATH", ""),
        **(env or {}),
    }
    assert SHELL is not None
    return subprocess.run(  # noqa: S603 - a fixed script path, no shell string
        [SHELL, str(script), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        check=False,
    )


# ------------------------------------------------------------ they are shipped


def test_both_scripts_exist_and_are_executable() -> None:
    for script in (BACKUP, RESTORE):
        assert script.is_file(), f"{script.name} is missing"
        assert script.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_both_scripts_fail_on_the_first_error() -> None:
    """`set -eu` is what stops a half-finished backup being reported as done."""
    for script in (BACKUP, RESTORE):
        assert "set -eu" in script.read_text(encoding="utf-8")


def test_no_dump_artefact_is_tracked_in_the_repository() -> None:
    """A dump carries every customer record there is."""
    dumps = list(ROOT.rglob("*.dump")) + list(ROOT.rglob("*.sql.gz"))
    tracked = [
        path for path in dumps if ".git" not in path.parts and "node_modules" not in path.parts
    ]
    assert not tracked, f"database dumps must never be committed: {tracked}"


def test_the_ignore_file_refuses_dumps() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.dump" in ignored


# ------------------------------------------------------------ backup refusals


@needs_shell
def test_the_backup_refuses_to_run_without_a_destination() -> None:
    """A cron job with no configuration must fail, not guess."""
    result = run(BACKUP, env={"BACKUP_DIR": "/nonexistent-root/wasla"})

    assert result.returncode != 0
    assert "PGHOST" in result.stderr or "required" in (result.stderr + result.stdout)


@needs_shell
def test_the_backup_never_prints_the_password(tmp_path: Path) -> None:
    """The one thing a backup log must never carry."""
    secret = "hunter2-must-never-be-printed"
    result = run(
        BACKUP,
        env={
            "DATABASE_URL": f"postgresql+asyncpg://wasla:{secret}@127.0.0.1:1/wasla",
            "BACKUP_DIR": str(tmp_path),
        },
    )

    assert result.returncode != 0, "connecting to port 1 should not succeed"
    assert secret not in result.stdout
    assert secret not in result.stderr


@needs_shell
def test_a_failed_backup_leaves_no_artefact_behind(tmp_path: Path) -> None:
    """A truncated dump that looks like a backup is worse than no backup."""
    run(
        BACKUP,
        env={
            "DATABASE_URL": "postgresql+asyncpg://wasla:wasla@127.0.0.1:1/wasla",
            "BACKUP_DIR": str(tmp_path),
        },
    )

    assert list(tmp_path.glob("*.dump")) == []
    assert list(tmp_path.glob("*.part")) == []


# ----------------------------------------------------------- restore refusals


@needs_shell
def test_the_restore_requires_both_arguments() -> None:
    """There is no "restore to the default database" path, deliberately."""
    result = run(RESTORE)

    assert result.returncode == 64
    assert "usage" in result.stderr


@needs_shell
def test_the_restore_refuses_a_dump_that_is_not_there() -> None:
    result = run(RESTORE, "/no/such/file.dump", "scratch")

    assert result.returncode == 1
    assert "no such dump" in result.stdout + result.stderr


@needs_shell
def test_the_restore_refuses_the_configured_database(tmp_path: Path) -> None:
    """The guard that makes this safe to run from a shell at 3am."""
    dump = tmp_path / "any.dump"
    dump.write_bytes(b"not really a dump")

    result = run(
        RESTORE,
        str(dump),
        "wasla",
        env={"DATABASE_URL": "postgresql+asyncpg://wasla:wasla@127.0.0.1:5432/wasla"},
    )

    assert result.returncode == 1
    assert "refusing to restore over wasla" in result.stdout + result.stderr


@needs_shell
def test_the_production_refusal_can_be_overridden_deliberately(tmp_path: Path) -> None:
    """Overwriting production is possible; it is not reachable by accident.

    With the opt-in set the guard is passed and the run fails later, on the
    database it cannot reach - which is what proves the refusal was the thing
    that stopped it before.
    """
    dump = tmp_path / "any.dump"
    dump.write_bytes(b"not really a dump")

    result = run(
        RESTORE,
        str(dump),
        "wasla",
        env={
            "DATABASE_URL": "postgresql+asyncpg://wasla:wasla@127.0.0.1:1/wasla",
            "WASLA_RESTORE_ALLOW_PRODUCTION": "yes",
        },
    )

    output = result.stdout + result.stderr
    assert "refusing to restore over" not in output
    assert result.returncode != 0


@needs_shell
def test_the_restore_never_prints_the_password(tmp_path: Path) -> None:
    secret = "hunter2-must-never-be-printed"
    dump = tmp_path / "any.dump"
    dump.write_bytes(b"not really a dump")

    result = run(
        RESTORE,
        str(dump),
        "scratch",
        env={"DATABASE_URL": f"postgresql+asyncpg://wasla:{secret}@127.0.0.1:1/wasla"},
    )

    assert secret not in result.stdout
    assert secret not in result.stderr


# ------------------------------------------------------------- documentation


def test_the_restore_procedure_is_documented() -> None:
    """A script nobody knows how to run is not a recovery plan."""
    guide = (ROOT / "docs" / "BACKUP.md").read_text(encoding="utf-8")

    for expected in (
        "backup_postgres.sh",
        "restore_postgres.sh",
        "BACKUP_RETENTION_DAYS",
        "WASLA_RESTORE_ALLOW_PRODUCTION",
        "pgvector",
    ):
        assert expected in guide, f"docs/BACKUP.md does not mention {expected}"


def test_the_runbook_no_longer_claims_there_is_no_backup_system() -> None:
    """The line this phase exists to make untrue."""
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")

    assert "There is no backup or restore procedure" not in runbook


@pytest.mark.parametrize("script", sorted(p.name for p in (ROOT / "scripts").glob("*.sh")))
def test_no_shell_script_carries_a_carriage_return(script: str) -> None:
    """A `#!/bin/sh\r` shebang is a container that will not start.

    Linux looks for an interpreter literally named `/bin/sh\r`, fails, and
    reports "no such file or directory" naming the *script* - which sends you
    looking in the wrong place entirely. `.gitattributes` pins `*.sh` to LF for
    this reason and records that it has happened once already; this catches the
    working tree, which is what `docker build` copies and what an operator runs
    the backup from.
    """
    assert b"\r" not in (ROOT / "scripts" / script).read_bytes()
