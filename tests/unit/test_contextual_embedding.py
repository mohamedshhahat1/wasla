"""A chunk is embedded with its document and section, and shown without them (RQ-14, FX-3.2)."""

from __future__ import annotations

import uuid

import pytest

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.services.contextual_embedding import LABEL_SEPARATOR, document_label, embedding_input
from app.services.document_indexing import Claim, prepare
from tests.fake_embeddings import FAKE_MODEL, FakeEmbeddings

# "7900" in Arabic-Indic digits, built rather than typed.
ARABIC_7900 = "".join(chr(0x0660 + int(digit)) for digit in "7900")


@pytest.mark.parametrize(
    ("title", "filename", "label"),
    [
        ("Finishing price list", None, "Finishing price list"),
        ("", "price-list.pdf", "price list"),
        ("scan_003.pdf", None, None),
        ("Untitled", "IMG_2041.pdf", None),
        ("Document (2)", None, None),
        ("untitled", "Returns policy.txt", "Returns policy"),
        ("Price list v2.0", None, "Price list v2.0"),
    ],
)
def test_a_title_that_says_nothing_is_not_a_label(
    title: str, filename: str | None, label: str | None
) -> None:
    assert document_label(title, filename) == label


def test_the_separator_is_a_single_angle_quotation_mark() -> None:
    assert " " + chr(0x203A) + " " == LABEL_SEPARATOR


def test_a_heading_the_chunk_opens_with_is_not_repeated() -> None:
    text = embedding_input(
        "Refund policy\n\nItems ...", title="Policies", heading_path="Refund policy"
    )
    assert text == "Policies\n\nRefund policy\n\nItems ..."
    nested = embedding_input("Premium\n\ncosts", title="Prices", heading_path="Pricing > Premium")
    assert nested == f"Prices{LABEL_SEPARATOR}Pricing\n\nPremium\n\ncosts"


def test_the_input_is_label_then_chunk_folded_for_matching() -> None:
    text = embedding_input(
        f"costs {ARABIC_7900} EGP per square metre",
        title="Finishing price list",
        heading_path="Premium",
    )
    assert text == (
        f"Finishing price list{LABEL_SEPARATOR}Premium\n\ncosts 7900 EGP per square metre"
    )
    assert embedding_input("bare chunk", title="scan_003.pdf", heading_path=None) == "bare chunk"


async def test_indexing_embeds_the_labelled_chunk_and_stores_the_chunk_alone() -> None:
    """M-R50: what the provider receives carries the label; the piece does not."""
    embeddings = FakeEmbeddings()
    claim = Claim(
        tenant_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        generation_id=uuid.uuid4(),
        number=1,
        token=uuid.uuid4(),
        attempt=1,
        content="Premium finishing costs 7900 EGP per square metre, materials included.",
        space=EmbeddingSpace(provider=OPENAI_PROVIDER, model=FAKE_MODEL, dimensions=1536),
        title="Finishing price list",
    )

    async def renew() -> bool:
        return True

    async def meter(_batch: object) -> None:
        return None

    prepared = await prepare(claim, embeddings, renew=renew, meter=meter)  # type: ignore[arg-type]

    (piece,) = prepared.pieces
    (sent,) = embeddings.embedded
    assert sent == (
        "Finishing price list\n\n"
        "Premium finishing costs 7900 EGP per square metre, materials included."
    )
    assert "Finishing price list" not in piece.content
