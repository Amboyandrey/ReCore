"""Import every model here so it registers on Base.metadata before Alembic autogenerate runs."""

from app.models.invitation import Invitation
from app.models.role import Role
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = ["Invitation", "Role", "User", "Workspace", "WorkspaceMember"]
