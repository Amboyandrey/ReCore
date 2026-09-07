"""Import every model here so it registers on Base.metadata before Alembic autogenerate runs."""

from app.models.conversation import Conversation
from app.models.credential import ProviderCredential
from app.models.invitation import Invitation
from app.models.llm_model import LLMModel
from app.models.message import Message
from app.models.message_role import MessageRole
from app.models.provider import Provider
from app.models.role import Role
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "Conversation",
    "Invitation",
    "LLMModel",
    "Message",
    "MessageRole",
    "Provider",
    "ProviderCredential",
    "Role",
    "User",
    "Workspace",
    "WorkspaceMember",
]
