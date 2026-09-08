"""Request and response shapes for conversations, messages, and sending a chat turn."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import MessageRole


class ConversationCreate(BaseModel):
    """What starting a conversation needs: which model to use, and an optional system prompt."""

    model_id: uuid.UUID
    system_prompt: str | None = None


class ConversationUpdate(BaseModel):
    """Changing a conversation after it's started — only the fields actually sent are touched
    (built with `exclude_unset`), so omitting one leaves it as it was.

    `model_id` switches which of the workspace's enabled models the conversation talks to
    mid-session; history already sent to the old model isn't rewritten or resent, only the turn
    that follows the switch goes to the new one. `shared` is its owner opting the conversation
    into being visible (and writable) by the rest of the workspace, or back out of it — see
    Conversation's own docstring.
    """

    model_id: uuid.UUID | None = None
    shared: bool | None = None


class ConversationOut(BaseModel):
    """A chat thread.

    `user_id` and `shared` are what a client uses to know whether *it* owns this conversation
    (and so can toggle sharing or see it in a private list) versus is only viewing one a
    teammate shared.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    model_id: uuid.UUID
    system_prompt: str | None
    shared: bool
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
