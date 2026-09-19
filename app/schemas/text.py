"""Free text a person types into the CRM, refused at the boundary when it cannot be stored.

A NUL in a handoff reason, a note, a lead's name, a tag or a follow-up's body
reached PostgreSQL, which cannot hold one, and the request failed with a 500
(CRM-12). The rule is the one the knowledge base already applies (RAG-11) and
the agent's tools apply to what a model writes: NUL and text that is not valid
Unicode are refused, never silently stripped - they are a client bug or a
probe, and the caller should hear which field was wrong. Everything else a
person can type is kept exactly: Arabic, emoji, combining marks, line breaks.

Lengths are not this module's business. Each field keeps its own `max_length`
beside it, where a reader of the schema looks for it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from app.core.text_safety import storable_problem


def storable(value: str) -> str:
    """Refuse text PostgreSQL cannot hold or Unicode cannot encode, as a 422."""
    problem = storable_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


#: A `str` that is refused, with a 422 naming the field, if it cannot be stored.
StorableText = Annotated[str, AfterValidator(storable)]

__all__ = ["StorableText", "storable"]
