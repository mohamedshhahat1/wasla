"""The kept-data stage's mutation matrix (F-1, M-K01..M-K10) and its regression lane.

    python -m scripts.verification.run_kept_data_mutations                 # everything
    python -m scripts.verification.run_kept_data_mutations M-K06 M-K07     # control + these
    python -m scripts.verification.run_kept_data_mutations --no-sweep ...  # killers only

Run from the repository root with `TEST_DATABASE_URL` naming a disposable
database and `WASLA_TEST_REDIS_HOST` / `WASLA_TEST_REDIS_URL` a disposable Redis.

M-K01..M-K05 break the rewritten AI-allowance invariants, M-K09 the scoped
observability assertion, M-K10 the kept-data runner's selection. M-K06..M-K08
break **the application** - charging an empty answer, charging at engagement,
a sweeper that never expires a hold - and for those the evidence is twofold:
the named application test fails, *and* the full kept-data sweep run at the
mutant counts the rows ENT-02 forbids (B6, B2/B6, A). That second half is the
point of the stage: the sweep is the net that catches the application, not only
hand-inserted rows.

The rules are the omnichannel runner's (`scripts/run_omnichannel_mutations.py`),
with one stricter: a control run first; one textual change per edit, matched
exactly once; every file restored byte for byte (SHA-256) and `git status`
unchanged; bytecode off; and **KILLED only when every named killer test is
among the failures** - a run that failed for some other reason is `wrong-kill`,
and one that never reached a test is `invalid`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

AI_INVARIANTS = "tests/integration/test_ai_invariants.py"
CHARGING = "tests/integration/test_ai_turn_charging.py"
OBSERVABILITY = "tests/integration/test_entitlement_observability.py"
RUNNER_TEST = "tests/unit/test_kept_data_sweep.py"

ORCHESTRATOR = "app/agents/orchestrator.py"
AI_WORKER = "app/workers/ai_worker.py"
TURNS = "app/repositories/agent_turn_repository.py"
ENTITLEMENTS = "app/services/entitlement_service.py"
SWEEP_SCRIPT = "scripts/kept_data_sweep.py"

STRANDED = "AI turn holds nothing settled, released or expired within the TTL and a sweep"
INCONSISTENT = "customer turns whose charge disagrees with their outcome or usage"


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutant:
    identifier: str
    removes: str
    edits: tuple[Edit, ...]
    killers: tuple[str, ...]
    #: Run the killers with `WASLA_TEST_KEEP_AI_DATA=1`, as the kept-data job does.
    kept: bool = False
    #: Also run the whole kept-data sweep at the mutant, and which checks must count.
    sweep_counts: tuple[str, ...] = field(default=())


CHARGE_EMPTY_ANSWER = Edit(
    ORCHESTRATOR,
    "        if ending is TurnOutcome.HANDED_OFF:\n",
    "        if ending in (TurnOutcome.HANDED_OFF, TurnOutcome.EMPTY_RESPONSE):\n",
)
CHARGE_AT_ENGAGEMENT = Edit(
    AI_WORKER,
    "                return _Reservation.LOST\n            return _Reservation.RESERVED\n",
    "                return _Reservation.LOST\n"
    "            mutant_turn = await AgentTurnRepository(\n"
    "                reservation, tenant_id=job.tenant_id\n"
    "            ).id_for(trigger_message_id=trigger_message_id)\n"
    "            assert mutant_turn is not None\n"
    "            await AITurnCharge(reservation, tenant_id=job.tenant_id).settle(\n"
    "                agent_turn_id=mutant_turn, chargeable=True\n"
    "            )\n"
    "            return _Reservation.RESERVED\n",
)

MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "M-K01",
        "F-1b no longer checks B1 (a turn charged twice, or charged with no event)",
        (
            Edit(
                AI_INVARIANTS,
                "     WHERE t.charge_state = 'charged' AND coalesce(c.charges, 0) <> 1\n",
                "     WHERE false\n",
            ),
        ),
        (
            f"{AI_INVARIANTS}::test_a_turn_charged_twice_is_counted",
            f"{AI_INVARIANTS}::test_a_charged_turn_without_its_usage_event_is_counted",
        ),
    ),
    Mutant(
        "M-K02",
        "F-1b treats every released turn as consistent whatever its usage events (B2)",
        (
            Edit(
                AI_INVARIANTS,
                "     WHERE t.charge_state IS DISTINCT FROM 'charged'\n",
                "     WHERE false\n",
            ),
        ),
        (f"{AI_INVARIANTS}::test_a_released_turn_with_a_usage_event_is_counted",),
    ),
    Mutant(
        "M-K03",
        "F-1b drops B6 (a never-charged ending, or no ending, that was charged)",
        (
            Edit(
                AI_INVARIANTS,
                "       AND (t.outcome::text IN {NEVER_CHARGED} OR t.outcome IS NULL)\n",
                "       AND false\n",
            ),
        ),
        (
            f"{AI_INVARIANTS}::test_a_provider_failure_that_was_charged_is_counted",
            f"{AI_INVARIANTS}::test_an_empty_answer_that_was_charged_is_counted",
        ),
    ),
    Mutant(
        "M-K04",
        "F-1a explains every open hold (A3 always true)",
        (
            Edit(
                AI_INVARIANTS,
                "     WHERE (t.charge_state = 'held'\n",
                "     WHERE false AND (t.charge_state = 'held'\n",
            ),
        ),
        (f"{AI_INVARIANTS}::test_a_hold_nothing_settled_released_or_expired_is_counted",),
    ),
    Mutant(
        "M-K05",
        "F-1a's threshold ignores the hold TTL (a fixed 900 s again)",
        (
            Edit(
                AI_INVARIANTS,
                "           < now() - make_interval(secs => :hold_cutoff_seconds)\n",
                "           < now() - interval '900 seconds'\n",
            ),
        ),
        (f"{AI_INVARIANTS}::test_a_hold_past_its_ttl_but_within_one_sweep_is_not_yet_stranded",),
    ),
    Mutant(
        "M-K06",
        "the application charges an empty answer (M-E02's change)",
        (CHARGE_EMPTY_ANSWER,),
        (f"{CHARGING}::test_an_empty_answer_is_not_charged",),
        sweep_counts=(INCONSISTENT,),
    ),
    Mutant(
        "M-K07",
        "the application charges at engagement instead of at success (M-E01's change)",
        (CHARGE_AT_ENGAGEMENT,),
        (f"{CHARGING}::test_a_provider_failure_is_not_charged_and_gives_its_hold_back",),
        sweep_counts=(INCONSISTENT,),
    ),
    Mutant(
        "M-K08",
        "the hold-expiry sweeper never releases an expired hold",
        (Edit(TURNS, "        if not claimed:\n", "        if claimed or not claimed:\n"),),
        (f"{CHARGING}::test_an_expired_hold_stops_counting_and_is_released_by_the_sweep",),
        sweep_counts=(STRANDED,),
    ),
    Mutant(
        "M-K09",
        "F-1c's assertion reads the process-wide counter again",
        (
            Edit(
                OBSERVABILITY,
                '        _labels(outcome="hold_expired"): float(len(swept)),\n',
                '        _labels(outcome="hold_expired"): 1.0,\n',
            ),
        ),
        (f"{OBSERVABILITY}::test_an_expired_hold_and_its_late_charge_are_counted",),
        kept=True,
    ),
    Mutant(
        "M-K10",
        "the kept-data runner drops one selected file",
        (
            Edit(
                SWEEP_SCRIPT,
                "    return [suite for suite in suites if EXCLUDED not in suite] + list(SWEEPS)\n",
                "    return [kept for kept in suites if EXCLUDED not in kept][1:] + list(SWEEPS)\n",
            ),
        ),
        (f"{RUNNER_TEST}::test_the_runner_selects_exactly_what_ci_selects",),
    ),
    # Regression lane: the entitlements stage's mutants, which must stay killed.
    Mutant(
        "M-E01",
        "charge at engagement again (regression; the edit M-K07 makes)",
        (CHARGE_AT_ENGAGEMENT,),
        (f"{CHARGING}::test_a_provider_failure_is_not_charged_and_gives_its_hold_back",),
    ),
    Mutant(
        "M-E02",
        "settle charges an empty provider response (regression; the edit M-K06 makes)",
        (CHARGE_EMPTY_ANSWER,),
        (f"{CHARGING}::test_an_empty_answer_is_not_charged",),
    ),
    Mutant(
        "M-E08",
        "expired holds keep counting (regression)",
        (Edit(ENTITLEMENTS, "            .where(AgentTurn.held_at > now - self._hold_ttl)\n", ""),),
        (f"{CHARGING}::test_an_expired_hold_stops_counting_and_is_released_by_the_sweep",),
    ),
)

_INVALID = (
    "ERROR collecting",
    "SyntaxError",
    "IndentationError",
    "ImportError while importing",
    "found no collectors",
    "no tests ran",
)


def _git_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607 - git on PATH, like CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _apply(source: str, edit: Edit, identifier: str) -> str:
    newline = "\r\n" if "\r\n" in source else "\n"
    old = edit.old.replace("\n", newline)
    new = edit.new.replace("\n", newline)
    found = source.count(old)
    if found != 1:
        raise ValueError(f"{identifier}: target in {edit.path} matched {found} times, not once")
    return source.replace(old, new)


def _environment(*, kept: bool) -> dict[str, str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if kept:
        environment["WASLA_TEST_KEEP_AI_DATA"] = "1"
    else:
        environment.pop("WASLA_TEST_KEEP_AI_DATA", None)
    return environment


def _pytest(tests: tuple[str, ...], *, kept: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and test selectors
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-markers --strict-config",
            "-q",
            "-p",
            "no:cacheprovider",
            *tests,
        ],
        cwd=ROOT,
        env=_environment(kept=kept),
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )


def _sweep() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "scripts.kept_data_sweep"],
        cwd=ROOT,
        env=_environment(kept=False),
        text=True,
        capture_output=True,
        timeout=3600,
        check=False,
    )


def _verdict(mutant: Mutant, result: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    output = result.stdout + result.stderr
    if result.returncode == 0:
        return "survived", ""
    if any(marker in output for marker in _INVALID):
        return "invalid", next((line for line in output.splitlines() if "Error" in line), "")
    failed = [line for line in output.splitlines() if line.startswith("FAILED ")]
    missing = [
        killer
        for killer in mutant.killers
        if not any(killer.split("::")[-1] in line for line in failed)
    ]
    raised = next((line for line in output.splitlines() if line.startswith("E ")), "")
    if missing:
        return "wrong-kill", f"named killers did not fail: {missing} || {failed[:3]}"
    return "killed", f"{failed[0]} || {raised.strip()}"


def _counted(output: str, checks: tuple[str, ...]) -> dict[str, str]:
    """What each check reported in the sweep's failure lines: the count and its rows."""
    found: dict[str, str] = {}
    for check in checks:
        line = next((x for x in output.splitlines() if f"AssertionError: {check}:" in x), "")
        found[check] = line.split(f"{check}:", 1)[1].strip() if line else ""
    return found


