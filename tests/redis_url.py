"""Where the Redis-backed suites find a real Redis.

CI runs a Redis service on `localhost:6379`, and each suite that needs a real
server keeps a database number of its own there, so two files never share keys
and one file's `FLUSHDB` cannot empty another's. On a developer machine port
6379 is often somebody else's - a development stack, another checkout - and a
suite that flushes its database there flushes theirs.

`WASLA_TEST_REDIS_HOST` points every such suite at another server instead - an
isolated container - while keeping each file's own database number. Unset, the
address is exactly what it always was.
"""

from __future__ import annotations

import os

DEFAULT_HOST = "redis://localhost:6379"


def redis_url_for(database: int) -> str:
    """The Redis URL for a suite that owns database `database`."""
    host = os.environ.get("WASLA_TEST_REDIS_HOST", DEFAULT_HOST).rstrip("/")
    return f"{host}/{database}"
