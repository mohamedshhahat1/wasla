"""Recognising a customer asking to stop receiving campaigns.

This is deliberately the crudest possible matcher, and the crudeness is the
design. It fires only when the *entire* message is one of a short list of words
that mean nothing else. "stop" on its own is a customer asking to be left alone;
"stop by the shop tomorrow" is not, and a matcher that looked for the word
anywhere in a message would confuse the two constantly.

The asymmetry decides where to sit between too eager and too cautious. A false
positive stops marketing to somebody who did not quite mean it — recoverable in
a moment by a colleague, and the customer can always write again. A false
negative keeps sending promotional messages to somebody who asked twice to stop,
which is how a business loses its WhatsApp number. So the matcher is narrow in
what it looks at and generous in what it accepts *there*.

What this cannot do is understand a sentence. "please take me off your list" is
not recognised, and that is an accepted limit rather than an oversight: the
alternative is a model call on every inbound message to decide something a
person can record in one click.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.repositories.consent_repository import ContactConsentRepository

logger = get_logger(__name__)

# Anything that is punctuation or whitespace around the word itself. A customer
# writing "stop." or "!!STOP!!" means what a customer writing "stop" means.
TRIM = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)

# Arabic diacritics and the tatweel, which are decorative and vary by keyboard.
ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟـ]")

# Letters an Arabic keyboard writes several ways for the same sound, folded
# so that a customer's keyboard does not decide whether their opt-out is
# honoured: the three hamza-bearing alefs become a bare alef, alef maksura
# becomes ya, and teh marbuta becomes heh.
ARABIC_FOLDING: Final = str.maketrans("أإآىة", "ااايه")

# The whole vocabulary. Short on purpose: every entry must be a phrase that
# means "stop messaging me" and cannot plausibly mean anything else on its own.
STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        # English, including the wording WhatsApp's own opt-out button sends.
        "stop",
        "stop promotions",
        "unsubscribe",
        "opt out",
        "optout",
        "no more messages",
        # Arabic. The alef variants are normalised before matching, so one
        # spelling of each is enough.
        "الغاء",
        "الغاء الاشتراك",
        "ايقاف",
        "ايقاف الرسائل",
        "توقف",
        "لا رسائل",
    }
)

# Longer than the longest phrase above, with room for punctuation. A message
# beyond this is a sentence, and a sentence is not what this matcher reads.
MAX_STOP_LENGTH: Final = 40


def normalised(text: str) -> str:
    """The message reduced to the form the vocabulary is written in.

    Case folded, stripped of surrounding punctuation, with Arabic diacritics
    removed and the alef and ya variants collapsed — a customer's keyboard
    should not decide whether their opt-out is honoured.
    """
    cleaned = unicodedata.normalize("NFKC", text).strip().casefold()
    cleaned = TRIM.sub("", cleaned)
    cleaned = ARABIC_MARKS.sub("", cleaned).translate(ARABIC_FOLDING)
    # Collapse runs of whitespace so "stop   promotions" matches.
    return " ".join(cleaned.split())


def is_stop_request(text: str | None) -> bool:
    """Whether this message, taken whole, asks to stop receiving campaigns."""
    if not text or len(text) > MAX_STOP_LENGTH:
        return False
    return normalised(text) in STOP_WORDS


async def record_opt_out(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: Channel,
    source: OptOutSource,
    via: OptOutVia,
    at: datetime,
) -> bool:
    """Record that this person wants no marketing on `channel`. Returns whether anything changed.

    The one writer of an opt-out, whichever way it arrived - a stop word, a
    tapped button, the provider's own preference record or refusal, a replay of
    retained evidence, a colleague (OMNI-030, OMNI-046). Per channel (ENT-19):
    a STOP on WhatsApp covers every WhatsApp number of the workspace and no
    other channel. Idempotent, and it never moves the timestamp: the first
    refusal is the one a dispute about a marketing message turns on. `source`
    is who decided; `via` is by which evidence.
    """
    consent = await ContactConsentRepository(session, tenant_id=tenant_id).lock(contact_id, channel)
    if consent.marketing_opt_out_at is not None:
        return False
    consent.marketing_opt_out_at = at
    consent.opt_out_source = source
    consent.opt_out_via = via
    logger.info(
        "campaign.opt_out_recorded",
        extra={
            "event": "campaign.opt_out_recorded",
            "contact_id": str(contact_id),
            "channel": channel.value,
            "source": source.value,
            "via": via.value,
        },
    )
    return True


async def record_resume(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: Channel,
    source: OptOutSource,
    via: OptOutVia,
    at: datetime,
    lifts: frozenset[OptOutVia] | None = None,
) -> bool:
    """Record a re-admission to marketing on `channel`. Returns whether an opt-out was lifted.

    The resume is always recorded, if newer than the last one: it is the
    evidence that stops a replay of older opt-out evidence from undoing it
    (OMNI-030). The opt-out on that channel - and only that channel (ENT-19) -
    is lifted when `lifts` is None (a colleague's deliberate clear) or names
    the evidence it was recorded by: the provider's own resume undoes only what
    the provider's own stop recorded, never a stop word the customer sent.
    """
    consent = await ContactConsentRepository(session, tenant_id=tenant_id).lock(contact_id, channel)
    if consent.resumed_at is None or consent.resumed_at < at:
        consent.resumed_at = at
        consent.resume_source = source
        consent.resume_via = via
    if consent.marketing_opt_out_at is None:
        return False
    if lifts is not None and consent.opt_out_via not in lifts:
        return False
    consent.marketing_opt_out_at = None
    consent.opt_out_source = None
    consent.opt_out_via = None
    logger.info(
        "campaign.opt_out_cleared",
        extra={
            "event": "campaign.opt_out_cleared",
            "contact_id": str(contact_id),
            "channel": channel.value,
            "via": via.value,
        },
    )
    return True


__all__ = [
    "MAX_STOP_LENGTH",
    "STOP_WORDS",
    "is_stop_request",
    "normalised",
    "record_opt_out",
    "record_resume",
]
