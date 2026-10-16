"""The namespaces of this application's two-key PostgreSQL advisory locks.

`pg_advisory_xact_lock(namespace, key)` serialises every transaction asking
for the same pair. The namespace says *which kind of thing* is locked, so two
unrelated operations that happen to pick the same key must never share one:
they would wait on each other for no reason. The database audit (DB-016) found
exactly that - password resets and platform-owner changes both used
`0x57415302`, while both declarations claimed to be distinct.

So every namespace is declared here, once, and
`tests/unit/test_advisory_lock_namespaces.py` refuses a duplicate value or a
namespace literal declared anywhere else.

The one-key form (`pg_advisory_xact_lock(bigint)`) is a separate key space in
PostgreSQL and cannot collide with these; the entitlement guard uses it, with
a hash of the workspace and limit as its key (`entitlement_service._lock_id`).
"""

from __future__ import annotations

from typing import Final

# Workspace creation, per account (`WorkspaceService`).
WORKSPACE_CREATION: Final = 0x5741_5301
# The set of platform owners, of which there is one (`platform.hierarchy`).
PLATFORM_OWNERS: Final = 0x5741_5302
# Password-reset issuance, per account (`PasswordResetService`).
PASSWORD_RESET_REQUEST: Final = 0x5741_5303
# The business-initiated ledger's reservation, per business portfolio
# (`BusinessInitiatedLedger`, LIM-03): count, decide and record as one step.
BUSINESS_PORTFOLIO: Final = 0x5741_5304
# A workspace's saved campaign segments, counted against their cap as one step
# with the insert (`CampaignSegmentService`, PG-06).
CAMPAIGN_SEGMENTS: Final = 0x5741_5305
# A workspace's campaign test sends, counted against the daily allowance as
# one step with the record (`CampaignService.send_test`, PG-09).
CAMPAIGN_TEST_SENDS: Final = 0x5741_5306

NAMESPACES: Final[dict[str, int]] = {
    "workspace_creation": WORKSPACE_CREATION,
    "platform_owners": PLATFORM_OWNERS,
    "password_reset_request": PASSWORD_RESET_REQUEST,
    "business_portfolio": BUSINESS_PORTFOLIO,
    "campaign_segments": CAMPAIGN_SEGMENTS,
    "campaign_test_sends": CAMPAIGN_TEST_SENDS,
}

__all__ = [
    "BUSINESS_PORTFOLIO",
    "CAMPAIGN_SEGMENTS",
    "CAMPAIGN_TEST_SENDS",
    "NAMESPACES",
    "PASSWORD_RESET_REQUEST",
    "PLATFORM_OWNERS",
    "WORKSPACE_CREATION",
]
