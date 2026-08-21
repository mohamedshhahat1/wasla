"""Lead pipeline rules that hold without a database.

The transition graph and the score bounds are business rules rather than
storage concerns, so they are worth pinning down here where the whole table of
cases is cheap to enumerate.
"""

from __future__ import annotations

import pytest

from app.db.models.lead import (
    AGENT_WRITABLE_FIELDS,
    ALLOWED_TRANSITIONS,
    MAX_SCORE,
    MIN_SCORE,
    TERMINAL_STATUSES,
    Lead,
    LeadStatus,
    clamp_score,
)


def _lead(status: LeadStatus) -> Lead:
    return Lead(status=status)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (LeadStatus.NEW, LeadStatus.CONTACTED),
        (LeadStatus.NEW, LeadStatus.QUALIFIED),
        (LeadStatus.NEW, LeadStatus.LOST),
        (LeadStatus.CONTACTED, LeadStatus.QUALIFIED),
        (LeadStatus.CONTACTED, LeadStatus.PROPOSAL),
        (LeadStatus.QUALIFIED, LeadStatus.PROPOSAL),
        (LeadStatus.QUALIFIED, LeadStatus.WON),
        (LeadStatus.PROPOSAL, LeadStatus.WON),
        (LeadStatus.PROPOSAL, LeadStatus.LOST),
    ],
)
def test_a_lead_moves_forward_through_the_pipeline(start, target):
    assert _lead(start).can_transition_to(target)


@pytest.mark.parametrize(
    ("start", "target"),
    [
        # Skipping stages backwards is not a pipeline.
        (LeadStatus.QUALIFIED, LeadStatus.NEW),
        (LeadStatus.PROPOSAL, LeadStatus.CONTACTED),
        # A closed deal stays closed. The next one is a new lead.
        (LeadStatus.WON, LeadStatus.NEW),
        (LeadStatus.WON, LeadStatus.PROPOSAL),
        (LeadStatus.WON, LeadStatus.LOST),
        # A lost lead reopens only to the start.
        (LeadStatus.LOST, LeadStatus.QUALIFIED),
        (LeadStatus.LOST, LeadStatus.WON),
    ],
)
def test_illegal_moves_are_refused(start, target):
    assert not _lead(start).can_transition_to(target)


def test_a_lost_lead_can_be_reopened():
    """Customers come back, and the pipeline has to be able to say so."""
    assert _lead(LeadStatus.LOST).can_transition_to(LeadStatus.NEW)


@pytest.mark.parametrize("status", list(LeadStatus))
def test_setting_the_status_a_lead_already_has_is_allowed(status):
    """A retried job must not fail because the work was already done."""
    assert _lead(status).can_transition_to(status)


def test_every_status_has_a_transition_rule():
    """A status added later without a rule would raise a KeyError at runtime."""
    assert set(ALLOWED_TRANSITIONS) == set(LeadStatus)


def test_terminal_statuses_lead_nowhere_except_reopening():
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] <= {LeadStatus.NEW}


def test_lost_is_reachable_from_every_open_status():
    """A customer can say no at any point before the deal closes."""
    for status in set(LeadStatus) - TERMINAL_STATUSES:
        assert LeadStatus.LOST in ALLOWED_TRANSITIONS[status]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-10, MIN_SCORE), (0, 0), (50, 50), (100, MAX_SCORE), (900, MAX_SCORE)],
)
def test_a_score_is_pinned_to_its_bounds(value, expected):
    """Clamped rather than rejected: a model's stray number should not lose the lead."""
    assert clamp_score(value) == expected


def test_an_agent_cannot_write_judgement_fields():
    """Status, score, assignment and tags are decisions, not extractions."""
    for field in ("status", "score", "assigned_to_id", "tags", "custom_fields"):
        assert field not in AGENT_WRITABLE_FIELDS


def test_an_agent_can_write_the_details_a_customer_states():
    assert {"name", "email", "phone", "interest", "budget_amount"} <= AGENT_WRITABLE_FIELDS


def test_agent_writable_fields_all_exist_on_the_lead():
    """A rename would otherwise leave extraction silently writing nothing."""
    columns = set(Lead.__mapper__.columns.keys())
    assert columns >= AGENT_WRITABLE_FIELDS


def test_a_lead_reports_whether_it_is_closed():
    assert _lead(LeadStatus.WON).is_closed
    assert _lead(LeadStatus.LOST).is_closed
    assert not _lead(LeadStatus.QUALIFIED).is_closed
