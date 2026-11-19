"""The CI splitter places every test file exactly once, the same way every time.

`scripts/ci_shards.py` decides which files each CI shard runs. A file it drops
is a file CI stops running while staying green, so these pin the properties
that make sharding safe: every file lands in exactly one shard, the split
depends on nothing but the tree and the timings, an unknown file is still
placed, and the completeness check really fails when a shard lost a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from _pytest.junitxml import mangle_test_address

from scripts import ci_shards

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: Which ci.yml job runs which suite, sharded.
SHARDED_JOBS = {"tests": "model", "migration-suites": "migrations"}


def _shard_count(job: str) -> int:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    shards: list[int] = workflow["jobs"][job]["strategy"]["matrix"]["shard"]
    return len(shards)


@pytest.mark.parametrize(("job", "suite"), sorted(SHARDED_JOBS.items()))
def test_ci_shards_together_run_every_file_exactly_once(job: str, suite: str) -> None:
    count = _shard_count(job)
    buckets = [ci_shards.shard(suite, count, index) for index in range(1, count + 1)]
    placed = [name for bucket in buckets for name in bucket]

    assert sorted(placed) == ci_shards.suite_files(suite)
    assert len(placed) == len(set(placed)), "a file is in two shards"
    assert all(buckets), "a shard with no files"


def test_the_suites_are_the_roots_ci_ran_unsharded() -> None:
    files = ci_shards.suite_files("model")
    assert "tests/unit/test_ci_shards.py" in files
    assert "tests/real_provider/test_openai_contract.py" in files
    assert "tests/e2e/test_billing_lifecycle.py" in files
    migrations = ci_shards.suite_files("migrations")
    assert set(migrations) < set(files)
    assert all(name.startswith(("tests/integration/", "tests/e2e/")) for name in migrations)
    assert len(migrations) > 300


def test_the_split_depends_only_on_its_inputs() -> None:
    files = [f"tests/test_{letter}.py" for letter in "abcdefghij"]
    cost = {name: float(len(name) % 3 + 1) for name in files}

    first = ci_shards.assign(files, cost, 3)
    second = ci_shards.assign(list(reversed(files)), cost, 3)

    # Reversed input changes nothing but the order each shard is listed in.
    assert [sorted(bucket) for bucket in first] == [sorted(bucket) for bucket in second]
    assert ci_shards.assign(files, cost, 3) == first


def test_longest_files_are_spread_rather_than_stacked() -> None:
    files = ["a", "b", "c", "d", "e", "f"]
    cost = {"a": 10.0, "b": 9.0, "c": 8.0, "d": 3.0, "e": 2.0, "f": 1.0}

    buckets = ci_shards.assign(files, cost, 3)
    loads = sorted(sum(cost[name] for name in bucket) for bucket in buckets)

    assert loads == [11.0, 11.0, 11.0]


def test_a_file_the_timings_do_not_know_is_still_placed_at_the_mean() -> None:
    cost = ci_shards.costs(["known_a", "known_b", "new"], {"known_a": 10.0, "known_b": 30.0})

    assert cost["new"] == 20.0
    placed = [name for bucket in ci_shards.assign(list(cost), cost, 2) for name in bucket]
    assert sorted(placed) == sorted(cost)


def test_a_shard_index_outside_the_matrix_is_refused() -> None:
    with pytest.raises(ValueError):
        ci_shards.shard("model", 4, 0)
    with pytest.raises(ValueError):
        ci_shards.shard("model", 4, 5)


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/unit/test_x.py::test_plain",
        "tests/unit/test_x.py::TestGroup::test_method",
        "tests/integration/test_y.py::test_param[a::b-1.5]",
        "tests/integration/test_y.py::test_param[with/slash.py]",
    ],
)
def test_node_ids_are_mangled_exactly_as_the_junit_writer_does(nodeid: str) -> None:
    """The completeness check compares collected ids with JUnit reports; if the
    two manglings drifted, every test would look missing - or worse, matching."""
    names = mangle_test_address(nodeid)

    assert ci_shards.junit_address(nodeid) == (".".join(names[:-1]), names[-1])


def test_completeness_fails_on_a_missing_a_duplicated_or_a_stray_test() -> None:
    expected = [
        "tests/unit/test_x.py::test_a",
        "tests/unit/test_x.py::test_p[1]",
        "tests/unit/test_x.py::test_p[2]",
    ]
    a, p1, p2 = (ci_shards.junit_address(nodeid) for nodeid in expected)
    stray = ("tests.unit.test_x", "test_z")

    def problems(reports: dict[str, list[tuple[str, str]]]) -> str:
        return " / ".join(ci_shards.compare(expected, reports))

    assert problems({"1.xml": [a, p1, p2]}) == ""
    assert "1 ran" in problems({"1.xml": [a, p1]}), "a dropped case"
    assert "0 ran" in problems({"1.xml": [p1, p2]}), "a dropped function"
    assert "more than one shard" in problems({"1.xml": [a, p1, p2], "2.xml": [a]})
    assert "0 cases collected" in problems({"1.xml": [a, p1, p2, stray]})
    assert "no tests" in problems({"1.xml": [a, p1, p2], "2.xml": []})


def test_a_parametrize_id_that_differs_between_processes_still_counts() -> None:
    """Three suites build ids from bytes with a zip timestamp or a fresh UUID,
    so the collecting process and the shard name the same case differently."""
    expected = ["tests/unit/test_x.py::test_p[stamp-1]", "tests/unit/test_x.py::test_p[uuid-a]"]
    ran = [("tests.unit.test_x", "test_p[stamp-2]"), ("tests.unit.test_x", "test_p[uuid-b]")]

    assert ci_shards.compare(expected, {"1.xml": ran}) == []
    assert ci_shards.compare(expected, {"1.xml": ran[:1]}) != []


def test_timings_are_summed_per_file_from_junit_reports(tmp_path: Path) -> None:
    report = tmp_path / "shard.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.integration.test_x" name="test_a" time="1.5"/>'
        '<testcase classname="tests.integration.test_x.TestGroup" name="test_b" time="2.25"/>'
        '<testcase classname="tests.unit.test_y" name="test_c[p]" time="0.25"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    assert ci_shards.file_seconds([report]) == {
        "tests/integration/test_x.py": 3.75,
        "tests/unit/test_y.py": 0.25,
    }


def test_the_readme_reproduces_a_shard_with_ci_s_own_counts() -> None:
    """A reproduction command with the wrong shard count runs other files."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for job, suite in SHARDED_JOBS.items():
        assert f"--suite {suite} --shards {_shard_count(job)}" in readme, suite
