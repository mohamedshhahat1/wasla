"""An exporter that keeps spans in memory, and the fixture that installs it.

Every tracing test in this suite runs against this rather than a collector. A
test that needed an OTLP endpoint would be a test that does not run in CI, and
what is being asserted — which spans exist, how they are related, and what they
are permitted to say — is entirely visible before anything leaves the process.

`BatchSpanProcessor` exports on its own thread, so `finished` forces a flush
first. Without it a test would be racing the batch interval, which is the sort
of timing flake this project has already paid for more than once.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.core import tracing


class Recorder(SpanExporter):
    """Collects finished spans instead of sending them anywhere."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []
        self._shutdown = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self._shutdown = True


class ExplodingExporter(SpanExporter):
    """An exporter that fails the way a collector being down fails.

    Raising rather than returning `FAILURE`, because the property under test is
    that *nothing* an exporter does reaches the work — and a raise is the
    harsher case. `BatchSpanProcessor` runs this on its own thread, so if the
    isolation holds the exception never touches the request that produced the
    span.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.attempts += 1
        raise RuntimeError("the collector is not there")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


class Recording:
    """The spans one test produced, with the questions tests ask of them."""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def finished(self) -> list[ReadableSpan]:
        """Every span that has ended, flushed out of the batch processor."""
        provider = tracing._provider
        if provider is not None:
            provider.force_flush()
        return list(self._recorder.spans)

    def names(self) -> list[str]:
        return [span.name for span in self.finished()]

    def named(self, name: str) -> ReadableSpan:
        matches = [span for span in self.finished() if span.name == name]
        assert matches, f"no span named {name!r}; saw {self.names()}"
        assert len(matches) == 1, f"{len(matches)} spans named {name!r}"
        return matches[0]

    def all_named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.finished() if span.name == name]

    def trace_ids(self) -> set[int]:
        return {span.context.trace_id for span in self.finished() if span.context is not None}


def install(sample_ratio: float = 1.0) -> tuple[Recording, Recorder]:
    """Point this process's spans at a fresh recorder."""
    recorder = Recorder()
    tracing.install_tracing(recorder, service_name="wasla-test", sample_ratio=sample_ratio)
    return Recording(recorder), recorder


def recording_spans(sample_ratio: float = 1.0) -> Iterator[Recording]:
    """Generator body for a fixture: install, hand over, tear down."""
    record, _ = install(sample_ratio)
    try:
        yield record
    finally:
        tracing.shutdown_tracing()


__all__ = ["ExplodingExporter", "Recorder", "Recording", "install", "recording_spans"]
