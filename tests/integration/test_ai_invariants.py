"""Invariants the AI path must leave true in the database, whatever ran before.

Each query below counts rows that must not exist. They are written against the
whole database rather than one test's workspace, so a run of the AI suites with
`WASLA_TEST_KEEP_AI_DATA=1` - which keeps every workspace those suites created -
sweeps everything they did, and an ordinary run sweeps whatever is there.

The last two are the AI allowance's (ENT-02, ENT-03, ADR-131), rewritten for
F-1 (KD-02, KD-03) once the charge stopped happening at engagement:

- every customer turn's charge agrees with its own row, its outcome and the
  usage ledger (B1..B7);
- no hold is left open that nothing settled, released or expired (A1..A3).

Both report the rows they count, not only how many, so a red kept-data job is
diagnosable from its log. Each rule is shown to bite by an injection below - one
violation added inside a rolled-back transaction, counted by exactly that rule
and by no other check - and by mutants of the application itself
(`scripts/verification/run_kept_data_mutations.py`, M-K01..M-K10).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.db.models.agent_turn import (
    AgentTurn,
    AgentTurnState,
    AITurnChargeState,
    AITurnReleaseReason,
    TurnOutcome,
)
from app.db.models.conversation import Message
from app.integrations.openai.types import MAX_REPORTED_TOKENS
from app.workers.billing_worker import POLL_SECONDS
from scripts.omnichannel_invariants import _NOT_CHARGEABLE
from tests.integration.ai_harness import settings_for
from tests.integration.test_entitlement_invariants import _charge, _inbound, _tenant, _turn

pytestmark = pytest.mark.integration

#: F-1a. Was "engaged turns stranded past any healthy turn's length".
STRANDED_HOLDS: Final = (
    "AI turn holds nothing settled, released or expired within the TTL and a sweep"
)
#: F-1b. Was "customer turns charged other than once per engaged turn".
INCONSISTENT_CHARGES: Final = "customer turns whose charge disagrees with their outcome or usage"

#: ENT-02's endings that are charged whenever they happen: reply text, or a
#: handoff the agent's own tool executed (`AgentOutcome.chargeable`).
ALWAYS_CHARGED: Final = ("replied", "handed_off")
#: ENT-02's endings that are never charged: no generation produced them. The
#: same list the entitlement ledger's E06 reads; a test below holds them equal.
NEVER_CHARGED: Final = (
    "escalated",
    "empty_response",
    "nothing_to_answer",
    "quota_blocked",
    "channel_not_in_plan",
)
#: Every reason a hold may be released for (`AITurnReleaseReason`).
RELEASE_REASONS: Final = ("not_chargeable", "generation_failed", "hold_expired")

#: How long past its TTL a hold may stay open before it is stranded (KD-03).
#: The billing sweep that releases a dead worker's hold runs every
#: `POLL_SECONDS`, so a healthy deployment releases it at most one interval
#: after the TTL; the margin absorbs commit and clock skew between that sweep
#: and this read.
HOLD_CUTOFF_MARGIN_SECONDS: Final = 60


def _hold_ttl_seconds() -> int:
    """The TTL the harness's workers run with: the deployment's setting."""
    return settings_for("postgresql+asyncpg://unused/unused").ai_turn_hold_ttl_seconds


def hold_cutoff_seconds() -> int:
    """TTL + one billing sweep interval + the margin: 900 + 600 + 60 by default."""
    return _hold_ttl_seconds() + int(POLL_SECONDS) + HOLD_CUTOFF_MARGIN_SECONDS


# F-1a (A1..A3). A hold is open (`held`, A1) until the turn's settle charges or
# releases it, or the billing sweep releases it as `hold_expired` (A3); any of
# those explains it. Older than the cutoff (A2), it is a hold nothing will ever
# settle. An engaged turn that recorded no hold at all is counted too: every
# engagement holds since ADR-131, so that is a hold nobody wrote and nothing
# will release.
STRANDED_HOLD_ROWS: Final = """
    SELECT 'A' AS rule, t.tenant_id, t.id
      FROM agent_turns t
     WHERE (t.charge_state = 'held'
            OR (t.charge_state IS NULL AND t.state = 'engaged'))
       AND coalesce(t.held_at, t.engaged_at)
           < now() - make_interval(secs => :hold_cutoff_seconds)
