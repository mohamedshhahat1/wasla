"""PDF page furniture and re-ordered lines (RQ-04, RQ-05, RV-04, RV-22, P-R15)."""

from __future__ import annotations

from app.services.pdf_structure import (
    ARABIC_INDIC_LEADING_ZERO,
    DIGITS_GLUED_TO_ARABIC,
    structure,
    warnings_for,
)

HEADER = "Wasla Finishing Co. - Service Handbook"
FOOTER = "Confidential - do not distribute"


def _page(number: int, body: str) -> str:
    return f"{HEADER}\n{body}\n{FOOTER}\n{number}"


def test_running_headers_footers_and_page_numbers_are_dropped() -> None:
    pages = [
        _page(1, "The warranty covers workmanship defects."),
        _page(2, "Claims must be reported in writing."),
        _page(3, "Refunds are issued within seven days."),
    ]
    text = structure(pages).text
    assert HEADER not in text
    assert FOOTER not in text
    assert not [line for line in text.split("\n") if line.strip().isdigit()]
    assert "Claims must be reported in writing." in text


def test_a_paragraph_crossing_a_page_break_is_joined_with_one_newline() -> None:
    pages = [
        _page(1, "Installation is scheduled after the deposit"),
        _page(2, "and the remaining balance is due on handover."),
        _page(3, "Next section."),
    ]
    text = structure(pages).text
    assert "after the deposit\nand the remaining balance" in text
    assert "on handover.\n\nNext section." in text


def test_page_numbers_in_many_forms_are_dropped_at_page_edges() -> None:
    for number in (
        "3",
        "- 3 -",
        "Page 3",
        "page 3 of 12",
        "3 / 12",
        "\u0663",
        "\u0635\u0641\u062d\u0629 3",
    ):
        text = structure([f"Body one.\n{number}", f"{number}\nBody two."]).text
        assert number not in text.split("\n"), number


def test_a_number_inside_the_body_is_content() -> None:
    text = structure(["Prices\n4500\nper square metre."]).text
    assert "4500" in text


def test_short_documents_keep_a_line_repeated_on_their_pages() -> None:
    # Two pages are too few to call a line "running" (RQ-05: three or more).
    text = structure([f"{HEADER}\nOne.", f"{HEADER}\nTwo."]).text
    assert text.count(HEADER) == 2


def test_digits_glued_to_arabic_letters_are_flagged_not_repaired() -> None:
    scrambled = " \u062c\u0646\u064a\u0647 \u0644\u0644\u0645\u062a\u0631.4500\u0633\u0639\u0631"
    result = structure([scrambled])
    assert result.warnings == {DIGITS_GLUED_TO_ARABIC: 1}
    assert ".4500\u0633\u0639\u0631" in result.text


def test_a_reversed_arabic_indic_number_is_flagged() -> None:
    assert warnings_for("\u0633\u0639\u0631 \u0660\u0660\u0662\u0667") == {
        ARABIC_INDIC_LEADING_ZERO: 1
    }


def test_correct_lines_raise_no_warning() -> None:
    for line in (
        "\u0633\u0639\u0631 \u0627\u0644\u0645\u062a\u0631 4500 \u062c\u0646\u064a\u0647",
        "\u0633\u0639\u0631 \u0627\u0644\u0645\u062a\u0631 \u0667\u0662\u0660\u0660 \u062c\u0646\u064a\u0647",  # noqa: E501
        "Starter 1,500 EGP 2",
        "Room 101, floor 0",
    ):
        assert warnings_for(line) == {}, line
