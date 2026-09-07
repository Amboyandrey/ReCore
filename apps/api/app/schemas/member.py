"""Request and response shapes for membership and invitation endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models import Role


class MemberOut(BaseModel):
    """One member of a workspace."""

    user_id: uuid.UUID
    email: str
    role: Role
    joined_at: datetime


class RoleUpdate(BaseModel):
    """The new role to assign a member."""

    role: Role


class InviteCreate(BaseModel):
    """Who to invite, and at what role."""

    email: EmailStr
    role: Role = Role.MEMBER


class InviteOut(BaseModel):
    """A pending invitation. `token` is populated only in the response to creating it."""

    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    token: str | None = None


class InvitePreview(BaseModel):
    """What an invite link shows before the visitor signs in — no auth required to see this."""

    workspace_name: str
    email: str
    role: Role
    expires_at: datetime