"""

# F-1b (B1..B7). Consistency, not a second charging decision: each rule
# compares two things the application wrote.
INCONSISTENT_CHARGE_ROWS: Final = f"""
    WITH charges AS (
        SELECT tenant_id, agent_turn_id, count(*) AS charges
          FROM usage_events
         WHERE event_type = 'ai_turn' AND agent_turn_id IS NOT NULL
         GROUP BY tenant_id, agent_turn_id
    )
    -- B1: a charged turn has exactly one ai_turn event.
    SELECT 'B1' AS rule, t.tenant_id, t.id
      FROM agent_turns t
      LEFT JOIN charges c ON c.tenant_id = t.tenant_id AND c.agent_turn_id = t.id
     WHERE t.charge_state = 'charged' AND coalesce(c.charges, 0) <> 1
    UNION ALL
    -- B2: a turn that is not charged has none.
    SELECT 'B2', t.tenant_id, t.id
      FROM agent_turns t
      JOIN charges c ON c.tenant_id = t.tenant_id AND c.agent_turn_id = t.id
     WHERE t.charge_state IS DISTINCT FROM 'charged'
    UNION ALL
    -- B3: a release says why, from the closed set.
    SELECT 'B3', t.tenant_id, t.id
      FROM agent_turns t
     WHERE t.charge_state = 'released'
       AND (t.charge_release_reason IS NULL
            OR t.charge_release_reason::text NOT IN {RELEASE_REASONS})
    UNION ALL
    -- B4: a charge carries no release reason - except the sweep's, which stays
    -- beside the late charge of a hold that outlived its TTL (ADR-131).
    SELECT 'B4', t.tenant_id, t.id
      FROM agent_turns t
     WHERE t.charge_state = 'charged'
       AND t.charge_release_reason IS NOT NULL
       AND t.charge_release_reason::text <> 'hold_expired'
    UNION ALL
    -- B5: an ending ENT-02 always charges was charged.
    SELECT 'B5', t.tenant_id, t.id
      FROM agent_turns t
     WHERE t.outcome::text IN {ALWAYS_CHARGED}
       AND t.charge_state IS DISTINCT FROM 'charged'
    UNION ALL
    -- B6: an ending ENT-02 never charges was not, nor a turn that reached no
    -- ending at all - a provider failure. Nothing is in flight when this runs,
    -- so a charged turn without an outcome is a failure that was charged, or a
    -- worker that died between the charge and the completion (which the AI-09
    -- stranded-turn gauge also shows); both want a person to look.
    SELECT 'B6', t.tenant_id, t.id
      FROM agent_turns t
     WHERE t.charge_state = 'charged'
       AND (t.outcome::text IN {NEVER_CHARGED} OR t.outcome IS NULL)
    UNION ALL
    -- B7: every ai_turn event names a turn of its own workspace. A turn deleted
    -- with its conversation leaves its charge on the ledger by design, so a
    -- missing turn counts only while that conversation still exists.
    SELECT 'B7', u.tenant_id, u.id
      FROM usage_events u
     WHERE u.event_type = 'ai_turn'
       AND (u.agent_turn_id IS NULL
            OR EXISTS (SELECT 1 FROM agent_turns o
                        WHERE o.id = u.agent_turn_id AND o.tenant_id <> u.tenant_id)
            OR (NOT EXISTS (SELECT 1 FROM agent_turns t
                             WHERE t.tenant_id = u.tenant_id AND t.id = u.agent_turn_id)
                AND (u.metadata ->> 'conversation_id' IS NULL
                     OR EXISTS (SELECT 1 FROM conversations c
                                 WHERE c.tenant_id = u.tenant_id
                                   AND c.id::text = u.metadata ->> 'conversation_id'))))
