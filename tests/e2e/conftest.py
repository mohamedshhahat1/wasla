"""Database fixtures for the end-to-end suite.

Re-exported from the integration conftest rather than duplicated: these tests
need the same prepared schema, and two definitions of "build the schema" would
drift apart the first time a migration changed.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  - re-exported as fixtures
    database_url,
    prepared_database,
)
