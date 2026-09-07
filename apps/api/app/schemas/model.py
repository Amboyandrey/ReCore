"""Request and response shapes for the model catalog."""

import uuid

from pydantic import BaseModel, Field


class AvailableModelOut(BaseModel):
    """One model a credential's provider reports as available — not yet enabled for chat."""

    id: str
    display_name: str
    context_window: int | None


class EnableModelRequest(BaseModel):
    """What enabling a model for chat needs — pricing is optional but drives usage cost later."""

    credential_id: uuid.UUID
    provider_model_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=100)
    context_window: int | None = None
    cost_per_mtok_in: float | None = Field(default=None, ge=0)
    cost_per_mtok_out: float | None = Field(default=None, ge=0)


class ModelOut(BaseModel):
    """An enabled model, with the pricing it was registered with."""

    id: uuid.UUID
    credential_id: uuid.UUID
    provider_model_id: str
    display_name: str
    context_window: int | None
    cost_per_mtok_in: float | None
    cost_per_mtok_out: float | None