"""  # noqa: S608 - module constants

#: The rows behind the two allowance checks, for the failure message.
EVIDENCE: Final[dict[str, str]] = {
    STRANDED_HOLDS: STRANDED_HOLD_ROWS,
    INCONSISTENT_CHARGES: INCONSISTENT_CHARGE_ROWS,
}

INVARIANTS: Final[dict[str, str]] = {
    "duplicate conversation sequence numbers": """
        SELECT count(*) FROM (
            SELECT conversation_id, sequence FROM messages
             GROUP BY conversation_id, sequence HAVING count(*) > 1
        ) duplicated
    """,
    "messages without a sequence": "SELECT count(*) FROM messages WHERE sequence IS NULL",
    "duplicate agent turns for one trigger": """
        SELECT count(*) FROM (
            SELECT tenant_id, trigger_message_id FROM agent_turns
             GROUP BY tenant_id, trigger_message_id HAVING count(*) > 1
        ) duplicated
    """,
    "agent turns referencing another workspace's conversation": """
        SELECT count(*) FROM agent_turns t
          JOIN conversations c ON c.id = t.conversation_id
         WHERE c.tenant_id <> t.tenant_id
    """,
    STRANDED_HOLDS: f"SELECT count(*) FROM ({STRANDED_HOLD_ROWS}) stranded",  # noqa: S608
    # `claimed_by` is set on every turn a worker claims and on no row a test
    # inserts directly, so it separates "the worker forgot to say how this ended"
    # from fixtures that model a turn's state by hand.
    "completed turns a worker claimed and finished with no outcome": """
        SELECT count(*) FROM agent_turns
         WHERE state = 'completed' AND outcome IS NULL AND claimed_by IS NOT NULL
    """,
    "agent replies with no idempotency key": """
        SELECT count(*) FROM messages
         WHERE direction = 'outbound' AND origin = 'agent' AND idempotency_key IS NULL
    """,
    "duplicate sentiment readings for one message": """
        SELECT count(*) FROM (
            SELECT message_id FROM message_sentiments
             GROUP BY message_id HAVING count(*) > 1
        ) duplicated
    """,
    "sentiment readings crossing a workspace": """
        SELECT count(*) FROM message_sentiments s
          JOIN messages m ON m.id = s.message_id
         WHERE m.tenant_id <> s.tenant_id
    """,
    "usage rows with a quantity of zero or less": (
        "SELECT count(*) FROM usage_events WHERE quantity <= 0"
    ),
    "token usage rows above any plausible provider count": """
        SELECT count(*) FROM usage_events
         WHERE event_type IN ('ai_input_token', 'ai_output_token')
           AND quantity > 2000000
    """,
    "agent turns surviving a purged workspace": """
        SELECT count(*) FROM agent_turns t
          JOIN tenants w ON w.id = t.tenant_id
         WHERE w.purged_at IS NOT NULL
    """,
    "automated replies in a suspended or deleted workspace": """
        SELECT count(*) FROM messages m
          JOIN tenants w ON w.id = m.tenant_id
         WHERE m.direction = 'outbound' AND m.origin = 'agent'
           AND (w.status <> 'active' OR w.deleted_at IS NOT NULL)
    """,
    "a reply sent for a turn whose outcome says it was not": """
        SELECT count(*) FROM agent_turns t
          JOIN messages m
            ON m.tenant_id = t.tenant_id
           AND m.idempotency_key = 'agent-turn:' || t.trigger_message_id::text
         WHERE t.outcome IN (
                'handed_off', 'escalated', 'quota_blocked', 'nothing_to_answer',
                'suppressed_human', 'suppressed_agent', 'suppressed_workspace',
                'suppressed_closed', 'suppressed_channel'
           )
    """,
    INCONSISTENT_CHARGES: (
        f"SELECT count(*) FROM ({INCONSISTENT_CHARGE_ROWS}) inconsistent"  # noqa: S608
    ),
}


def _parameters() -> dict[str, object]:
    return {"hold_cutoff_seconds": hold_cutoff_seconds()}


async def violations(connection: AsyncConnection) -> dict[str, int]:
    """Every check's count, on `connection` - which may hold uncommitted rows."""
    counted: dict[str, int] = {}
    for name, query in INVARIANTS.items():
        counted[name] = int(await connection.scalar(text(query), _parameters()) or 0)
    return counted


