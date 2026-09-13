"""What an enabled model is for — whether it belongs in the chat picker or the embedding picker."""

import enum


class ModelKind(enum.StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"
