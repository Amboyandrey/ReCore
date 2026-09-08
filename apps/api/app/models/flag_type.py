"""What kind of value a flag resolves to — only booleans for now, but a distinct enum column
keeps room for a string/number/json variant later without a data migration."""

import enum


class FlagType(enum.StrEnum):
    """The value shape stored in a flag's `default_value` and its overrides."""

    BOOLEAN = "boolean"
