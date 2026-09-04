"""Documentation claims that a machine can check, and only those.

An audit found four status lines that had gone stale behind the code, and a
fresh pass over the whole set found sixteen. Every one of them was a sentence
somebody would have had to notice was wrong, which is a review asking a person
to hold two files in their head - and a review does that badly.

**These are structural assertions, not snapshots.** Prose is not tested here and
should not be: a test that pinned a paragraph would fail on every improvement to
the wording and teach the next person to stop editing documentation. What is
pinned is the small set of claims with a machine-readable counterpart - a
migration range, a list of worker kinds - where "the code says X and the
document says Y" is a fact rather than a judgement.

A status-vocabulary check was written and removed: several documents carry
deliberate prose in that position - "partially wired", "implemented,
unexecuted" - and a test that refused them would have been the tail wagging the
dog.

The rest of the documentation-truth problem is answered by
`test_deployment_configuration.py`, which holds `Settings` against
`.env.example` and both Compose files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.workers.runner import ALL_KINDS

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "ARCHITECTURE.md"
MIGRATIONS = ROOT / "alembic" / "versions"


def _migration_head() -> str:
    """The highest revision on disk, read the way an operator would."""
    revisions = sorted(
        match.group(1)
        for path in MIGRATIONS.glob("*.py")
        if (match := re.search(r"_(\d{4})_", path.name))
    )
    assert revisions, "no migrations found; this test is looking in the wrong place"
    return revisions[-1]


def test_the_readme_migration_range_ends_at_the_current_head() -> None:
    """`0001`-`0037` sat in the README while the head was `0041`.

    Harmless on its own and corrosive in aggregate: a reader who finds one
    number stale stops trusting the others, and the README's whole job is to be
    the thing somebody trusts first.
    """
    text = README.read_text(encoding="utf-8")
    match = re.search(r"migrations \(`(\d{4})`.`(\d{4})`\)", text)
    assert match is not None, "the README no longer states a migration range"

    first, last = match.groups()
    assert first == "0001"
    assert last == _migration_head()


def test_every_worker_kind_is_described_in_the_architecture() -> None:
    """The tree in ARCHITECTURE named four loops when the runner ran ten.

    An operator reading it to work out what `WORKER_KINDS` accepts would have
    got the wrong answer, and the six missing ones included the reaper that
    recovers crashed jobs.
    """
    text = ARCHITECTURE.read_text(encoding="utf-8")
    missing = sorted(kind for kind in ALL_KINDS if kind not in text)
    assert not missing, f"ARCHITECTURE.md does not mention worker kinds: {', '.join(missing)}"


@pytest.mark.parametrize(
    ("subject", "phrase"),
    [
        # Each of these was a live claim in the documentation while the code
        # said otherwise. They are pinned as *absences* rather than as
        # replacement prose, so the wording stays free to improve.
        ("auth rate limiting", "rate limiting on authentication endpoints remains Planned"),
        ("tracing", "OpenTelemetry tracing and an error-monitoring provider remain Planned"),
        ("platform audit trail", "there is no audit log until Phase 14"),
        ("platform audit trail", "there is no audit log until phase 14"),
        ("workspace credentials", "until there is encryption at rest"),
        ("dunning", "overage pricing and dunning are Planned"),
    ],
)
def test_a_corrected_claim_has_not_come_back(subject: str, phrase: str) -> None:
    """The six sentences this audit corrected, held down by name.

    Narrow on purpose. A general "does the documentation match the code" test
    cannot be written, and one that tried would be a snapshot of prose. What can
    be written is "this specific sentence was wrong once", which is cheap and
    catches the revert.
    """
    for path in (README, ARCHITECTURE, ROOT / "TASKS.md", *sorted((ROOT / "docs").glob("*.md"))):
        assert phrase not in path.read_text(
            encoding="utf-8"
        ), f"{subject} claim is back in {path.name}"
