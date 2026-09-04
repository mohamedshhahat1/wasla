"""A billing worker that charges a card and is killed before recording the answer.

Not a test - the entry point of one. `test_billing_crash_recovery.py` runs this
as a real child process, waits for the line it sends over a socket after the
charge has been made, and then terminates it from outside. The kill is the
point: nothing here catches a signal, no `finally` runs, no transaction is
rolled back politely, and the process simply stops existing between the money
moving and the record of it.

That is the failure ADR-088 is built for, and it cannot be produced in-process.
An injected exception unwinds through context managers, closes the session and
rolls the transaction back, which is a tidier ending than a container being
killed mid-sweep. What this leaves behind is the untidy one.

**The charge is reported to the parent, not to the child's own memory.** A
count kept here would die with the process, and the whole question is what
survives it. So the provider double opens a socket back to the test and sends
the reference it was asked to charge; after the child is gone, the only two
things that know money moved are the parent and PostgreSQL.

Deliberately no `test_` prefix, so pytest does not collect it.

Reads its configuration from the environment the parent passes:

    WASLA_CHILD_DATABASE_URL   the test database
    WASLA_CHILD_TENANT_ID      the workspace whose renewal to collect
    WASLA_CHILD_CHARGE_PORT    where to report a charge before blocking
"""

from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.billing import Subscription
from app.db.models.invoice import Invoice, InvoiceStatus
from app.integrations.billing.checkout import SavedMethodCharge, SavedPaymentMethod
from app.services.recurring_service import RecurringService


class ReportingProvider:
    """Charges nothing, tells the parent it did, and then never returns.

    Stands where Paymob would. `charge_saved_method` is the money-moving call,
    so reporting from inside it and then blocking puts the process in exactly
    the state the drill needs: the charge has happened as far as anybody
    outside is concerned, and the worker has not yet written a word about it.
    """

    name = "paymob"

    def __init__(self, port: int) -> None:
        self._port = port

    @property
    def can_charge_saved_methods(self) -> bool:
        return True

    def verify_token_callback(self, *, payload: bytes, signature: str | None) -> SavedPaymentMethod:
        raise NotImplementedError("no saved-card callback arrives in this drill")

    async def charge_saved_method(self, request: SavedMethodCharge) -> str:
        reader, writer = await asyncio.open_connection("127.0.0.1", self._port)
        writer.write(f"{request.reference}\n".encode())
        await writer.drain()
        writer.close()
        del reader

        # Waits to be killed. An `Event` nothing ever sets, rather than a sleep
        # loop, so this blocks for exactly as long as the parent takes and no
        # timer wakes it up in between.
        await asyncio.Event().wait()
        raise AssertionError("the parent was supposed to kill this process")


async def _main() -> None:
    tenant_id = uuid.UUID(os.environ["WASLA_CHILD_TENANT_ID"])
    port = int(os.environ["WASLA_CHILD_CHARGE_PORT"])

    engine = create_async_engine(os.environ["WASLA_CHILD_DATABASE_URL"])
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        due = await session.execute(
            select(Invoice)
            .where(Invoice.tenant_id == tenant_id)
            .where(Invoice.status == InvoiceStatus.OPEN)
        )
        invoice = due.scalars().one()
        owner = await session.execute(
            select(Subscription).where(Subscription.id == invoice.subscription_id)
        )
        subscription = owner.scalars().one()

        service = RecurringService(
            session,
            tenant_id=tenant_id,
            provider=ReportingProvider(port),
        )
        # The real service, the real protocol, the real commits. What is faked
        # is only the far end of the socket.
        await service.collect(invoice, subscription=subscription)


if __name__ == "__main__":
    # No handler around this. An exception that escapes prints its own
    # traceback to stderr and exits non-zero, which is exactly what the parent
    # reads when the child fails to reach the provider - so catching it to
    # print something shorter would only lose the part worth having.
    asyncio.run(_main())
