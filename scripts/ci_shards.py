"""Split CI's two long pytest runs into shards by file, and prove nothing fell out.

`ci.yml` used to run the model-built suite and the migration-built
integration/e2e suite as one pytest process each - one 62-minute step and one
35-minute step, which set the whole workflow's wall time. They are now matrix
jobs, each running a shard of the files this script assigns.

Three rules make that safe:

* **The split is deterministic.** The same tree, the same timings file and the
  same shard count always give the same files to the same shard, so a red
  shard is reproduced by running exactly what it ran
  (`python -m scripts.ci_shards files --suite model --shards 6 --index 3`).
* **It is balanced by measured time, not by file count.** Files are placed
  longest-first onto the least-loaded shard (LPT). Their durations come from
  `scripts/ci_test_durations.json`; a file the timings do not know yet - one
  added since they were recorded - is costed at the mean, so it is still run,
  merely placed less precisely. `record` refreshes the timings from the JUnit
  reports the shards upload.
* **Completeness is checked, not assumed.** `verify` collects the suite the way
  the unsharded run would have (same roots, same `--deselect`s) and requires
  the shards' JUnit reports to hold, for every test function, exactly the
  number of cases collected, all from one shard. A file the splitter missed, a
  shard that silently collected nothing, or a test run twice fails CI there.

Within a shard, files keep the order the unsharded run had them in (sorted by
path), so the only thing sharding changes is which files run *before* a given
file - and a test that depended on that was coupled to another file's leftovers,
which is a bug to report, not an order to restore.

Usage:
    python -m scripts.ci_shards files  --suite model --shards 6 --index 1
    python -m scripts.ci_shards plan   --suite model --shards 6
    python -m scripts.ci_shards verify --suite model --reports DIR [-- pytest args]
    python -m scripts.ci_shards record --suite model REPORT.xml [REPORT.xml ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMINGS = ROOT / "scripts" / "ci_test_durations.json"

#: What each CI run passes to pytest as its roots. `model` is the bare `pytest`
#: of the "Tests and migrations" job (`testpaths = ["tests"]`); `migrations` is
#: the migration-built job's `tests/integration tests/e2e`.
SUITES: dict[str, tuple[str, ...]] = {
    "model": ("tests",),
    "migrations": ("tests/integration", "tests/e2e"),
}

#: pytest's default `python_files`. `pyproject.toml` does not override it, and
#: `tests/unit/test_delivery_pipeline.py` holds it to that.
TEST_FILE_GLOB = "test_*.py"


def suite_files(suite: str, root: Path = ROOT) -> list[str]:
    """Every test file the unsharded run of `suite` collects from, in its order."""
    found: set[str] = set()
    for base in SUITES[suite]:
        for path in (root / base).rglob(TEST_FILE_GLOB):
            found.add(path.relative_to(root).as_posix())
    return sorted(found)


def load_timings(suite: str, path: Path = TIMINGS) -> dict[str, float]:
    document = json.loads(path.read_text(encoding="utf-8"))
    timings: dict[str, float] = document["suites"][suite]
    return timings


def costs(files: Sequence[str], timings: dict[str, float]) -> dict[str, float]:
    """Each file's expected seconds; unknown files are costed at the known mean."""
    known = [timings[name] for name in files if name in timings]
    default = sum(known) / len(known) if known else 1.0
    return {name: timings.get(name, default) for name in files}


def assign(files: Sequence[str], cost: dict[str, float], shards: int) -> list[list[str]]:
    """Longest-processing-time-first onto the least-loaded shard.

    Ties are broken by path and then by shard index, so the result depends on
    nothing but its inputs. Each shard's files come back in suite order.
    """
    if shards < 1:
        raise ValueError("at least one shard")
    loads = [0.0] * shards
    buckets: list[list[str]] = [[] for _ in range(shards)]
    for name in sorted(files, key=lambda item: (-cost[item], item)):
        target = min(range(shards), key=lambda index: (loads[index], index))
        buckets[target].append(name)
        loads[target] += cost[name]
    order = {name: position for position, name in enumerate(files)}
    return [sorted(bucket, key=order.__getitem__) for bucket in buckets]


def shard(suite: str, shards: int, index: int, root: Path = ROOT) -> list[str]:
    """The files shard `index` (1-based) of `shards` runs."""
    if not 1 <= index <= shards:
        raise ValueError(f"shard index {index} is outside 1..{shards}")
    files = suite_files(suite, root)
    buckets = assign(files, costs(files, load_timings(suite)), shards)
    return buckets[index - 1]


# --- completeness -----------------------------------------------------------


def junit_address(nodeid: str) -> tuple[str, str]:
    """A node id as pytest's JUnit writer records it: (classname, name).

    The same mangling `_pytest.junitxml.mangle_test_address` applies, written
    out here rather than imported from a private module.
    """
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def reported(report: Path) -> list[tuple[str, str]]:
    tree = ElementTree.parse(report)  # noqa: S314 - written by a CI shard of this run
    return [(case.get("classname", ""), case.get("name", "")) for case in tree.iter("testcase")]


def _function(case: tuple[str, str]) -> tuple[str, str]:
    """A test case's function: its address with any parametrize id dropped."""
    classname, name = case
    return classname, name.partition("[")[0]


