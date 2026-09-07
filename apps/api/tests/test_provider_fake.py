"""FakeProvider behaves deterministically — used by credential-flow tests in the next commit."""

import pytest

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
