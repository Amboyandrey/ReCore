"""Which models an eval run uses, and how each is reached in live, record, or replay mode."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.evals.cassette import RecordingProvider, ReplayProvider
from app.models.provider import Provider
from app.providers.base import LLMProvider
from app.providers.registry import build_provider

Mode = Literal["live", "record", "replay"]


class MissingKeyError(Exception):
    """A live or record run needs the env var named by a model's `key_env`, and it isn't set."""


class ModelSpec(BaseModel):
    """One model under test or the judge; `key_env` names an env var, the key itself never appears here."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: Provider
    provider_model_id: str
    base_url: str | None = None
    key_env: str
    cost_per_mtok_in: float
    cost_per_mtok_out: float


class EvalModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelSpec]
    judge: ModelSpec


def load_models(path: Path) -> EvalModels:
    return EvalModels.model_validate(yaml.safe_load(path.read_text()))


def build_adapter(spec: ModelSpec, mode: Mode, cassette_path: Path) -> LLMProvider:
    """Replay needs no key; live and record call the real provider adapter for this spec."""
    if mode == "replay":
        return ReplayProvider(cassette_path)
    api_key = os.environ.get(spec.key_env)
    if not api_key:
        raise MissingKeyError(f"set {spec.key_env} to run {spec.id} in {mode} mode")
    adapter = build_provider(spec.provider, api_key=api_key, base_url=spec.base_url)
    return RecordingProvider(adapter, cassette_path) if mode == "record" else adapter
