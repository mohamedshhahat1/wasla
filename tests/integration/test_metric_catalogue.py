"""Every cross-process counter that is written must also be rendered.

`_increment_by` writes to a Redis hash named after the metric. The scrape reads
`REDIS_COUNTERS` and renders what it finds. Nothing connected the two, so a
counter could be written for months into a hash nobody ever read - which is
what happened to `wasla_payment_reconciliation_total`, the metric ADR-088 names
as the compensating control for an accepted risk, that `docs/OBSERVABILITY.md`
gives four alert expressions against, and that no test mentioned anywhere.

This file closes both halves. The first two tests are the property, against
real Redis and the real exposition. The third is the guard: it discovers every
metric name passed to the increment path by parsing the module's own syntax
tree, and asserts the catalogue covers all of them. That is the test that would
have caught this one and will catch the next.

**Non-vacuity is the point of the third test**, because the failure mode of a
discovery test is finding nothing and passing. It asserts the discovered set is
non-empty and contains a name it can check by hand before it compares anything.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import AsyncIterator, Iterator

import pytest
from redis.asyncio import Redis

from app.core import telemetry
from app.core.metrics import MetricsRegistry
from app.core.telemetry import (
    COUNTER_PREFIX,
    REDIS_COUNTERS,
    REDIS_HISTOGRAMS,
    record_payment_reconciliation,
    set_counter_sink,
)
from app.services.metrics_service import MetricsService

pytestmark = pytest.mark.integration

# A database of its own, so a run cannot disturb whatever else uses this Redis.
REDIS_URL = "redis://localhost:6379/13"

# The counter and the histogram this file exists for. Named as constants so the
# tests read as statements about them rather than about strings.
RECONCILIATION = "wasla_payment_reconciliation_total"
OLDEST_PENDING = "wasla_oldest_pending_payment_age_seconds"
# A neighbour that already rendered before this fix, used as the positive
# control: a test proving the reconciliation counter is absent proves nothing
# unless something comparable is present in the same exposition.
CONTROL = "wasla_media_upload_reconciliation_total"


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - environment without Redis
        await client.aclose()
        pytest.skip("No Redis reachable; these need one to observe a real counter.")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def sink(redis: Redis) -> Iterator[Redis]:
    set_counter_sink(redis)
    try:
        yield redis
    finally:
        set_counter_sink(None)


async def _exposition(redis: Redis) -> str:
    """The exposition `GET /metrics` returns, built the way the route builds it.

    A registry of its own so a series left behind by a neighbouring test cannot
    be mistaken for one this file produced.
    """
    service = MetricsService(redis, registry=MetricsRegistry())
    return await service.render()


# ------------------------------------------------------------ the property


async def test_the_reconciliation_counter_reaches_the_exposition(
    sink: Redis,
) -> None:
    """F-4. Written to Redis, and now read back out of it.

    Before the catalogue entry this asserted the opposite: the hash existed and
    held the outcomes, and `/metrics` did not mention the name at all, because
    the scrape iterates `REDIS_COUNTERS` and the metric was not in it.
    """
    await record_payment_reconciliation(
        settled=3,
        failed=1,
        abandoned=0,
        still_pending=2,
        not_found=0,
        unreachable=0,
        pending=7,
        oldest_pending_seconds=0.0,
    )

    stored = await sink.hgetall(f"{COUNTER_PREFIX}:{RECONCILIATION}")  # type: ignore[misc]
    assert stored, "the counter must reach Redis before rendering can be the question"

    body = await _exposition(sink)

    assert f"# TYPE {RECONCILIATION} counter" in body
    assert f'{RECONCILIATION}{{outcome="settled"}} 3' in body
    assert f'{RECONCILIATION}{{outcome="pending"}} 7' in body
    assert f'{RECONCILIATION}{{outcome="failed"}} 1' in body
    assert CONTROL in body, "the positive control must render in the same body"


async def test_the_oldest_pending_age_reaches_the_exposition(sink: Redis) -> None:
    """The other half of the alerting story ADR-088 relies on.

    A backlog of one is a callback in flight; a backlog of one that is a day
    old is an invoice nobody can collect. The count alone cannot say which, so
    the age has to render too - and unlike the counter it always did. This is
    the control that says the counter was the only thing missing.
    """
    await record_payment_reconciliation(
        settled=0,
        failed=0,
        abandoned=0,
        still_pending=1,
        not_found=0,
        unreachable=0,
        pending=1,
        oldest_pending_seconds=7200.0,
    )

    body = await _exposition(sink)

    assert f"# TYPE {OLDEST_PENDING} histogram" in body
    assert f"{OLDEST_PENDING}_sum 7200" in body


async def test_the_outcome_label_domain_stays_bounded(sink: Redis) -> None:
    """Seven values, for ever, and no identifier among them.

    The reason this module writes Prometheus text by hand rather than pulling a
    library in: a library accepts `tenant_id` as a label without complaint, and
    the resulting cardinality explosion is silent and weeks late. The domain is
    asserted rather than described.
    """
    await record_payment_reconciliation(
        settled=1,
        failed=1,
        abandoned=1,
        still_pending=1,
        not_found=1,
        unreachable=1,
        pending=1,
        oldest_pending_seconds=0.0,
    )

    stored = await sink.hgetall(f"{COUNTER_PREFIX}:{RECONCILIATION}")  # type: ignore[misc]

    assert set(stored) == {
        "outcome=settled",
        "outcome=failed",
        "outcome=abandoned",
        "outcome=still_pending",
        "outcome=not_found",
        "outcome=unreachable",
        "outcome=pending",
    }
    assert REDIS_COUNTERS[RECONCILIATION][1] == ("outcome",)


async def test_an_unlabelled_metric_renders_its_samples_and_not_only_its_header(
    sink: Redis,
) -> None:
    """F-11, found while proving F-4, and the same defect one layer down.

    `_field({})` spells an unlabelled sample as the empty hash field, and
    `_parse_field` read the empty field as unparseable - so every observation of
    a metric with no labels was written to Redis and dropped on the way out.
    The exposition carried the `# HELP` and `# TYPE` and never a bucket, which
    is indistinguishable from "nothing has happened yet" and is why nobody
    noticed. `wasla_oldest_pending_payment_age_seconds` is the only unlabelled
    cross-process metric, and it is the one ADR-088 nominates as the signal for
    an attempt nobody can resolve.

    The header alone was never the property. A `_bucket` line is.
    """
    await record_payment_reconciliation(
        settled=0,
        failed=0,
        abandoned=0,
        still_pending=1,
        not_found=0,
        unreachable=0,
        pending=1,
        oldest_pending_seconds=7200.0,
    )

    body = await _exposition(sink)
    lines = [line for line in body.splitlines() if line.startswith(OLDEST_PENDING)]

    assert lines, "declared and empty is what this test exists to tell apart"
    assert f"{OLDEST_PENDING}_sum 7200" in lines
    assert f"{OLDEST_PENDING}_count 1" in lines
    # The alert in `docs/OBSERVABILITY.md` is a `histogram_quantile` over
    # `..._bucket`, so the bucket series is the one that has to exist.
    assert any(line.startswith(f"{OLDEST_PENDING}_bucket") for line in lines)
    assert f'{OLDEST_PENDING}_bucket{{le="+Inf"}} 1' in lines


async def test_a_labelled_neighbour_was_never_affected(sink: Redis) -> None:
    """The control that places F-11 exactly where it is.

    `wasla_provider_request_duration_seconds` carries two labels, so its hash
    fields were never empty and it rendered throughout. A fix that changed how
    labelled fields parse would break this, and a test that only checked the
    unlabelled metric would not notice.
    """
    await telemetry.record_provider_call(
        provider=telemetry.Provider.OPENAI,
        operation="responses",
        outcome=telemetry.CallOutcome.SUCCESS,
        duration_seconds=1.5,
    )

    body = await _exposition(sink)

    assert 'wasla_provider_request_duration_seconds_sum{operation="responses"' in body
    assert 'wasla_provider_requests_total{operation="responses"' in body


async def test_a_malformed_hash_field_is_still_refused(sink: Redis) -> None:
    """The other control: an empty field became meaningful, junk did not.

    A field written by an older release under a different label scheme must not
    be read as a sample of the current one, and the widened parse must not have
    turned "unparseable" into "unlabelled".
    """
    await sink.hset(f"{COUNTER_PREFIX}:{RECONCILIATION}", "not-a-label-pair", "5")  # type: ignore[misc]
    await sink.hset(f"{COUNTER_PREFIX}:{RECONCILIATION}", "outcome=settled", "2")  # type: ignore[misc]

    body = await _exposition(sink)

    assert f'{RECONCILIATION}{{outcome="settled"}} 2' in body
    assert "not-a-label-pair" not in body


async def test_a_broken_metric_sink_does_not_break_the_billing_pass(
    redis: Redis,
) -> None:
    """Instrumentation may lose a sample; it may not lose a reconciliation.

    The sweep this counter measures resolves payments. A Redis that has gone
    away must leave the recording function returning normally, because the
    caller has already committed the work being counted.
    """

    class Broken:
        async def hincrby(self, *args: object, **kwargs: object) -> int:
            raise ConnectionError("redis is gone")

        async def eval(self, *args: object, **kwargs: object) -> int:
            raise ConnectionError("redis is gone")

    set_counter_sink(Broken())  # type: ignore[arg-type]
    try:
        await record_payment_reconciliation(
            settled=1,
            failed=0,
            abandoned=0,
            still_pending=0,
            not_found=0,
            unreachable=0,
            pending=1,
            oldest_pending_seconds=60.0,
        )
    finally:
        set_counter_sink(None)


# ------------------------------------------------------------- the guard


def _written_metric_names() -> tuple[set[str], set[str]]:
    """Every metric name handed to the counter and histogram paths.

    Read from the syntax tree rather than by importing and introspecting,
    because the names are arguments at call sites and there is nothing at
    runtime to enumerate. A regular expression over the text would find the
    same strings and would also find them in comments and docstrings, which is
    how a drift guard starts reporting metrics that do not exist.

    One indirection is resolved: a name bound to a string constant earlier in
    the same function. That is not generality for its own sake - it is the
    module's own idiom, because a call that observes a distribution also needs
    `REDIS_HISTOGRAMS[metric]` for its bucket bounds and naming the string
    twice would be the drift this file exists to prevent. Anything less direct
    than that is refused by
    `test_every_written_metric_name_resolves_to_a_literal`, so the parser stays
    simple and the shape it depends on is asserted rather than assumed.
    """
    source = pathlib.Path(telemetry.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    counters: set[str] = set()
    histograms: set[str] = set()
    for name, target in _recording_calls(tree):
        if name == "_observe":
            histograms.add(target)
        else:
            counters.add(target)
    return counters, histograms


RECORDERS = ("_increment", "_increment_by", "_observe")


def _constants(function: ast.AST) -> dict[str, str]:
    """String constants bound to plain names inside one function body."""
    bound: dict[str, str] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bound[target.id] = node.value.value
    return bound


def _recording_calls(tree: ast.Module) -> list[tuple[str, str]]:
    """(recorder, metric name) for every call whose name can be resolved.

    Calls inside the recorders themselves are skipped: `_increment` forwards to
    `_increment_by` with the name it was handed, and counting that forwarding
    as an unresolvable call site would make the shape test fail on the module's
    own plumbing.
    """
    found: list[tuple[str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if function.name in RECORDERS:
            continue
        bound = _constants(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in RECORDERS or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.func.id, first.value))
            elif isinstance(first, ast.Name) and first.id in bound:
                found.append((node.func.id, bound[first.id]))
    return found


def _unresolvable_calls() -> list[str]:
    """Call sites whose metric name the guard above cannot see."""
    source = pathlib.Path(telemetry.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    resolved = len(_recording_calls(tree))
    total: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if function.name in RECORDERS:
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in RECORDERS
            ):
                total.append(f"line {node.lineno}")
    return total[resolved:] if len(total) > resolved else []


def test_metric_discovery_finds_something_to_check() -> None:
    """The non-vacuity gate, and it runs before any completeness claim.

    A discovery test that finds nothing passes every completeness assertion
    ever written against it. So this asserts the shape of what was found - a
    minimum count in each catalogue, and one name in each that can be verified
    by reading the module - and the tests below are only meaningful because
    this one is.
    """
    counters, histograms = _written_metric_names()

    assert len(counters) >= 4, counters
    assert len(histograms) >= 2, histograms
    assert RECONCILIATION in counters
    assert OLDEST_PENDING in histograms
    assert CONTROL in counters


def test_the_counter_catalogue_covers_every_name_that_is_written() -> None:
    """The test that would have caught F-4.

    `REDIS_COUNTERS` is what the scrape iterates, so a name written but not
    declared is a hash that accumulates and is never read. There is no way to
    notice that from the outside: no error, no empty series, no metric at all -
    which is why this went unnoticed while three documents told operators to
    alert on it.
    """
    counters, _ = _written_metric_names()

    undeclared = counters - set(REDIS_COUNTERS)

    assert undeclared == set(), f"written but never rendered: {sorted(undeclared)}"


def test_the_histogram_catalogue_covers_every_name_that_is_observed() -> None:
    """The same property for distributions, checked the same way.

    Written as its own test rather than folded into the one above because the
    two catalogues are separate dictionaries rendered by separate loops, and a
    single assertion over their union would pass while one of them was empty.
    """
    _, histograms = _written_metric_names()

    undeclared = histograms - set(REDIS_HISTOGRAMS)

    assert undeclared == set(), f"observed but never rendered: {sorted(undeclared)}"


def test_the_catalogues_declare_nothing_that_is_never_written() -> None:
    """The other direction, which is a different defect with the same cause.

    A declared metric nobody writes renders as a bare `# HELP`/`# TYPE` pair
    for ever - an operator writes an alert against a series that will never
    have a sample, and it never fires. Both catalogues are checked, and each is
    expected to be exactly what the module writes, with no exemption list: an
    exemption list is the hand-maintained structure this file replaces.
    """
    counters, histograms = _written_metric_names()

    assert set(REDIS_COUNTERS) - counters == set()
    assert set(REDIS_HISTOGRAMS) - histograms == set()


def test_every_written_metric_name_resolves_to_a_literal() -> None:
    """Indirection would make the guard above quietly incomplete.

    `_written_metric_names` resolves a constant argument, or a name bound to a
    string constant in the same function. A call that computes its metric name
    any other way is invisible to it, so the drift test would keep passing
    while a metric went undeclared - the exact failure this file exists to end.
    Rather than making the parser cleverer, the shape is pinned.
    """
    assert (
        _unresolvable_calls() == []
    ), f"metric named in a way the catalogue guard cannot see: {_unresolvable_calls()}"
