"""Who a flag override targets — one specific user, or every member of one workspace."""

import enum


class FlagScope(enum.StrEnum):
    """The two override tiers the resolution order checks before falling back to a default."""

    USER = "user"
    WORKSPACE = "workspace"