async def evidence(connection: AsyncConnection, name: str, *, limit: int = 20) -> list[str]:
    """The first `limit` rows a check counts, each as `rule tenant row`."""
    rows = await connection.execute(
        text(
            f"SELECT * FROM ({EVIDENCE[name]}) counted"  # noqa: S608 - module constants
            f" ORDER BY 1, 2, 3 LIMIT {int(limit)}"
        ),
        _parameters(),
    )
    return [f"{rule} {tenant} {row}" for rule, tenant, row in rows.all()]


@pytest.mark.parametrize("name", list(INVARIANTS))
async def test_the_ai_path_leaves_no_violation(engine: AsyncEngine, name: str) -> None:
    async with engine.connect() as connection:
        counted = await connection.scalar(text(INVARIANTS[name]), _parameters())
        rows = await evidence(connection, name) if counted and name in EVIDENCE else []
    assert counted == 0, f"{name}: {counted} {rows}"


@pytest.mark.skipif(
    os.environ.get("WASLA_TEST_KEEP_AI_DATA") != "1",
    reason="only a run that kept the AI suites' data has something to prove was swept",
)
async def test_a_kept_run_really_had_something_to_sweep(engine: AsyncEngine) -> None:
    """Non-vacuity: every invariant above passes trivially on an empty database.

    The allowance checks need every charge state ENT-02 writes to be there, or
    B1..B7 and A1..A3 were judged against nothing.
    """
    async with engine.connect() as connection:
        turns = await connection.scalar(
            text("SELECT count(*) FROM agent_turns WHERE outcome IS NOT NULL")
        )
        replies = await connection.scalar(
            text("SELECT count(*) FROM messages WHERE origin = 'agent'")
        )
        charged = await connection.scalar(
            text("SELECT coalesce(sum(quantity), 0) FROM usage_events WHERE event_type = 'ai_turn'")
        )
        outcomes = await connection.scalar(
            text("SELECT count(DISTINCT outcome) FROM agent_turns WHERE outcome IS NOT NULL")
        )
        states = {
            f"{state}/{reason}": int(count)
            for state, reason, count in (
                await connection.execute(
                    text(
                        "SELECT charge_state::text, coalesce(charge_release_reason::text, '-'),"
                        " count(*) FROM agent_turns WHERE charge_state IS NOT NULL GROUP BY 1, 2"
                    )
                )
            ).all()
        }
    assert turns and turns >= 20, turns
    assert replies and replies >= 10, replies
    assert charged and charged >= 10, charged
    assert outcomes and outcomes >= 8, outcomes
    for present in (
        "charged/-",
        "released/not_chargeable",
        "released/generation_failed",
        "released/hold_expired",
        "charged/hold_expired",
    ):
        assert states.get(present), (present, states)


def test_the_literals_in_these_queries_match_the_constants_they_restate() -> None:
    """The SQL is literal on purpose, and these are the values it restates."""
    assert MAX_REPORTED_TOKENS == 2_000_000
    assert (
        "'suppressed_closed'" in INVARIANTS["a reply sent for a turn whose outcome says it was not"]
    )
    assert set(RELEASE_REASONS) == {reason.value for reason in AITurnReleaseReason}
    assert {outcome.value for outcome in TurnOutcome} >= set(ALWAYS_CHARGED) | set(NEVER_CHARGED)
    # The entitlement ledger's E06 and this check read one list of endings.
    restated = "(" + ", ".join(f"'{outcome}'" for outcome in NEVER_CHARGED) + ")"
    assert restated == _NOT_CHARGEABLE


