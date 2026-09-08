"""Request and response shapes for conversations, messages, and sending a chat turn."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import MessageRole


class ConversationCreate(BaseModel):
    """What starting a conversation needs: which model to use, and an optional system prompt."""

    model_id: uuid.UUID
    system_prompt: str | None = None


class ConversationOut(BaseModel):
    """A chat thread."""

    id: uuid.UUID
    title: str
    model_id: uuid.UUID
    system_prompt: str | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """One turn in a conversation."""

    id: uuid.UUID
    role: MessageRole
    content: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    finish_reason: str | None
    error: str | None
    created_at: datetime


class SendMessageRequest(BaseModel):
    """The user's next message, plus any already-uploaded attachments to send alongside it."""

    content: str = Field(min_length=1)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list)


class ActiveGenerationOut(BaseModel):
    """Whether a generation is currently running for a conversation, for a page that just loaded."""

    generation_id: str | None
