"""Import every model here so it registers on Base.metadata before Alembic autogenerate runs."""

from app.models.assistant import Assistant
from app.models.assistant_connector import AssistantConnector
from app.models.assistant_delegate import AssistantDelegate
from app.models.assistant_tool import AssistantTool
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.connector import Connector
from app.models.connector_chunk import ConnectorChunk
from app.models.connector_document import ConnectorDocument
from app.models.connector_kind import ConnectorKind
from app.models.connector_status import ConnectorStatus
from app.models.conversation import Conversation
from app.models.credential import ProviderCredential
from app.models.document_status import DocumentStatus
from app.models.extract_status import ExtractStatus
from app.models.feature_flag import FeatureFlag
from app.models.flag_override import FlagOverride
from app.models.flag_scope import FlagScope
from app.models.flag_type import FlagType
from app.models.invitation import Invitation
from app.models.knowledge_settings import KnowledgeSettings
from app.models.llm_model import LLMModel
from app.models.memory_credential import MemoryCredential
from app.models.message import Message
from app.models.message_role import MessageRole
from app.models.message_source import MessageSource
from app.models.model_kind import ModelKind
from app.models.provider import Provider
from app.models.role import Role
from app.models.tool import Tool
from app.models.tool_invocation import ToolInvocation
from app.models.tool_invocation_status import ToolInvocationStatus
from app.models.tool_kind import ToolKind
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "Assistant",
    "AssistantConnector",
    "AssistantDelegate",
    "AssistantTool",
    "Attachment",
    "AuditLog",
    "Connector",
    "ConnectorChunk",
    "ConnectorDocument",
    "ConnectorKind",
    "ConnectorStatus",
    "Conversation",
    "DocumentStatus",
    "ExtractStatus",
    "FeatureFlag",
    "FlagOverride",
    "FlagScope",
    "FlagType",
    "Invitation",
    "KnowledgeSettings",
    "LLMModel",
    "MemoryCredential",
    "Message",
    "MessageRole",
    "MessageSource",
    "ModelKind",
    "Provider",
    "ProviderCredential",
    "Role",
    "Tool",
    "ToolInvocation",
    "ToolInvocationStatus",
    "ToolKind",
    "UsageEvent",
    "User",
    "Workspace",
    "WorkspaceMember",
]