def test_the_stranded_hold_cutoff_is_the_ttl_plus_one_sweep_plus_a_margin() -> None:
    """KD-03's formula, at the deployment defaults: 900 s + 600 s + 60 s = 1,560 s."""
    assert hold_cutoff_seconds() == _hold_ttl_seconds() + POLL_SECONDS + HOLD_CUTOFF_MARGIN_SECONDS
    assert (_hold_ttl_seconds(), POLL_SECONDS, HOLD_CUTOFF_MARGIN_SECONDS) == (900, 600, 60)
    assert ":hold_cutoff_seconds" in STRANDED_HOLD_ROWS


# ------------------------------------------------- injections (F-1, KD-02/KD-03)
#
# Each one builds a small legal set of turns with the helpers the entitlement
# ledger's injections use, proves the two allowance checks read zero on it, adds
# one violation, and proves exactly one rule of exactly one check counts it. A
# rule the database itself enforces - a unique index, the
# `charge_state_consistent` constraint - is lifted first, inside the test's
# rolled-back transaction: the only way to show the query sees what the guard
# keeps out.


async def _counts(session: AsyncSession) -> dict[str, int]:
    await session.flush()
    return await violations(await session.connection())


async def _rules(session: AsyncSession, name: str) -> list[str]:
    await session.flush()
    return [line.split(" ")[0] for line in await evidence(await session.connection(), name)]


async def _added(session: AsyncSession, before: dict[str, int]) -> dict[str, int]:
    after = await _counts(session)
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


async def _legal(session: AsyncSession) -> tuple[uuid.UUID, list[Message], dict[str, int]]:
    """A workspace holding every legal shape of a settled turn, and spare triggers."""
    tenant = await _tenant(session)
    triggers = await _inbound(session, tenant, 6)
    moment = datetime.now(UTC)
    held = moment - timedelta(seconds=30)
    replied = _turn(
        triggers[0], charge_state=AITurnChargeState.CHARGED, held_at=held, charged_at=moment
    )
    replied.state, replied.outcome = AgentTurnState.COMPLETED, TurnOutcome.REPLIED
    failed = _turn(
        triggers[1],
        charge_state=AITurnChargeState.RELEASED,
        held_at=held,
        released_at=moment,
        charge_release_reason=AITurnReleaseReason.GENERATION_FAILED,
    )
    empty = _turn(
        triggers[2],
        charge_state=AITurnChargeState.RELEASED,
        held_at=held,
        released_at=moment,
        charge_release_reason=AITurnReleaseReason.NOT_CHARGEABLE,
    )
    empty.state, empty.outcome = AgentTurnState.COMPLETED, TurnOutcome.EMPTY_RESPONSE
    late = _turn(
        triggers[3],
        charge_state=AITurnChargeState.CHARGED,
        held_at=moment - timedelta(hours=1),
        released_at=moment - timedelta(minutes=30),
        charged_at=moment,
        charge_release_reason=AITurnReleaseReason.HOLD_EXPIRED,
    )
    late.state, late.outcome = AgentTurnState.COMPLETED, TurnOutcome.REPLIED
    session.add_all([replied, failed, empty, late])
    await session.flush()
    session.add_all([_charge(replied), _charge(late)])
    before = await _counts(session)
    assert before[STRANDED_HOLDS] == 0, before
    assert before[INCONSISTENT_CHARGES] == 0, before
    return tenant.id, triggers[4:], before


async def _lift(session: AsyncSession, guard: str) -> None:
    statements = {
        "unique charge": "DROP INDEX uq_usage_events_tenant_id_agent_turn_id",
        "charge columns": (
            "ALTER TABLE agent_turns DROP CONSTRAINT ck_agent_turns_charge_state_consistent"
        ),
    }
    await session.execute(text(statements[guard]))


