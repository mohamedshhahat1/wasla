"""CI's kept-data sweep, run locally exactly as `ci.yml` selects it (KD-04, F-1).

    python -m scripts.kept_data_sweep             # the sweep, as CI runs it
    python -m scripts.kept_data_sweep --list      # the selection, nothing run

Run from the repository root with `TEST_DATABASE_URL` naming a disposable
database - the suites drop its public schema - and `WASLA_TEST_REDIS_HOST` /
`WASLA_TEST_REDIS_URL` naming a disposable Redis.

**Why this exists.** The `migration-parity` job ends with a session of its own:
every integration suite built on the AI harness runs with
`WASLA_TEST_KEEP_AI_DATA=1`, so nothing it writes is deleted, and then
`test_ai_invariants.py` and `test_tool_invariants.py` sweep all of it. Two
implementation stages reported every lane green while this job was red (F-1),
because nobody ran it outside GitHub. This is that job, runnable by anybody.

**The selection is CI's, not a copy of it.** `ci.yml` keeps its own shell rule
(`grep -l "ai_harness" tests/integration/test_*.py | grep -v _invariants`,
then the two invariant files) and is not changed; `selection()` here applies
the same rule, and `tests/unit/test_kept_data_sweep.py` parses the job's
command out of `ci.yml` and proves the two agree. Drift on either side fails
that test rather than this sweep quietly testing something CI does not.

**What counts as a pass is CI's too:** pytest exits 0 *and* no test reported
`SKIPPED` - the job fails on any skip, because the two non-vacuity checks skip
outside kept mode and a skip there would mean the sweep proved nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: What `ci.yml` greps the suites for, and what it excludes from the match.
MARKER = "ai_harness"
EXCLUDED = "_invariants"
SUITE_GLOB = "tests/integration/test_*.py"
#: The sweeps, run last in the same session so they see everything kept.
SWEEPS = (
    "tests/integration/test_ai_invariants.py",
    "tests/integration/test_tool_invariants.py",
)
#: The job's schema: `migration-parity` builds it with `alembic upgrade head`.
SCHEMA = "migrations"


@dataclass(frozen=True, slots=True)
class SweepResult:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    exit_code: int

    @property
    def green(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.errors == 0 and not self.skipped


def selection(root: Path = ROOT) -> list[str]:
    """The files CI's kept-data step passes to pytest, in CI's order.

    `grep -l` lists the glob's files in the shell's order, which on the
    runner's `C.UTF-8` locale is byte order - `sorted` on the relative path.
    """
    suites = sorted(
        path.relative_to(root).as_posix()
        for path in root.glob(SUITE_GLOB)
        if MARKER in path.read_text(encoding="utf-8")
    )
    return [suite for suite in suites if EXCLUDED not in suite] + list(SWEEPS)


def _counts(report: Path, exit_code: int) -> SweepResult:
    suite = ElementTree.parse(report).getroot()  # noqa: S314 - written by this run
    if suite.tag == "testsuites":
        suite = suite[0]
    collected = int(suite.get("tests", "0"))
    failed = int(suite.get("failures", "0"))
    errors = int(suite.get("errors", "0"))
    skipped = int(suite.get("skipped", "0"))
    return SweepResult(
        collected=collected,
        passed=collected - failed - errors - skipped,
        failed=failed,
        errors=errors,
        skipped=skipped,
        exit_code=exit_code,
    )


def run(files: list[str], *, schema: str, extra: list[str]) -> SweepResult:
    """Run the sweep once, as the job does, and count what happened."""
    environment = {**os.environ, "WASLA_TEST_KEEP_AI_DATA": "1"}
    if schema == "migrations":
        environment["WASLA_TEST_SCHEMA"] = "migrations"
    else:
        environment.pop("WASLA_TEST_SCHEMA", None)
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "kept.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            f"--junitxml={report}",
            *extra,
            *files,
        ]
        completed = subprocess.run(  # noqa: S603 - fixed interpreter, files from the selection
            command, cwd=ROOT, env=environment, check=False
        )
        if not report.exists():
            return SweepResult(0, 0, 0, 1, 0, completed.returncode or 1)
        return _counts(report, completed.returncode)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.kept_data_sweep")
    parser.add_argument("--list", action="store_true", help="print the selection and stop")
    parser.add_argument(
        "--schema",
        choices=("migrations", "models"),
        default=SCHEMA,
        help="how the session builds its schema (CI: migrations)",
    )
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments, after --")
    arguments = parser.parse_args(argv)

    files = selection()
    for name in files:
        print(name)  # noqa: T201 - CLI output
    print(f"{len(files) - len(SWEEPS)} suites + {len(SWEEPS)} sweeps")  # noqa: T201
    if arguments.list:
        return 0
    if not os.environ.get("TEST_DATABASE_URL"):
        print(  # noqa: T201 - CLI output
            "TEST_DATABASE_URL is required: the suites drop the public schema of the "
            "database it names, so it must be a disposable one.",
            file=sys.stderr,
        )
        return 2

    result = run(files, schema=arguments.schema, extra=list(arguments.pytest_args))
    print(  # noqa: T201 - CLI output
        f"kept-data sweep ({arguments.schema}): collected {result.collected}, "
        f"passed {result.passed}, failed {result.failed}, errors {result.errors}, "
        f"skipped {result.skipped}, exit {result.exit_code}"
    )
    if result.skipped:
        message = "a test skipped in the kept-data sweep; CI fails on that"
        print(message, file=sys.stderr)  # noqa: T201 - CLI output
    return 0 if result.green else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
