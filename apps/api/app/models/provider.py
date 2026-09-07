"""Which LLM backend a credential talks to."""

import enum


class Provider(enum.StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    OPENAI_COMPATIBLE = "openai_compatible"
