"""Conversation and messaging endpoints.

Every route is workspace-scoped through the active workspace dependency, so a
conversation id belonging to another workspace answers not-found.

These are operational routes: any member of the workspace may read, reply, hand
off and assign. Restricting them to admins would stop the people who actually
staff an inbox from using it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    InboxServiceDep,
    MediaServiceDep,
    MediaStorageDep,
    MessagingServiceDep,
    SentimentServiceDep,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.sentiment import ConversationPriority
from app.schemas.conversation import (
    AssignmentRequest,
    ConversationRead,
    CursorPage,
    MessageRead,
    ModeUpdateRequest,
    PriorityUpdateRequest,
    SendTemplateRequest,
    SendTextRequest,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
# Opaque to callers: it comes from a previous page's `next_cursor` and is not
# meant to be constructed by hand. Bounded so a long query string is rejected
# before any decoding is attempted.
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]

# WhatsApp's own caption limit.
MAX_CAPTION_LENGTH = 1024
# Bounded here as well as in the media settings, because this limit protects the
# API process rather than the store: the whole upload is held in memory to be
# handed to Meta, and an unbounded one is a way to exhaust it.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


@router.get("", response_model=CursorPage[ConversationRead])
async def list_conversations(
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    priority: ConversationPriority | None = None,
) -> CursorPage[ConversationRead]:
    """Open conversations, most recently active first.

    Paged by cursor rather than offset: the inbox is written to while it is
    being read, and an offset would let an arriving conversation push a row
    across the page boundary.

    `priority` narrows the list without reordering it, so the flagged queue can
    be worked deliberately while the default view stays as it was.
    """
    page = await inbox.list_conversations(limit=limit, cursor=cursor, priority=priority)
    return CursorPage[ConversationRead](
        items=[
            ConversationRead.from_model(
                conversation,
                service_window_open=messaging.window_open(conversation),
            )
            for conversation in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: uuid.UUID,
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    conversation = await inbox.get_conversation(conversation_id)
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )


@router.get("/{conversation_id}/messages", response_model=CursorPage[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    inbox: InboxServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> CursorPage[MessageRead]:
    """Most recent messages first."""
    page = await inbox.list_messages(
        conversation_id=conversation_id,
        limit=limit,
        cursor=cursor,
    )
    return CursorPage[MessageRead](
        items=[MessageRead.from_model(message) for message in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_text(
    conversation_id: uuid.UUID,
    payload: SendTextRequest,
    workspace: ActiveWorkspaceDep,
    messaging: MessagingServiceDep,
) -> MessageRead:
    """Send free text, allowed only inside the 24-hour service window.

    Answers 201 even when Meta rejects the message: the attempt is recorded, and
    the returned status says whether it was sent or failed.
    """
    message = await messaging.send_text(
        conversation_id=conversation_id,
        body=payload.body,
        preview_url=payload.preview_url,
        sent_by_id=workspace.user.id,
    )
    return MessageRead.from_model(message)


@router.post(
    "/{conversation_id}/messages/template",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_template(
    conversation_id: uuid.UUID,
    payload: SendTemplateRequest,
    workspace: ActiveWorkspaceDep,
    messaging: MessagingServiceDep,
) -> MessageRead:
    """Send an approved template, which is valid outside the service window."""
    message = await messaging.send_template(
        conversation_id=conversation_id,
        name=payload.name,
        language=payload.language,
        components=payload.components,
        sent_by_id=workspace.user.id,
    )
    return MessageRead.from_model(message)


@router.post(
    "/{conversation_id}/messages/media",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def send_media(
    conversation_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    messaging: MessagingServiceDep,
    storage: MediaStorageDep,
    file: Annotated[UploadFile, File()],
    caption: Annotated[str | None, Form(max_length=MAX_CAPTION_LENGTH)] = None,
) -> MessageRead:
    """Send an attachment, which Meta receives as an upload rather than a link.

    Multipart rather than JSON: base64 in a request body inflates a file by a
    third and has to be held in memory twice.

    An attachment is a free-form message, so the 24-hour service window applies
    exactly as it does to text. Answers 201 even when Meta rejects it - the
    attempt is recorded, and the returned status says whether it was sent.
    """
    content = await file.read()
    if not content:
        raise ValidationError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValidationError("This file is too large to send.")

    message = await messaging.send_media(
        conversation_id=conversation_id,
        content=content,
        # `content_type` is what the browser claimed. It decides how Meta
        # renders the file, not how anything here reads it, and an unsupported
        # value is refused by the service rather than guessed at.
        mime_type=file.content_type or "application/octet-stream",
        filename=file.filename,
        caption=caption,
        sent_by_id=workspace.user.id,
        storage=storage,
    )
    return MessageRead.from_model(message)


@router.get("/{conversation_id}/media/{media_id}")
async def download_media(
    conversation_id: uuid.UUID,
    media_id: uuid.UUID,
    media_service: MediaServiceDep,
) -> Response:
    """The stored bytes of one attachment.

    Streamed back through the application rather than served from a public URL.
    A customer's photograph is workspace data, and a link that needs no
    authentication is a link that can be forwarded out of the workspace.

    `Content-Disposition: attachment` is not decoration. Serving a customer-
    supplied file inline invites the browser to render it on this origin, which
    turns an uploaded HTML file into a script running against the session
    viewing it.
    """
    media = await media_service.get(media_id)
    if media.conversation_id != conversation_id:
        # The media exists in this workspace but not on this conversation.
        # Answered as not-found rather than as a mismatch, which would confirm
        # the id names something real.
        raise NotFoundError()

    content = await media_service.read(media)
    return Response(
        content=content,
        media_type=media.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": "attachment",
            # Belt and braces with the disposition above: a file that is never
            # sniffed is never re-typed into something executable.
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{conversation_id}/mode", response_model=ConversationRead)
async def set_mode(
    conversation_id: uuid.UUID,
    payload: ModeUpdateRequest,
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    """Hand the conversation to a human, or return it to the AI."""
    conversation = await inbox.set_mode(
        conversation_id=conversation_id,
        mode=payload.mode,
        handoff_reason=payload.handoff_reason,
    )
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )


@router.post("/{conversation_id}/priority", response_model=ConversationRead)
async def set_priority(
    conversation_id: uuid.UUID,
    payload: PriorityUpdateRequest,
    sentiment: SentimentServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    """Set the priority by hand.

    The only way it comes down. Assessment raises priority on a bad reading and
    never lowers it on a good one, so returning a conversation to the ordinary
    queue is a decision somebody makes after looking at it.
    """
    conversation = await sentiment.set_priority(
        conversation_id=conversation_id,
        priority=payload.priority,
    )
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )


@router.post("/{conversation_id}/assignment", response_model=ConversationRead)
async def assign(
    conversation_id: uuid.UUID,
    payload: AssignmentRequest,
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    """Assign to a member of this workspace, or clear the assignment."""
    conversation = await inbox.assign(
        conversation_id=conversation_id,
        assigned_to_id=payload.assigned_to_id,
    )
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )


@router.post("/{conversation_id}/close", response_model=ConversationRead)
async def close_conversation(
    conversation_id: uuid.UUID,
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    conversation = await inbox.close(conversation_id)
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )


@router.post("/{conversation_id}/reopen", response_model=ConversationRead)
async def reopen_conversation(
    conversation_id: uuid.UUID,
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
) -> ConversationRead:
    conversation = await inbox.reopen(conversation_id)
    return ConversationRead.from_model(
        conversation,
        service_window_open=messaging.window_open(conversation),
    )
