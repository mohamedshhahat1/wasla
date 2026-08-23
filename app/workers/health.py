"""The worker container's health command.

A command rather than an endpoint, because the worker serves no HTTP. The image
used to inherit the API's `curl /health/live`, which the worker could never
answer - so it reported unhealthy for its entire life, which made `docker ps`
lie and trained operators to ignore the health column.

Exits 0 when every loop this container is configured to run has beaten recently,
and 1 when one has not. It is a separate process from the worker it asks about,
which is why it reads Redis rather than any in-process state: a probe that
shared memory with the thing it is probing would report a hung process as
healthy.
"""

from __future__ import annotations

import asyncio
import sys

from app.workers.runner import check_health


def main() -> int:
    return 0 if asyncio.run(check_health()) else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
