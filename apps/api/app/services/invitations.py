"""Inviting people into a workspace, and turning an accepted invite into a membership."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientRole, InvitationInvalid
from app.core.security import generate_token
from app.models import Invitation, Role, User, WorkspaceMember

INVITATION_TTL = timedelta(days=3)


def _hash_token(token: str) -> str:
    """Hash an invite token for storage — the plaintext token exists only in the invite link."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invitation(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invited_by: User,
    acting_role: Role,
    email: str,
    role: Role,
) -> tuple[Invitation, str]:
    """Create an invite and return it along with its plaintext token, visible this one time.

    Only an owner may invite someone in *as* an owner — otherwise an admin could hand out full
    control by inviting a colluding account, bypassing the same rule change_member_role enforces.
    """
    if role == Role.OWNER and acting_role != Role.OWNER:
        raise InsufficientRole()
    token = generate_token()
    invitation = Invitation(
        workspace_id=workspace_id,
        email=email,
        role=role,
        token_hash=_hash_token(token),
        invited_by=invited_by.id,
        expires_at=datetime.now(UTC) + INVITATION_TTL,
    )
    db.add(invitation)
    await db.flush()
    return invitation, token


async def list_pending_invitations(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[Invitation]:
    """List invitations for a workspace that haven't been accepted yet, most recent first."""
    stmt = (
        select(Invitation)
        .where(Invitation.workspace_id == workspace_id, Invitation.accepted_at.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_invitation_by_token(db: AsyncSession, token: str) -> Invitation:
    """Look up a still-pending, unexpired invitation by its plaintext token, or raise."""
    invitation = await db.scalar(select(Invitation).where(Invitation.token_hash == _hash_token(token)))
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.expires_at < datetime.now(UTC)
    ):
        raise InvitationInvalid()
    return invitation


async def accept_invitation(db: AsyncSession, *, invitation: Invitation, user: User) -> WorkspaceMember:
    """Turn an invitation into a membership — only the invited email may accept it."""
    if user.email.lower() != invitation.email.lower():
        raise InvitationInvalid()
    existing = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == invitation.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    invitation.accepted_at = datetime.now(UTC)
    if existing is not None:
        return existing
    member = WorkspaceMember(workspace_id=invitation.workspace_id, user_id=user.id, role=invitation.role)
    db.add(member)
    await db.flush()
    return member
