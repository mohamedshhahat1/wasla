"""The event stream's ticket (RT-01)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EventTicketRead(BaseModel):
    """A single-use ticket for `GET /events/stream?ticket=`; never logged."""

    ticket: str
    expires_at: datetime
