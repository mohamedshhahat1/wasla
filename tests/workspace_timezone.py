"""The time zone every test workspace is created in (TZ-01).

A workspace cannot exist without one - `tenants.timezone` is NOT NULL and has
no default - so every fixture that builds a `Tenant` passes this, and every
request that creates a workspace sends it. One place, so a test that needs a
different zone says so by naming another.
"""

from __future__ import annotations

from typing import Final

#: Where Wasla's workspaces are; also 0108's backfill.
TEST_TIMEZONE: Final = "Africa/Cairo"