class _Collector:
    def __init__(self) -> None:
        self.nodeids: list[str] = []

    def pytest_collection_finish(self, session: object) -> None:
        self.nodeids = [item.nodeid for item in session.items]  # type: ignore[attr-defined]


def collected(suite: str, extra: Sequence[str]) -> list[str]:
    """The node ids an unsharded run of `suite` would execute."""
    import pytest

    collector = _Collector()
    code = pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", *SUITES[suite], *extra],
        plugins=[collector],
    )
    if code != 0:
        raise SystemExit(f"collecting {suite} failed with exit status {code}")
    return collector.nodeids


def compare(expected: Iterable[str], shard_reports: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Problems with the shards' reports against the expected node ids; empty is good.

    Cases are counted per test *function* rather than matched by full id,
    because a few parametrize ids are not the same in two processes (an id
    built from zip bytes that carry a timestamp, or from a fresh UUID), so the
    collecting process and the shard name the same case differently. Per
    function that is still exact for a split by file: a case that did not run
    lowers its function's count, a file run by two shards doubles it, and a
    function is also required to have run in one shard only.
    """
    wanted = Counter(_function(junit_address(nodeid)) for nodeid in expected)
    problems: list[str] = []
    ran: Counter[tuple[str, str]] = Counter()
    where: dict[tuple[str, str], set[str]] = {}
    for name, cases in sorted(shard_reports.items()):
        if not cases:
            problems.append(f"{name} reported no tests at all")
        for case in cases:
            function = _function(case)
            ran[function] += 1
            where.setdefault(function, set()).add(name)
    for function in sorted(wanted.keys() | ran.keys()):
        label = "::".join(function)
        if len(where.get(function, ())) > 1:
            problems.append(f"{label} ran in more than one shard: {sorted(where[function])}")
        if ran[function] != wanted[function]:
            problems.append(
                f"{label}: {wanted[function]} cases collected and {ran[function]} ran in the shards"
            )
    return problems


# --- timings ----------------------------------------------------------------


def file_seconds(reports: Iterable[Path]) -> dict[str, float]:
    """Seconds per test file, summed over every test case in the reports."""
    totals: dict[str, float] = {}
    for report in reports:
        cases = ElementTree.parse(report).iter("testcase")  # noqa: S314 - CI shard reports
        for case in cases:
            classname = case.get("classname", "")
            module = classname.split(".")
            # tests.integration.test_x[.TestClass...] -> tests/integration/test_x.py
            depth = next(
                (i for i, part in enumerate(module) if part.startswith("test_")), len(module) - 1
            )
            name = "/".join(module[: depth + 1]) + ".py"
            totals[name] = totals.get(name, 0.0) + float(case.get("time", "0") or 0)
    return {name: round(seconds, 2) for name, seconds in sorted(totals.items())}


def write_timings(suite: str, seconds: dict[str, float], path: Path = TIMINGS) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["suites"][suite] = seconds
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- command line -------------------------------------------------------------


def _say(message: str) -> None:
    print(message)  # noqa: T201 - CLI output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ci_shards")
    commands = parser.add_subparsers(dest="command", required=True)

    files_cmd = commands.add_parser("files", help="print one shard's files")
    plan_cmd = commands.add_parser("plan", help="print every shard's expected seconds")
    for command in (files_cmd, plan_cmd):
        command.add_argument("--suite", choices=sorted(SUITES), required=True)
        command.add_argument("--shards", type=int, required=True)
    files_cmd.add_argument("--index", type=int, required=True)

    verify_cmd = commands.add_parser("verify", help="every collected test ran exactly once")
    verify_cmd.add_argument("--suite", choices=sorted(SUITES), required=True)
    verify_cmd.add_argument("--reports", type=Path, required=True)
    verify_cmd.add_argument("pytest_args", nargs=argparse.REMAINDER)

    record_cmd = commands.add_parser("record", help="refresh the timings from JUnit reports")
    record_cmd.add_argument("--suite", choices=sorted(SUITES), required=True)
    record_cmd.add_argument("reports", type=Path, nargs="+")

    args = parser.parse_args(argv)

    if args.command == "files":
        _say(" ".join(shard(args.suite, args.shards, args.index)))
        return 0

    if args.command == "plan":
        names = suite_files(args.suite)
        cost = costs(names, load_timings(args.suite))
        for number, bucket in enumerate(assign(names, cost, args.shards), start=1):
            seconds = sum(cost[name] for name in bucket)
            _say(f"shard {number}: {len(bucket):3d} files, {seconds:8.1f} s")
        return 0

    if args.command == "verify":
        extra = [arg for arg in args.pytest_args if arg != "--"]
        reports = {path.name: reported(path) for path in sorted(args.reports.rglob("*.xml"))}
        if not reports:
            _say(f"::error::no JUnit reports under {args.reports}")
            return 1
        expected = collected(args.suite, extra)
        problems = compare(expected, reports)
        if problems:
            _say(f"::error::the {args.suite} shards did not run the suite exactly once")
            _say("\n".join(problems))
            return 1
        _say(f"{len(expected)} tests, each run exactly once across {len(reports)} shards")
        return 0

    write_timings(args.suite, file_seconds(args.reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
