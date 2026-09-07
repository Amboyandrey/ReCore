"""The workspace role hierarchy — one enum and one ordering, shared by every membership check."""

import enum


class Role(enum.StrEnum):
    """A member's standing within one workspace, from least to most privileged."""

    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


_ORDER = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


def role_at_least(role: Role, minimum: Role) -> bool:
    """Report whether `role` meets or exceeds `minimum` in the hierarchy."""
    return _ORDER[role] >= _ORDER[minimum]
