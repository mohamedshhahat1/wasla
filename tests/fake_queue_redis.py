"""A Redis stand-in that actually keeps the lists, sets and hashes.

The queue tests used to hand each queue a fake that recorded the commands it
received and answered every `lrem` with 1. That was enough while a job had two
outcomes, and it is not enough now: dead-letter deduplication is *defined* as
"the second `lrem` removes nothing", so a fake that always says it removed
something cannot tell the working implementation from the broken one.

This keeps real state. It implements only the commands the queues issue, and
it implements them the way Redis does - `lrem` returns how many it removed,
`zrem` returns how many it took, `blmove` returns None on an empty list - so a
test that passes here is testing the semantics the production code relies on.
"""

from __future__ import annotations

from typing import Any


class FakeQueueRedis:
    """Lists, one sorted set per key, and hashes. Nothing else."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        # Every command issued, for the few assertions that care about the
        # shape of the conversation rather than its result.
        self.calls: list[tuple[str, ...]] = []

    # ------------------------------------------------------------- lists

    async def rpush(self, key: str, value: str) -> int:
        self.calls.append(("rpush", key, value))
        queue = self.lists.setdefault(key, [])
        queue.append(value)
        return len(queue)

    async def lpush(self, key: str, value: str) -> int:
        queue = self.lists.setdefault(key, [])
        queue.insert(0, value)
        return len(queue)

    # ASYNC109 wants asyncio.timeout, but this mirrors redis-py's own
    # signature: the fake has to accept what the queue actually passes.
    async def blmove(
        self,
        source: str,
        destination: str,
        timeout: int,  # noqa: ASYNC109 - redis-py's own signature, which the queue calls
    ) -> str | None:
        self.calls.append(("blmove", source, destination, str(timeout)))
        queue = self.lists.get(source) or []
        if not queue:
            return None
        value = queue.pop(0)
        self.lists.setdefault(destination, []).append(value)
        return value

    async def lrem(self, key: str, count: int, value: str) -> int:
        self.calls.append(("lrem", key, str(count), value))
        queue = self.lists.get(key)
        if not queue or value not in queue:
            return 0
        removed = 0
        limit = count or len(queue)
        while value in queue and removed < limit:
            queue.remove(value)
            removed += 1
        return removed

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    async def lindex(self, key: str, index: int) -> str | None:
        queue = self.lists.get(key) or []
        try:
            return queue[index]
        except IndexError:
            return None

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        queue = self.lists.get(key) or []
        if end == -1:
            return queue[start:]
        return queue[start : end + 1]

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        queue = self.lists.get(key)
        if queue is None:
            return True
        self.lists[key] = queue[start:] if end == -1 else queue[start : end + 1]
        return True

    # -------------------------------------------------------- sorted set

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.calls.append(("zadd", key))
        members = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in members)
        members.update(mapping)
        return added

    async def zrangebyscore(
        self,
        key: str,
        minimum: Any,
        maximum: Any,
        start: int = 0,
        num: int | None = None,
    ) -> list[str]:
        members = self.zsets.get(key) or {}
        low = float("-inf") if minimum in {"-inf", b"-inf"} else float(minimum)
        high = float("inf") if maximum in {"+inf", b"+inf"} else float(maximum)
        ordered = sorted(
            (member for member, score in members.items() if low <= score <= high),
            key=lambda member: members[member],
        )
        window = ordered[start:]
        return window[:num] if num is not None else window

    async def zrem(self, key: str, member: str) -> int:
        members = self.zsets.get(key) or {}
        return 1 if members.pop(member, None) is not None else 0

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key) or {})

    async def zscore(self, key: str, member: str) -> float | None:
        return (self.zsets.get(key) or {}).get(member)

    # ------------------------------------------------------------ hashes

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        fields = self.hashes.setdefault(key, {})
        fields[field] = fields.get(field, 0) + amount
        return fields[field]

    async def hgetall(self, key: str) -> dict[str, int]:
        return dict(self.hashes.get(key) or {})

    # ------------------------------------------------------------- misc

    async def exists(self, key: str) -> int:
        return 1 if key in self.lists or key in self.zsets or key in self.hashes else 0


class FailingRedis(FakeQueueRedis):
    """Refuses whichever commands a test names, to prove nothing depends on them.

    Used for the guarantee that a metrics failure cannot break the work being
    measured: a `hincrby` that raises must lose a sample and nothing else.
    """

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.failing = failing

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        if "hincrby" in self.failing:
            raise RuntimeError("Redis said no")
        return await super().hincrby(key, field, amount)

    async def hgetall(self, key: str) -> dict[str, int]:
        if "hgetall" in self.failing:
            raise RuntimeError("Redis said no")
        return await super().hgetall(key)


__all__ = ["FailingRedis", "FakeQueueRedis"]
