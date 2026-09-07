"""Who sent one turn in a conversation — distinct from workspace Role (viewer/admin/...)."""

import enum


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