def _charged(trigger: Message, outcome: TurnOutcome | None) -> AgentTurn:
    moment = datetime.now(UTC)
    turn = _turn(
        trigger,
        charge_state=AITurnChargeState.CHARGED,
        held_at=moment - timedelta(seconds=5),
        charged_at=moment,
    )
    if outcome is not None:
        turn.state, turn.outcome = AgentTurnState.COMPLETED, outcome
    return turn


def _released(
    trigger: Message, outcome: TurnOutcome | None, reason: AITurnReleaseReason | None
) -> AgentTurn:
    moment = datetime.now(UTC)
    turn = _turn(
        trigger,
        charge_state=AITurnChargeState.RELEASED,
        held_at=moment - timedelta(seconds=5),
        released_at=moment,
        charge_release_reason=reason,
    )
    if outcome is not None:
        turn.state, turn.outcome = AgentTurnState.COMPLETED, outcome
    return turn


async def test_a_turn_charged_twice_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    await _lift(db_session, "unique charge")
    turn = _charged(trigger, TurnOutcome.REPLIED)
    db_session.add(turn)
    await db_session.flush()

    db_session.add_all([_charge(turn), _charge(turn)])

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B1"]


async def test_a_charged_turn_without_its_usage_event_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)

    db_session.add(_charged(trigger, TurnOutcome.REPLIED))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B1"]


async def test_a_released_turn_with_a_usage_event_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    turn = _released(trigger, None, AITurnReleaseReason.GENERATION_FAILED)
    db_session.add(turn)
    await db_session.flush()

    db_session.add(_charge(turn))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B2"]


async def test_a_released_turn_without_a_reason_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    await _lift(db_session, "charge columns")

    db_session.add(_released(trigger, TurnOutcome.EMPTY_RESPONSE, None))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B3"]


async def test_a_charged_turn_with_a_release_reason_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    await _lift(db_session, "charge columns")
    turn = _charged(trigger, TurnOutcome.REPLIED)
    turn.released_at = datetime.now(UTC)
    turn.charge_release_reason = AITurnReleaseReason.NOT_CHARGEABLE
    db_session.add(turn)
    await db_session.flush()

    db_session.add(_charge(turn))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B4"]


async def test_a_replied_turn_left_uncharged_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)

    db_session.add(_released(trigger, TurnOutcome.REPLIED, AITurnReleaseReason.NOT_CHARGEABLE))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B5"]


async def test_a_provider_failure_that_was_charged_is_counted(db_session: AsyncSession) -> None:
    """The failure reached no outcome; its turn stayed engaged, and was charged."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    turn = _charged(trigger, None)
    db_session.add(turn)
    await db_session.flush()

    db_session.add(_charge(turn))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B6"]


async def test_an_empty_answer_that_was_charged_is_counted(db_session: AsyncSession) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    turn = _charged(trigger, TurnOutcome.EMPTY_RESPONSE)
    db_session.add(turn)
    await db_session.flush()

    db_session.add(_charge(turn))

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B6"]


async def test_a_usage_event_without_a_turn_is_counted(db_session: AsyncSession) -> None:
    """Its conversation is still there, so nothing deleted the turn it names."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    missing = _turn(trigger)
    missing.id = uuid.uuid4()
    charge = _charge(missing)
    charge.meta = {"conversation_id": str(trigger.conversation_id)}

    db_session.add(charge)

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B7"]


async def test_a_usage_event_of_another_workspaces_turn_is_counted(
    db_session: AsyncSession,
) -> None:
    _tenant_id, (trigger, *_), _before = await _legal(db_session)
    theirs = _released(trigger, None, AITurnReleaseReason.GENERATION_FAILED)
    db_session.add(theirs)
    ours = await _tenant(db_session)
    before = await _counts(db_session)
    charge = _charge(theirs)
    charge.tenant_id = ours.id

    db_session.add(charge)

    assert await _added(db_session, before) == {INCONSISTENT_CHARGES: 1}
    assert await _rules(db_session, INCONSISTENT_CHARGES) == ["B7"]


