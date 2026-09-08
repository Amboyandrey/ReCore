"""ChatMessage's own invariant: images can only ever ride on a user turn."""

import pytest

from app.providers.base import ChatMessage, ImagePart

_IMAGE = ImagePart(mime="image/png", data=b"\x89PNG\r\n\x1a\n")


def test_a_user_message_can_carry_images() -> None:
    """The normal case — no exception, the image is just there."""
    message = ChatMessage(role="user", content="What's in this?", images=(_IMAGE,))

    assert message.images == (_IMAGE,)


def test_an_assistant_message_cannot_carry_images() -> None:
    """Anthropic and OpenAI both reject images outside a user turn — enforced here once, so no
    adapter (or history-replay path) has to defend against it independently."""
    with pytest.raises(ValueError, match="user"):
        ChatMessage(role="assistant", content="Here's what I see.", images=(_IMAGE,))


def test_a_system_message_cannot_carry_images() -> None:
    """A system prompt has nowhere to put an image either."""
    with pytest.raises(ValueError, match="user"):
        ChatMessage(role="system", content="Be terse.", images=(_IMAGE,))


def test_a_message_with_no_images_needs_no_role_restriction() -> None:
    """The common case (text only) is never affected by the guard, on any role."""
    ChatMessage(role="user", content="fine")
    ChatMessage(role="assistant", content="fine")
    ChatMessage(role="system", content="fine")
