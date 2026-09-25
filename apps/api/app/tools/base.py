"""What every tool executor produces — the shared shape execute.py and the agent loop both use,
regardless of which kind of tool actually ran."""

from dataclasses import dataclass

from app.core.config import get_settings

# Raster formats only: an SVG can carry script, and these are served back to the browser inline.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_IMAGES_PER_RESULT = 4


@dataclass(frozen=True)
class ToolImage:
    """One image a tool returned — saved as an attachment on the reply and shown to the user,
    never sent back to the model."""

    mime: str
    data: bytes


@dataclass(frozen=True)
class ToolExecutionResult:
    """The outcome of running one tool call.

    `content` is fed back to the model as the tool's result either way — an error is still a
    result the model gets to see and react to (try a different query, apologize, ask the user),
    not a reason to fail the whole generation. `images` go to the user only; `content` says they
    were produced so the model can refer to them.
    """

    ok: bool
    content: str
    images: tuple[ToolImage, ...] = ()


def accept_image(mime: str, data: bytes) -> ToolImage | None:
    """Keep an image only if it's an allowed raster format within the attachment size limit."""
    mime = mime.split(";", 1)[0].strip().lower()
    if mime not in IMAGE_MIMES or not data or len(data) > get_settings().max_attachment_size_bytes:
        return None
    return ToolImage(mime=mime, data=data)
