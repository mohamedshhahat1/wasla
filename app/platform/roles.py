"""The operator's command for platform roles, and the only way to grant one.

    docker compose exec api python -m app.platform.roles list
    docker compose exec api python -m app.platform.roles grant ops@example.com platform_owner
    docker compose exec api python -m app.platform.roles revoke ops@example.com

A command rather than an HTTP endpoint, and that is the decision rather than an
omission (ADR-094). The audience for granting authority over every workspace on
the platform is whoever can already exec into a container, which is exactly the
audience a command has - and a route would have needed a caller who already held
the role, which does not answer the question it exists for.

Shaped like `app.workers.queues`, the other operator command in this
repository: argparse, an exit code, and output that is the point rather than a
debug leftover.

**Nothing here creates an account.** A role can only be given to a user who
signed up in the ordinary way, so the worst this command can do is change what
somebody who already exists is allowed to do - which is bad enough to be
audited, and not bad enough to need a second person to approve it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.core.config import get_settings
from app.core.exceptions import ValidationError
from app.core.logging import configure_logging, get_logger
from app.db.models.enums import PlatformRole
from app.db.session import Database
from app.platform.owner_service import PlatformRoleService

logger = get_logger(__name__)


async def show(database: Database) -> int:
    """Everyone who currently holds a platform role."""
    async with database.session() as session:
        staff = await PlatformRoleService(session).staff()

    if not staff:
        print("No account holds a platform role.")  # noqa: T201 - the command's purpose
        return 0

    print(f"{'role':<18}{'email':<40}id")  # noqa: T201
    print("-" * 96)  # noqa: T201
    for user in staff:
        role = user.platform_role.value if user.platform_role else ""
        print(f"{role:<18}{user.email:<40}{user.id}")  # noqa: T201
    return 0


async def grant(database: Database, *, identity: str, role: PlatformRole) -> int:
    """Give one account a platform role, and record that it happened."""
    async with database.session() as session:
        change = await PlatformRoleService(session).grant(identity, role)

    if not change.changed:
        print(f"{change.email} already holds {role.value}; nothing changed.")  # noqa: T201
        return 0
    previous = change.previous.value if change.previous else "none"
    print(f"{change.email} ({change.user_id}): {previous} -> {role.value}")  # noqa: T201
    return 0


async def revoke(database: Database, *, identity: str) -> int:
    """Take a platform role away, unless it is the last owner's."""
    async with database.session() as session:
        change = await PlatformRoleService(session).revoke(identity)

    if not change.changed:
        print(f"{change.email} holds no platform role; nothing changed.")  # noqa: T201
        return 0
    print(f"{change.email} ({change.user_id}): {change.previous} -> none")  # noqa: T201
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.platform.roles",
        description="Grant, withdraw and list platform administration roles.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show every account holding a platform role")

    given = commands.add_parser("grant", help="give an existing account a platform role")
    given.add_argument("identity", help="the account's email address or its exact id")
    given.add_argument("role", choices=[role.value for role in PlatformRole])

    taken = commands.add_parser("revoke", help="withdraw an account's platform role")
    taken.add_argument("identity", help="the account's email address or its exact id")
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    """Parse, act, and turn a refusal into an exit code rather than a traceback.

    A `ValidationError` here is an operator naming an account that does not
    exist or trying to remove the last owner. Both are answers, and both belong
    on stderr with a non-zero status - not as a stack trace somebody has to read
    past to find the sentence.
    """
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    database = Database(settings)
    try:
        if arguments.command == "list":
            return await show(database)
        if arguments.command == "grant":
            return await grant(
                database,
                identity=arguments.identity,
                role=PlatformRole(arguments.role),
            )
        return await revoke(database, identity=arguments.identity)
    except ValidationError as refusal:
        print(refusal.message, file=sys.stderr)  # noqa: T201
        return 2
    finally:
        await database.dispose()


def main() -> int:  # pragma: no cover - process entry point
    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