def control(mutants: tuple[Mutant, ...]) -> dict[str, object]:
    normal = tuple(dict.fromkeys(k for m in mutants if not m.kept for k in m.killers))
    kept = tuple(dict.fromkeys(k for m in mutants if m.kept for k in m.killers))
    results = [_pytest(normal, kept=False)] if normal else []
    if kept:
        results.append(_pytest(kept, kept=True))
    return {
        "id": "control",
        "tests": len(normal) + len(kept),
        "passed": all(result.returncode == 0 for result in results),
        "summary": [(r.stdout.strip().splitlines() or [""])[-1] for r in results],
    }


def control_sweep() -> dict[str, object]:
    result = _sweep()
    return {
        "id": "control-sweep",
        "passed": result.returncode == 0,
        "summary": (result.stdout.strip().splitlines() or [""])[-1],
    }


def run(mutant: Mutant, *, sweep: bool) -> dict[str, object]:
    before = _git_status()
    originals: dict[str, bytes] = {}
    for edit in mutant.edits:
        if edit.path not in originals:
            originals[edit.path] = (ROOT / edit.path).read_bytes()
    altered = {name: content.decode("utf-8") for name, content in originals.items()}
    for edit in mutant.edits:
        altered[edit.path] = _apply(altered[edit.path], edit, mutant.identifier)
    result: subprocess.CompletedProcess[str] | None = None
    swept: subprocess.CompletedProcess[str] | None = None
    try:
        for name, source in altered.items():
            (ROOT / name).write_bytes(source.encode("utf-8"))
        result = _pytest(mutant.killers, kept=mutant.kept)
        if sweep and mutant.sweep_counts:
            swept = _sweep()
    finally:
        for name, content in originals.items():
            (ROOT / name).write_bytes(content)
    restored = all(
        hashlib.sha256((ROOT / name).read_bytes()).digest() == hashlib.sha256(content).digest()
        for name, content in originals.items()
    )
    clean = _git_status() == before
    if not (restored and clean):
        raise RuntimeError(f"{mutant.identifier}: the source was not restored exactly")
    if result is None:
        raise RuntimeError(f"{mutant.identifier}: the tests did not run")
    verdict, evidence = _verdict(mutant, result)
    outcome: dict[str, object] = {
        "id": mutant.identifier,
        "removes": mutant.removes,
        "files": sorted(originals),
        "verdict": verdict,
        "evidence": re.sub(r"\s+", " ", evidence)[:500],
        "restored": restored and clean,
    }
    if swept is not None:
        output = swept.stdout + swept.stderr
        counted = _counted(output, mutant.sweep_counts)
        outcome["sweep"] = (swept.stdout.strip().splitlines() or [""])[-1]
        outcome["sweep_counted"] = {k: v[:600] for k, v in counted.items()}
        if verdict == "killed" and not all(counted.values()):
            outcome["verdict"] = "sweep-missed"
    return outcome


def main(argv: list[str]) -> int:
    sweep = "--no-sweep" not in argv
    selected = {argument for argument in argv if not argument.startswith("--")}
    chosen = tuple(m for m in MUTANTS if not selected or m.identifier in selected)
    baseline = control(chosen)
    print(json.dumps(baseline), flush=True)  # noqa: T201 - CLI output
    if not baseline["passed"]:
        return 2
    if sweep and any(m.sweep_counts for m in chosen):
        swept = control_sweep()
        print(json.dumps(swept), flush=True)  # noqa: T201 - CLI output
        if not swept["passed"]:
            return 2
    verdicts: dict[str, int] = {}
    for mutant in chosen:
        try:
            outcome = run(mutant, sweep=sweep)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            outcome = {"id": mutant.identifier, "verdict": "error", "evidence": str(error)}
        verdicts[str(outcome["verdict"])] = verdicts.get(str(outcome["verdict"]), 0) + 1
        print(json.dumps(outcome, ensure_ascii=False), flush=True)  # noqa: T201 - CLI output
    print(json.dumps({"summary": verdicts, "mutants": len(chosen)}), flush=True)  # noqa: T201
    return 0 if verdicts.get("killed", 0) == len(chosen) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
