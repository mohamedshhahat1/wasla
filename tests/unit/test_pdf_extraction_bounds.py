"""Knowledge PDF extraction bounds, proven with real child processes (RAG-02, RAG-08).

The property under test is that a hostile PDF is parsed somewhere that can be
killed, and a mock of a process cannot be killed - so these start the real
parser. Each bound is tested at, just past and well past its limit, and each
refusal is paired with a positive control. The text-safety primitives the
submission path validates with are here too (RAG-11).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.text_safety import has_visible_content, storable_problem
from app.services.extraction import (
    DocumentTooLargeError,
    UnreadableDocumentError,
    extract_pdf_bounded,
)
from app.services.knowledge_limits import MAX_EXTRACTED_CHARACTERS, MAX_PDF_BYTES, MAX_PDF_PAGES
from tests.pdf_fixtures import CHARACTERS_PER_EXPANDING_LINE, expanding_pdf, paged_pdf

# ------------------------------------------------------------ PDF page limit


async def test_a_pdf_at_the_page_limit_is_read_whole() -> None:
    text = await extract_pdf_bounded(paged_pdf(MAX_PDF_PAGES))

    assert "Page 1 says hello." in text
    # The last page too: nothing was cut at a limit it did not exceed.
    assert f"Page {MAX_PDF_PAGES} says hello." in text


@pytest.mark.parametrize("pages", [MAX_PDF_PAGES + 1, 60])
async def test_a_pdf_past_the_page_limit_is_refused_with_the_limit_named(pages: int) -> None:
    with pytest.raises(DocumentTooLargeError) as raised:
        await extract_pdf_bounded(paged_pdf(pages))

    assert f"has {pages} pages" in raised.value.message
    assert f"The limit is {MAX_PDF_PAGES} pages" in raised.value.message


# ------------------------------------------------------------ extracted text


async def test_a_compressed_pdf_is_refused_before_its_text_passes_the_limit() -> None:
    lines = 20_000
    pdf = expanding_pdf(lines)
    # Non-vacuity: the fixture genuinely expands past the limit, from far less.
    assert lines * CHARACTERS_PER_EXPANDING_LINE > MAX_EXTRACTED_CHARACTERS * 2
    assert len(pdf) < 8_000

    with pytest.raises(DocumentTooLargeError) as raised:
        await extract_pdf_bounded(pdf)

    assert f"The limit is {MAX_EXTRACTED_CHARACTERS} characters" in raised.value.message


async def test_a_compressed_pdf_under_the_limit_is_read() -> None:
    lines = 1_000
    assert lines * CHARACTERS_PER_EXPANDING_LINE < MAX_EXTRACTED_CHARACTERS

    text = await extract_pdf_bounded(expanding_pdf(lines))

    assert len(text) >= lines * CHARACTERS_PER_EXPANDING_LINE


async def test_the_character_limit_is_enforced_during_extraction_not_after() -> None:
    """A small limit on a large page: stopped by the parser callback, not a slice."""
    with pytest.raises(DocumentTooLargeError):
        await extract_pdf_bounded(expanding_pdf(2_000), max_characters=5_000)


async def test_decoded_bytes_are_bounded_before_a_process_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[object] = []

    async def spy(*args: object, **kwargs: object) -> object:
        started.append(args)
        raise AssertionError("no process should start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    with pytest.raises(DocumentTooLargeError):
        await extract_pdf_bounded(b"%PDF" + b"x" * MAX_PDF_BYTES)

    assert started == []


@pytest.mark.parametrize("content", [b"", b"not a pdf at all", b"%PDF-1.4\n1 0 obj"])
async def test_something_that_is_not_a_pdf_is_unreadable(content: bytes) -> None:
    with pytest.raises(UnreadableDocumentError):
        await extract_pdf_bounded(content)


# ------------------------------------------------------------ time and the loop


async def test_a_parse_past_its_deadline_is_killed_and_refused() -> None:
    """The audit's 150,000-line shape. Its parse alone outlasts a short deadline."""
    pdf = expanding_pdf(150_000)
    started = time.perf_counter()

    with pytest.raises(DocumentTooLargeError) as raised:
        await extract_pdf_bounded(pdf, timeout_seconds=2.0)

    elapsed = time.perf_counter() - started
    assert "took too long" in raised.value.message
    # Genuinely past the deadline, and genuinely stopped near it.
    assert 2.0 <= elapsed < 10.0


async def test_the_event_loop_keeps_running_while_a_hostile_pdf_is_parsed() -> None:
    """The property RAG-02 lost: everything else on the loop stays live.

    A ticker records the longest gap between its wake-ups while the parse
    runs. Parsed on the loop, as it used to be, the gap is the parse itself.
    """
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    started = time.perf_counter()
    with pytest.raises(DocumentTooLargeError):
        await extract_pdf_bounded(expanding_pdf(150_000), timeout_seconds=3.0)
    stop.set()
    await task

    assert time.perf_counter() - started >= 3.0, "the parse genuinely ran for its deadline"
    assert len(gaps) > 50
    assert max(gaps) < 0.5


async def test_the_parser_process_inherits_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-reach-the-parser")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    seen: dict[str, str] = {}

    real = asyncio.create_subprocess_exec

    async def spy(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        seen.update(environment)
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    await extract_pdf_bounded(paged_pdf(1))

    assert seen, "the process was started through the spy"
    assert "OPENAI_API_KEY" not in seen
    assert "DATABASE_URL" not in seen


# ------------------------------------------------------------ text safety


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("ok\x00", "must not contain NUL characters"),
        ("lone \ud800 surrogate", "must be valid Unicode text"),
    ],
)
def test_unstorable_text_is_named(value: str, problem: str) -> None:
    assert storable_problem(value) == problem


@pytest.mark.parametrize(
    "value",
    [
        "Refunds",
        "\u0633\u064a\u0627\u0633\u0629 \u0627\u0644\u0627\u0633\u062a\u0631\u062c\u0627\u0639",
        "\u200f\u0645\u0631\u062d\u0628\u0627\u200d",
        "😀",
        "\x07\x1b[31mred",
        "a\tb\nc",
    ],
)
def test_ordinary_text_including_arabic_marks_and_controls_is_storable_and_visible(
    value: str,
) -> None:
    assert storable_problem(value) is None
    assert has_visible_content(value)


@pytest.mark.parametrize(
    "value", ["", " \t\n", "\u200b\u200c\u200d", "\u200f\u200e", "\ufeff", "\u0651\u064b"]
)
def test_text_with_nothing_visible_is_recognised(value: str) -> None:
    assert not has_visible_content(value)
