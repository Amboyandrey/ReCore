"""Response shape for the audit log viewer."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    """One privileged action, as the audit viewer shows it."""

    id: uuid.UUID
    actor_id: uuid.UUID
    action: str
    target_type: str
    target_id: str
    ip: str
    event_metadata: dict[str, Any] | None
    created_at: datetime
