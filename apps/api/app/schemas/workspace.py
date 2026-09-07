"""Request and response shapes for the workspace endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Role


class WorkspaceCreate(BaseModel):
    """What creating a workspace needs: just a display name — the slug is derived from it."""

    name: str = Field(min_length=1, max_length=100)


class WorkspaceOut(BaseModel):
    """A workspace as seen by one of its members, including their own role in it."""

    id: uuid.UUID
    slug: str
    name: str
    role: Role
    created_at: datetime
