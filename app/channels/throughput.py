"""A connection's sending allowance, shared by everything that sends on it (OMNI-017).

A provider limits what one connection - one WhatsApp number, one Page - may send
in a period, and it limits the connection, not the sender. Before this, every
bulk sender held its own rate: a campaign's `messages_per_minute` and
`next_send_at` (ADR-026) spaced *that campaign*, so two campaigns on one number
ran at twice the number's intended rate, and follow-ups, agent replies and
people's replies were not counted against anything at all.

**Off unless configured.** `CONNECTION_SENDS_PER_MINUTE` unset - the default -
takes nothing and changes nothing, so ADR-026's per-campaign rate stays exactly
what governs a campaign, and a deployment opts into sharing when it knows the
limit it wants to hold (ADR-123).

**Held on the connection row, like ADR-026's timestamp.** One fixed one-minute
window per connection: when it started, and how many sends it has admitted.
Taking a unit is one conditional UPDATE, so concurrent senders on one connection
serialise on its row for the length of one statement and the count can never
pass the allowance; a refusal writes nothing and says when the window reopens.
Nothing is slept and nothing lives in process memory, so the allowance holds
across replicas and restarts.

**Bulk waits; conversation is counted, never refused.** Every send on the
connection spends a unit, so a campaign sees the replies going out beside it -
but only a bulk sender (`THROTTLED_ORIGINS`: campaigns and follow-ups) is ever
refused. A reply to a customer is not held back to make room for a broadcast:
an agent's reply is sent after its inference is paid for, and refusing it then
would lose an answer the customer is waiting for, while a person replying in
the inbox is a person, not a batch.

**Taken before anything is staged.** A refusal raises `ConnectionThrottledError`
before a message row exists, so a throttled send is provably not a delivery and
leaves nothing behind to reconcile. A campaign or follow-up that meets it waits
for the window without spending an attempt.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from typing import Final

from app.core.exceptions import RateLimitedError
from app.db.models.conversation import MessageOrigin

#: The period an allowance counts over.
SEND_WINDOW: Final = timedelta(minutes=1)

#: The senders an exhausted allowance refuses. Every other origin is counted
#: against the allowance and never refused.
THROTTLED_ORIGINS: Final = frozenset({MessageOrigin.CAMPAIGN, MessageOrigin.FOLLOW_UP})

#: The `error_code` of a send refused by its connection's allowance.
CONNECTION_THROTTLED: Final = "connection_throttled"


class ConnectionThrottledError(RateLimitedError):
    """This connection has admitted its allowance for the current window.

    Raised before anything is staged, so nothing was sent and nothing needs
    settling. `retry_at` is when the window reopens.
    """

    error_code = CONNECTION_THROTTLED
    message = "This connection has reached its sending allowance. Try again shortly."

    def __init__(self, *, connection_id: uuid.UUID, retry_at: datetime, now: datetime) -> None:
        wait = max(1, math.ceil((retry_at - now).total_seconds()))
        super().__init__(headers={"Retry-After": str(wait)})
        self.connection_id = connection_id
        self.retry_at = retry_at


class ProviderThrottledError(ConnectionThrottledError):
    """The provider throttled this connection; the send was declined (OMNI-035).

    Raised to a bulk sender after the message is recorded as undelivered - a
    throttle is a refusal before reading, so nothing reached a customer - so a
    campaign or follow-up sweep waits for `retry_at` instead of marking its
    recipients failed one by one.
    """

    error_code = "provider_throttled"
    message = "The provider is rate limiting this connection. Try again shortly."


#: How long a bulk sender waits after the provider throttles a connection.
PROVIDER_THROTTLE_BACKOFF: Final = timedelta(seconds=60)


__all__ = [
    "CONNECTION_THROTTLED",
    "PROVIDER_THROTTLE_BACKOFF",
    "SEND_WINDOW",
    "THROTTLED_ORIGINS",
    "ConnectionThrottledError",
    "ProviderThrottledError",
]
