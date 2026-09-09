"""ChatMessage's own invariants: images only ride on a user turn, tool_calls only on an
assistant turn, and a tool turn always carries both tool_call_id and tool_name (or neither)."""

import pytest

from app.providers.base import ChatMessage, ImagePart, ToolCall

_IMAGE = ImagePart(mime="image/png", data=b"\x89PNG\r\n\x1a\n")
_CALL = ToolCall(id="call_1", name="get_weather", arguments={"city": "Paris"})


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


def test_an_assistant_message_can_carry_tool_calls() -> None:
    """The normal case for a replayed tool-calling turn — no exception."""
    message = ChatMessage(role="assistant", content="", tool_calls=(_CALL,))

    assert message.tool_calls == (_CALL,)


def test_a_user_message_cannot_carry_tool_calls() -> None:
    """Only the assistant ever asks for a tool — enforced here once."""
    with pytest.raises(ValueError, match="assistant"):
        ChatMessage(role="user", content="fine", tool_calls=(_CALL,))


def test_a_tool_message_needs_both_tool_call_id_and_tool_name() -> None:
    """OpenAI/Anthropic match a result by id, Google by name — a tool turn carries both rather
    than forcing one adapter to thread the name through from several messages back."""
    with pytest.raises(ValueError, match="tool_call_id"):
        ChatMessage(role="tool", content="42 degrees", tool_name="get_weather")
    with pytest.raises(ValueError, match="tool_call_id"):
        ChatMessage(role="tool", content="42 degrees", tool_call_id="call_1")


def test_a_tool_message_with_both_ids_is_fine() -> None:
    message = ChatMessage(
        role="tool", content="42 degrees", tool_call_id="call_1", tool_name="get_weather"
    )

    assert message.tool_call_id == "call_1"
    assert message.tool_name == "get_weather"


def test_a_non_tool_message_cannot_carry_tool_call_id_or_tool_name() -> None:
    """Those two fields are exclusively a tool turn's — carrying either on any other role is
    always a mistake, not just an unused field."""
    with pytest.raises(ValueError, match="tool turn"):
        ChatMessage(role="assistant", content="fine", tool_call_id="call_1")
    with pytest.raises(ValueError, match="tool turn"):
        ChatMessage(role="user", content="fine", tool_name="get_weather")
