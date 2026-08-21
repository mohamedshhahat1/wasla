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

from fastapi import APIRouter, Query, status

from app.api.dependencies import ActiveWorkspaceDep, InboxServiceDep, MessagingServiceDep
from app.schemas.conversation import (
    AssignmentRequest,
    ConversationRead,
    MessageRead,
    ModeUpdateRequest,
    SendTemplateRequest,
    SendTextRequest,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=list[ConversationRead])
async def list_conversations(
    inbox: InboxServiceDep,
    messaging: MessagingServiceDep,
    limit: LimitQuery = 50,
) -> list[ConversationRead]:
    """Open conversations, most recently active first."""
    conversations = await inbox.list_conversations(limit=limit)
    return [
        ConversationRead.from_model(
            conversation,
            service_window_open=messaging.window_open(conversation),
        )
        for conversation in conversations
    ]


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


@router.get("/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    inbox: InboxServiceDep,
    limit: LimitQuery = 50,
) -> list[MessageRead]:
    """Most recent messages first."""
    messages = await inbox.list_messages(conversation_id=conversation_id, limit=limit)
    return [MessageRead.from_model(message) for message in messages]


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
