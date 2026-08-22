"""Working out what a stored file says.

Split from `MediaService` on purpose. That service owns the row and its states -
downloading, storing, skipping, failing - and this owns the three ways a file
becomes text. Keeping them apart means the state machine can be tested without a
provider and the readers can be tested without a database.

The three paths and what each produces:

| Kind | How | Result |
| --- | --- | --- |
| Image | Responses API, image input | A description written for an agent |
| Audio | Transcription endpoint | The words that were spoken |
| Document | PDF or plain text extraction | The text layer |

Everything here returns a transcript or raises. Deciding what a failure means -
retry or give up - belongs to the caller, which is the only thing that knows how
many attempts are left.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.transcription import TranscriptionClient
from app.integrations.openai.types import Turn
from app.services import extraction

logger = get_logger(__name__)

# What can be read, and by which route. The tables live here rather than beside
# the state machine that consults them, because knowing that a PDF has a text
# layer and an OGG file has speech is this module's subject. `MediaService`
# imports them to decide what is worth downloading at all - a file nothing can
# interpret is not worth the bytes.
IMAGE_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"},
)
AUDIO_TYPES: Final = frozenset(
    {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/amr", "audio/aac", "audio/wav", "audio/webm"},
)
DOCUMENT_TYPES: Final = frozenset({"application/pdf", "text/plain"})

READABLE_TYPES: Final = IMAGE_TYPES | AUDIO_TYPES | DOCUMENT_TYPES

# Written for the reader rather than for the customer. The agent that
# eventually sees this is answering a business question, so what matters is
# what is in the picture, not how it is composed.
#
# The last line is the important one. A photograph of a price list is a
# perfectly ordinary thing for a customer to send, and a description that
# paraphrases it - "a list of prices" - throws away the entire message.
VISION_INSTRUCTIONS: Final = """
You are describing an image a customer sent to a business over WhatsApp.

Describe what is actually in it, plainly and in no more than a short paragraph.
Lead with whatever a person answering this customer would need to know: the
product, the document, the damage, the place.

If the image contains text - a price, a receipt, a screenshot, a serial number,
a label, a handwritten note - transcribe that text exactly rather than
summarising it. The specific characters are usually the entire point.

Do not guess at anything you cannot see, and do not address the customer.
""".strip()

# Bounded so one enormous description cannot dominate the conversation window
# the agent is given.
MAX_VISION_TOKENS: Final = 400

VISION_PROMPT: Final = "Describe this image."


class SilentRecordingError(ExternalServiceError):
    """A recording that contained no speech.

    Not a failure of the transcription: the file was read and there was nothing
    in it. Retrying would produce the same silence, so the caller records this
    as a decision rather than an error.
    """

    message = "No speech could be heard in this recording."


class ScannedDocumentError(ExternalServiceError):
    """A document with no text layer, such as a photograph of a page.

    Reading it again will not find text that is not there. It needs OCR, which
    is not part of this system, so the honest outcome is to say so.
    """

    message = "No text could be read from this document. It may be a scan."


@dataclass(frozen=True, slots=True)
class ReadResult:
    """What a file turned out to say, and how that was worked out."""

    transcript: str
    method: str


class MediaReader:
    """Turns bytes into text, by whichever route the type calls for.

    Both clients are optional. A worker configured for documents alone should
    not need an API key, and a test exercising one route should not have to
    provide stand-ins for the other two.
    """

    def __init__(
        self,
        *,
        responses: ResponsesClient | None = None,
        transcription: TranscriptionClient | None = None,
        vision_model: str = "gpt-4.1-mini",
    ) -> None:
        self._responses = responses
        self._transcription = transcription
        self._vision_model = vision_model

    def can_read(self, mime_type: str | None) -> bool:
        return (mime_type or "").lower() in READABLE_TYPES

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        """Read one file. Raises if its type has no route or the provider fails."""
        kind = (mime_type or "").lower()
        if kind in IMAGE_TYPES:
            return await self._describe(content=content, mime_type=kind)
        if kind in AUDIO_TYPES:
            return await self._transcribe(content=content, mime_type=kind)
        if kind in DOCUMENT_TYPES:
            return self._extract(content=content, mime_type=kind)
        raise extraction.UnreadableDocumentError()

    async def _describe(self, *, content: bytes, mime_type: str) -> ReadResult:
        if self._responses is None:
            raise ExternalServiceError("Image understanding is not configured.")

        encoded = base64.b64encode(content).decode("ascii")
        reply = await self._responses.respond(
            model=self._vision_model,
            instructions=VISION_INSTRUCTIONS,
            turns=[
                Turn(
                    role="user",
                    text=VISION_PROMPT,
                    images=(f"data:{mime_type};base64,{encoded}",),
                )
            ],
            max_output_tokens=MAX_VISION_TOKENS,
        )

        described = (reply.text or "").strip()
        if not described:
            # A refusal, or a model that produced nothing. Either way there is
            # no description, and inventing one would be worse than saying so.
            raise ExternalServiceError("The image could not be described.")
        return ReadResult(transcript=described, method="vision")

    async def _transcribe(self, *, content: bytes, mime_type: str) -> ReadResult:
        if self._transcription is None:
            raise ExternalServiceError("Transcription is not configured.")

        transcript = await self._transcription.transcribe(content=content, mime_type=mime_type)
        spoken = transcript.text.strip()
        if not spoken:
            # Silence, or a recording of nothing but background noise. A real
            # outcome rather than an error, and the caller records it as a file
            # that was read and had nothing in it.
            raise SilentRecordingError()
        return ReadResult(transcript=spoken, method="transcription")

    def _extract(self, *, content: bytes, mime_type: str) -> ReadResult:
        text = extraction.extract_document(content=content, mime_type=mime_type)
        if not text:
            raise ScannedDocumentError()
        return ReadResult(transcript=text, method="extraction")
