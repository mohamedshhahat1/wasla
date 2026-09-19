"""The CRM's invariants, as SQL that returns the number of violating rows.

Each query must return 0 on a healthy database. They are the same statements
`docs/RUNBOOK.md` gives an operator to run against production after the CRM
remediation is deployed; here they are scoped to the workspaces a test built,
through `:tenants`, so a sweep proves something about its own population
rather than about whatever else a shared database holds.

"Human-owned with no assignee" is deliberately absent: an automatic handoff
leaves a conversation unassigned by design (PD-CRM-3).
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

INVARIANTS: Final[dict[str, str]] = {
    "follow_up_lead_in_another_tenant": """
        SELECT count(*) FROM follow_ups f JOIN leads l ON l.id = f.lead_id
        WHERE l.tenant_id <> f.tenant_id AND f.tenant_id IN :tenants""",
    "lead_conversation_with_another_customer": """
        SELECT count(*) FROM leads l JOIN conversations c ON c.id = l.conversation_id
        WHERE l.contact_id IS NOT NULL AND c.contact_id <> l.contact_id
          AND l.tenant_id IN :tenants""",
    "follow_up_lead_of_another_customer": """
        SELECT count(*) FROM follow_ups f
        JOIN leads l ON l.id = f.lead_id
        JOIN conversations c ON c.id = f.conversation_id
        WHERE l.contact_id IS DISTINCT FROM c.contact_id AND f.tenant_id IN :tenants""",
    "conversation_assigned_to_non_member": """
        SELECT count(*) FROM conversations c
        JOIN tenants t ON t.id = c.tenant_id AND t.deleted_at IS NULL
        WHERE c.assigned_to_id IS NOT NULL AND c.tenant_id IN :tenants
          AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.tenant_id = c.tenant_id
                          AND m.user_id = c.assigned_to_id AND m.status = 'active')""",
    "lead_assigned_to_non_member": """
        SELECT count(*) FROM leads l
        JOIN tenants t ON t.id = l.tenant_id AND t.deleted_at IS NULL
        WHERE l.assigned_to_id IS NOT NULL AND l.tenant_id IN :tenants
          AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.tenant_id = l.tenant_id
                          AND m.user_id = l.assigned_to_id AND m.status = 'active')""",
    "pending_reminder_of_a_departed_member": """
        SELECT count(*) FROM follow_ups f
        JOIN tenants t ON t.id = f.tenant_id AND t.deleted_at IS NULL
        WHERE f.status = 'pending' AND f.created_by_kind = 'user'
          AND f.created_by_id IS NOT NULL AND f.tenant_id IN :tenants
          AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.tenant_id = f.tenant_id
                          AND m.user_id = f.created_by_id AND m.status = 'active')""",
    "second_handoff_without_a_resume": """
        SELECT count(*) FROM (
          SELECT event_type,
                 lag(event_type) OVER (PARTITION BY conversation_id
                                       ORDER BY occurred_at, id) AS previous
          FROM analytics_events
          WHERE event_type IN ('handoff', 'handoff_resumed') AND tenant_id IN :tenants
        ) e WHERE e.event_type = 'handoff' AND e.previous = 'handoff'""",
    "handoff_without_its_ownership_audit": """
        SELECT count(*) FROM (
          SELECT conversation_id, count(*) AS n FROM analytics_events
          WHERE event_type = 'handoff' AND tenant_id IN :tenants GROUP BY conversation_id
        ) h LEFT JOIN (
          SELECT (metadata->>'conversation_id')::uuid AS conversation_id, count(*) AS n
          FROM audit_logs
          WHERE action = 'conversation_taken_over' AND tenant_id IN :tenants
          GROUP BY 1
        ) a ON a.conversation_id = h.conversation_id
        WHERE a.n IS DISTINCT FROM h.n""",
    "ai_conversation_with_a_handoff_reason": """
        SELECT count(*) FROM conversations
        WHERE mode = 'ai' AND handoff_reason IS NOT NULL AND tenant_id IN :tenants""",
    "cancelled_follow_up_that_was_sent": """
        SELECT count(*) FROM follow_ups
        WHERE status = 'cancelled' AND (message_id IS NOT NULL OR sent_at IS NOT NULL)
          AND tenant_id IN :tenants""",
    "finished_follow_up_still_claimed": """
        SELECT count(*) FROM follow_ups
        WHERE status <> 'pending' AND claim_token IS NOT NULL AND tenant_id IN :tenants""",
    "open_lead_with_closed_at": """
        SELECT count(*) FROM leads
        WHERE status NOT IN ('won', 'lost') AND closed_at IS NOT NULL
          AND tenant_id IN :tenants""",
    "closed_lead_without_closed_at": """
        SELECT count(*) FROM leads
        WHERE status IN ('won', 'lost') AND closed_at IS NULL AND tenant_id IN :tenants""",
    "status_move_not_from_the_previous_status": """
        SELECT count(*) FROM (
          SELECT data->>'from' AS moved_from,
                 lag(data->>'to') OVER (PARTITION BY lead_id
                                        ORDER BY created_at, id) AS previous_to
          FROM lead_activities
          WHERE kind = 'status_changed' AND tenant_id IN :tenants
        ) s WHERE s.previous_to IS NOT NULL AND s.moved_from <> s.previous_to""",
    "lead_status_disagrees_with_its_last_move": """
        SELECT count(*) FROM leads l
        JOIN LATERAL (
          SELECT data->>'to' AS last_to FROM lead_activities a
          WHERE a.lead_id = l.id AND a.kind = 'status_changed'
          ORDER BY a.created_at DESC, a.id DESC LIMIT 1
        ) last ON true
        WHERE last.last_to <> l.status::text AND l.tenant_id IN :tenants""",
    "more_than_one_open_lead_per_customer": """
        SELECT count(*) FROM (
          SELECT contact_id FROM leads
          WHERE contact_id IS NOT NULL AND status NOT IN ('won', 'lost')
            AND tenant_id IN :tenants
          GROUP BY tenant_id, contact_id HAVING count(*) > 1
        ) d""",
}


async def sweep(session: AsyncSession, tenants: list[uuid.UUID]) -> dict[str, int]:
    """Every invariant's violation count over `tenants`."""
    found: dict[str, int] = {}
    for name, query in INVARIANTS.items():
        statement = text(query).bindparams(bindparam("tenants", expanding=True))
        found[name] = int(await session.scalar(statement, {"tenants": tenants}) or 0)
    return found


__all__ = ["INVARIANTS", "sweep"]
