"""The local kept-data runner selects exactly what CI's kept-data step selects (KD-04, F-1).

`ci.yml` keeps its own shell rule and `scripts/kept_data_sweep.py` applies it in
Python. This parses the step out of the workflow, evaluates its rule against the
tree independently of the script, and requires the three to agree: the
workflow's rule, the files it names after it, and the script's selection. A
change to either side - a file dropped by the script, a rule edited in the
workflow - fails here, not silently in a sweep that no longer tests what CI does.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import yaml

from scripts import kept_data_sweep

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The only shape of rule this test can evaluate. Anything else in the step is a
# change to the selection somebody has to look at, so it fails rather than
# being guessed at.
RULE = re.compile(
    r'SUITES=\$\(grep -l "(?P<marker>[^"]+)" (?P<glob>\S+)'  # the files that mention it
    r" \| grep -v (?P<excluded>\S+)\)"  # less the sweeps themselves
)


def _kept_step() -> tuple[dict[str, Any], dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    found = [
        (job, step)
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if (step.get("env") or {}).get("WASLA_TEST_KEEP_AI_DATA") == "1"
    ]
    assert len(found) == 1, f"expected one kept-data step in ci.yml, found {len(found)}"
    return found[0]


def _ci_selection() -> list[str]:
    _job, step = _kept_step()
    script = step["run"]
    rule = RULE.search(script)
    assert rule is not None, "the kept-data step's selection rule changed shape"
    marker, pattern, excluded = rule["marker"], rule["glob"], rule["excluded"]
    # `grep -l` over the shell's glob order (byte order on the runner's locale),
    # then `grep -v` on the path.
    matched = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob(pattern)
        if marker in path.read_text(encoding="utf-8")
    )
    suites = [name for name in matched if excluded not in name]
    command = script[script.index("pytest") :].split("|")[0].replace("\\\n", " ")
    after = shlex.split(command)[shlex.split(command).index("$SUITES") + 1 :]
    return suites + [token for token in after if token.endswith(".py")]


def test_the_runner_selects_exactly_what_ci_selects() -> None:
    ci = _ci_selection()
    assert kept_data_sweep.selection() == ci
    # Non-vacuity: the rule really matched the suites the sweep exists for.
    assert len(ci) > 20
    assert "tests/integration/test_ai_turn_charging.py" in ci
    assert ci[-2:] == list(kept_data_sweep.SWEEPS)


def test_the_runner_builds_the_schema_the_job_builds() -> None:
    job, _step = _kept_step()
    assert job["env"]["WASLA_TEST_SCHEMA"] == kept_data_sweep.SCHEMA


def test_the_runner_fails_on_a_skip_like_the_job() -> None:
    _job, step = _kept_step()
    assert 'grep -q "^SKIPPED"' in step["run"]
    skipped = kept_data_sweep.SweepResult(
        collected=3, passed=2, failed=0, errors=0, skipped=1, exit_code=0
    )
    assert not skipped.green
    assert kept_data_sweep.SweepResult(3, 3, 0, 0, 0, 0).green
