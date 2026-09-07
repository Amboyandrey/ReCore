"""FakeProvider behaves deterministically — used by credential-flow and chat-orchestration tests."""

import pytest

from app.providers.base import ChatMessage, Done, TextDelta, Usage
from app.providers.fake import VALID_KEY, FakeProvider


async def test_valid_key_validates_and_lists_models() -> None:
    """The one recognized key validates and returns its fixed model list."""
    provider = FakeProvider(api_key=VALID_KEY)

    check = await provider.validate()
    models = await provider.list_models()

    assert check.ok is True
    assert {m.id for m in models} == {"fake-small", "fake-large"}


async def test_any_other_key_fails_validation() -> None:
    """Anything but the recognized key fails, deterministically."""
    provider = FakeProvider(api_key="wrong-key")

    check = await provider.validate()

    assert check.ok is False


async def test_listing_models_for_an_invalid_key_raises() -> None:
    """list_models refuses to pretend an invalid credential has models."""
    provider = FakeProvider(api_key="wrong-key")

    with pytest.raises(RuntimeError):
        await provider.list_models()


async def test_stream_yields_multiple_deltas_then_usage_then_done() -> None:
    """The fake streams its fixed reply as more than one chunk — real streaming, not one blob."""
    provider = FakeProvider(api_key=VALID_KEY)
    messages = [ChatMessage(role="user", content="hi there")]

    chunks = [c async for c in provider.stream(model="fake-small", messages=messages, max_tokens=50)]

    deltas = [c for c in chunks if isinstance(c, TextDelta)]
    assert len(deltas) > 1
    assert "".join(d.text for d in deltas) == "Hello from the fake provider. "
    assert isinstance(chunks[-2], Usage)
    assert chunks[-1] == Done(finish_reason="stop")
