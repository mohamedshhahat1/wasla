"""Documentation claims that a machine can check, and only those.

An audit found four status lines that had gone stale behind the code, a fresh
pass over the whole set found sixteen, and a later one found twelve of twelve
mechanically-checkable claims stale - including the declared technical source
of truth stating that billing did not exist while it was taking card payments.
Every one of them was a sentence somebody would have had to notice was wrong,
which is a review asking a person to hold two files in their head - and a review
does that badly.

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

    # And the *count* in the prose beside the tree, because a kind whose name
    # happens to appear elsewhere in the document - "telemetry" inside
    # "OpenTelemetry" - satisfies the check above without being described.
    # ARCHITECTURE.md said six loops while ten ran.
    spelled = {
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        11: "eleven",
        12: "twelve",
    }
    counted = spelled.get(len(ALL_KINDS), str(len(ALL_KINDS)))
    assert f"{counted} loops" in text, f"ARCHITECTURE.md does not say {counted} loops"


def test_the_route_count_the_documentation_publishes_is_the_real_one() -> None:
    """`docs/API.md` states how many operations the production shape serves.

    Added because F-7's twelve stale claims were all *unfalsifiable from the
    outside*: a reviewer would have had to count 126 routes by hand to notice.
    A number the documentation states and a test derives is a claim that
    corrects itself.

    The trap this walks into deliberately: FastAPI 0.141 nests included routers
    behind `original_router`, so a flat search for `APIRoute` finds **zero** and
    every conclusion drawn from it is vacuously true. The assertion below the
    walk is what catches that, and it is the reason the walk is written out
    here rather than borrowed.
    """
    from fastapi.routing import APIRoute

    from app.core.config import Settings
    from app.main import create_app

    application = create_app(Settings(_env_file=None, environment="test", docs_enabled=False))

    def walk(router: object, found: list[APIRoute]) -> list[APIRoute]:
        for route in getattr(router, "routes", []):
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested, found)
            elif isinstance(route, APIRoute):
                found.append(route)
            else:
                walk(route, found)
        return found

    # `methods` is optional on the type even though an `APIRoute` always has
    # one, and an operation is a (path, method) pair rather than a route - a
    # route answering both GET and DELETE is two of them.
    operations = sum(len(route.methods or ()) for route in walk(application.router, []))

    assert operations > 100, "the router walk found nothing; the count below is meaningless"
    published = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    stated = {int(found) for found in re.findall(r"\*\*(\d{2,3}) operations\*\*", published)}
    assert stated, "docs/API.md no longer states an operation count"
    assert stated == {operations}, f"documented {sorted(stated)}, actual {operations}"


def test_the_environment_policy_table_covers_every_environment() -> None:
    """`docs/SECURITY.md` says what each tier may be lax about.

    F-5 and F-6 were one policy, written once, about one environment - so a
    tier nobody thought about got the weakest behaviour by default. The table
    is the corrected statement of it, and a fifth environment added to
    `Environment` and not to the table is the same mistake again.
    """
    from typing import get_args

    from app.core.config import DEVELOPER_ENVIRONMENTS, Environment

    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    environments = get_args(Environment)

    assert len(environments) >= 4, environments
    for environment in environments:
        assert f"`{environment}`" in security, f"{environment} is absent from the policy table"
    assert set(DEVELOPER_ENVIRONMENTS) < set(environments)


def test_the_billing_subsystem_the_documentation_describes_is_importable() -> None:
    """The other direction of the rule, and the one F-7's worst instance needed.

    `ARCHITECTURE.md` said invoices and a payment provider did not exist while
    `CheckoutService` was taking card payments. A document is wrong when it
    describes something absent *and* when it denies something present, and the
    cheapest guard against the second is to import what it names.
    """
    from app.services.checkout_service import CheckoutService
    from app.services.recurring_service import RecurringService
    from app.services.refund_service import RefundService

    named = (CheckoutService, RecurringService, RefundService)
    assert [service.__name__ for service in named] == [
        "CheckoutService",
        "RecurringService",
        "RefundService",
    ]


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
        # F-7. The technical source of truth stated that billing did not exist
        # while it was taking money, and five more denied things that ship.
        ("billing", "Invoices and a payment provider do not"),
        ("billing", "What is not built is money changing hands"),
        ("workers", "There is no worker service yet"),
        ("workers", "background workers arrive in Phase 8"),
        ("saved cards", "Recurring card debits are not part of"),
        ("saved cards", "There is no card on file"),
        ("email", "No message has ever been delivered by Resend"),
        ("whatsapp", "configuration only until the WhatsApp phase lands"),
    ],
)
def test_a_corrected_claim_has_not_come_back(subject: str, phrase: str) -> None:
    """The six sentences this audit corrected, held down by name.

    Narrow on purpose. A general "does the documentation match the code" test
    cannot be written, and one that tried would be a snapshot of prose. What can
    be written is "this specific sentence was wrong once", which is cheap and
    catches the revert.
    """
    for path in (
        README,
        ARCHITECTURE,
        ROOT / "TASKS.md",
        # One of F-7's twelve lived in a comment here, which is documentation
        # an operator reads while configuring a deployment.
        ROOT / ".env.example",
        *sorted((ROOT / "docs").glob("*.md")),
    ):
        assert phrase not in path.read_text(
            encoding="utf-8"
        ), f"{subject} claim is back in {path.name}"
