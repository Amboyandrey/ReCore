"""Import every model here so it registers on Base.metadata before Alembic autogenerate runs."""

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.credential import ProviderCredential
from app.models.extract_status import ExtractStatus
from app.models.feature_flag import FeatureFlag
from app.models.flag_override import FlagOverride
from app.models.flag_scope import FlagScope
from app.models.flag_type import FlagType
from app.models.invitation import Invitation
from app.models.llm_model import LLMModel
from app.models.message import Message
from app.models.message_role import MessageRole
from app.models.provider import Provider
from app.models.role import Role
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "Attachment",
    "Conversation",
    "ExtractStatus",
    "FeatureFlag",
    "FlagOverride",
    "FlagScope",
    "FlagType",
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
