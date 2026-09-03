"""A pytest plugin that runs a suite against a span exporter which always fails.

Not loaded by default. Opt in for a drill:

    PYTHONPATH=. pytest -p tests.exploding_exporter tests/integration/...

`PYTHONPATH` because a `-p` plugin is imported during preparse, before pytest
puts the rootdir on `sys.path`. Setting `pythonpath` in `pyproject.toml` would
also work and would change how every one of the 3,500 other tests resolves
imports, which is a poor trade for a diagnostic that runs on demand.

Answers the strongest form of "can a telemetry failure alter a payment
outcome": not one `ProviderCall` in isolation, but every settlement, refund,
dunning and recurring-billing test, against an exporter that raises on every
export.

Two things had to be right before the run meant anything, and both were got
wrong first.

`configure_tracing` is neutralised for the duration. The application calls it
in its lifespan, and with `tracing_enabled=False` — the test default — it sets
the provider to None, which silently discards the exporter installed here. The
first attempt at this drill did exactly that: 107 tests passed against an
exporter that was never consulted.

And the exporter counts the spans it was handed, reported at the end. "The
suite passed with a failing exporter" is also consistent with the suite never
producing a span, and nothing distinguishes the two readings without a number.
"""

from collections.abc import Iterator, Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult

from app.core import tracing
from tests.tracing_recorder import ExplodingExporter

COUNTED = {"spans": 0}


class CountingExploder(ExplodingExporter):
    """Raises like the original, and says how much it was asked to export."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        COUNTED["spans"] += len(spans)
        return super().export(spans)


EXPORTER = CountingExploder()


def _noop(*args: object, **kwargs: object) -> None:
    return None


@pytest.fixture(autouse=True, scope="session")
def _explode(request: pytest.FixtureRequest) -> Iterator[None]:
    import app.main

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tracing, "configure_tracing", _noop)
    monkey.setattr(app.main, "configure_tracing", _noop)
    tracing.install_tracing(EXPORTER, service_name="wasla-exploding")

    yield

    provider = tracing._provider
    if provider is not None:
        provider.force_flush()
    monkey.undo()
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f">>> {COUNTED['spans']} spans handed to an exporter that raised on "
            f"every one of {EXPORTER.attempts} attempts"
        )
