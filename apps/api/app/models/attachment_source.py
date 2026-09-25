"""Where an attachment came from — what decides whether it belongs to a user's message or a tool's."""

import enum


class AttachmentSource(enum.StrEnum):
    """An upload rides along with the user's next message; a tool image belongs to the tool call
    that produced it and is shown under it, never re-sent to the model."""

    UPLOAD = "upload"
    TOOL = "tool"