async def test_a_charge_whose_conversation_was_deleted_is_not_counted(
    db_session: AsyncSession,
) -> None:
    """Negative: the ledger outlives a deleted conversation's turns, by design."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    deleted = _turn(trigger)
    deleted.id = uuid.uuid4()
    charge = _charge(deleted)
    charge.meta = {"conversation_id": str(uuid.uuid4())}

    db_session.add(charge)

    assert await _added(db_session, before) == {}


async def test_a_hold_nothing_settled_released_or_expired_is_counted(
    db_session: AsyncSession,
) -> None:
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    held_at = datetime.now(UTC) - timedelta(seconds=hold_cutoff_seconds() + 1)

    db_session.add(_turn(trigger, charge_state=AITurnChargeState.HELD, held_at=held_at))

    assert await _added(db_session, before) == {STRANDED_HOLDS: 1}
    assert await _rules(db_session, STRANDED_HOLDS) == ["A"]


async def test_a_hold_past_its_ttl_but_within_one_sweep_is_not_yet_stranded(
    db_session: AsyncSession,
) -> None:
    """Negative, and the threshold's own: the sweep has one interval to release it."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    held_at = datetime.now(UTC) - timedelta(seconds=_hold_ttl_seconds() + 120)

    db_session.add(_turn(trigger, charge_state=AITurnChargeState.HELD, held_at=held_at))

    assert await _added(db_session, before) == {}


async def test_an_expired_hold_is_not_stranded(db_session: AsyncSession) -> None:
    """Negative (A3): a dead worker's turn stays engaged, but the sweep released its hold."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    held_at = datetime.now(UTC) - timedelta(days=1)

    db_session.add(
        _turn(
            trigger,
            charge_state=AITurnChargeState.RELEASED,
            held_at=held_at,
            engaged_at=held_at,
            released_at=held_at + timedelta(minutes=20),
            charge_release_reason=AITurnReleaseReason.HOLD_EXPIRED,
        )
    )

    assert await _added(db_session, before) == {}


async def test_a_released_provider_failure_is_not_counted(db_session: AsyncSession) -> None:
    """Negative: ENT-02 gives a failure's hold back, and its turn stays engaged."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)
    turn = _released(trigger, None, AITurnReleaseReason.GENERATION_FAILED)
    turn.held_at = turn.engaged_at = datetime.now(UTC) - timedelta(days=1)

    db_session.add(turn)

    assert await _added(db_session, before) == {}


async def test_an_uncharged_empty_answer_is_not_counted(db_session: AsyncSession) -> None:
    """Negative: the shape the old once-per-engaged-turn check miscounted (F-1b)."""
    _tenant_id, (trigger, *_), before = await _legal(db_session)

    db_session.add(
        _released(trigger, TurnOutcome.EMPTY_RESPONSE, AITurnReleaseReason.NOT_CHARGEABLE)
    )

    assert await _added(db_session, before) == {}


async def test_the_legal_set_holds_every_shape_the_rules_must_not_count(
    db_session: AsyncSession,
) -> None:
    """Non-vacuity of the injections' baseline: the legal shapes really are there."""
    tenant_id, _spare, _before = await _legal(db_session)
    connection = await db_session.connection()
    shapes = (
        await connection.execute(
            text(
                "SELECT t.charge_state::text, coalesce(t.charge_release_reason::text, '-'),"
                " coalesce(t.outcome::text, '-'),"
                " (SELECT count(*) FROM usage_events u WHERE u.tenant_id = t.tenant_id"
                "   AND u.agent_turn_id = t.id)"
                " FROM agent_turns t WHERE t.tenant_id = :tenant ORDER BY 1, 2, 3"
            ),
            {"tenant": tenant_id},
        )
    ).all()
    assert [tuple(row) for row in shapes] == [
        ("charged", "-", "replied", 1),
        ("charged", "hold_expired", "replied", 1),
        ("released", "generation_failed", "-", 0),
        ("released", "not_chargeable", "empty_response", 0),
    ]
