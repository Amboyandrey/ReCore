"""Inviting people into a workspace, and turning an accepted invite into a membership."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientRole, InvitationInvalid, InvitationNotFound
from app.core.security import generate_token
from app.models import Invitation, Role, User, WorkspaceMember

INVITATION_TTL = timedelta(days=3)


def _hash_token(token: str) -> str:
    """Hash an invite token for storage — the plaintext token exists only in the invite link."""
    return hashlib.sha256(token.encode()).hexdigest()


def _is_pending(invitation: Invitation) -> bool:
    """Whether an invitation can still be accepted: not already used, not expired."""
    return invitation.accepted_at is None and invitation.expires_at >= datetime.now(UTC)


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

    Inviting an email that already has a pending invitation to this workspace re-sends it rather
    than creating a second, separate one: the existing row gets a new token, role, and expiry
    instead of the workspace ending up with several pending invites for the same address (each
    with its own now-worthless stale link) — "same email counts as one invite."
    """
    if role == Role.OWNER and acting_role != Role.OWNER:
        raise InsufficientRole()
    existing = await db.scalar(
        select(Invitation).where(
            Invitation.workspace_id == workspace_id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
        )
    )
    token = generate_token()
    if existing is not None:
        existing.role = role
        existing.token_hash = _hash_token(token)
        existing.invited_by = invited_by.id
        existing.expires_at = datetime.now(UTC) + INVITATION_TTL
        await db.flush()
        return existing, token
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
    """List invitations for a workspace that haven't been accepted yet, most recent first —
    expired-but-unaccepted ones are included too, so an admin can see and revoke stale invites."""
    stmt = (
        select(Invitation)
        .where(Invitation.workspace_id == workspace_id, Invitation.accepted_at.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def revoke_invitation(
    db: AsyncSession, *, workspace_id: uuid.UUID, invitation_id: uuid.UUID
) -> None:
    """Permanently withdraw a pending invitation — its link (if anyone still has it) stops
    working immediately, same as if it had already expired."""
    invitation = await db.scalar(
        select(Invitation).where(
            Invitation.id == invitation_id, Invitation.workspace_id == workspace_id
        )
    )
    if invitation is None:
        raise InvitationNotFound()
    await db.delete(invitation)
    await db.flush()


async def list_pending_invitations_for_email(db: AsyncSession, *, email: str) -> list[Invitation]:
    """List every still-acceptable invitation waiting for this email address, across every
    workspace — what a freshly signed-in account gets asked about, whether or not it ever
    followed the original invite link."""
    stmt = (
        select(Invitation)
        .where(Invitation.email == email, Invitation.accepted_at.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    return [i for i in (await db.scalars(stmt)).all() if _is_pending(i)]


async def get_invitation_by_token(db: AsyncSession, token: str) -> Invitation:
    """Look up a still-pending, unexpired invitation by its plaintext token, or raise."""
    invitation = await db.scalar(select(Invitation).where(Invitation.token_hash == _hash_token(token)))
    if invitation is None or not _is_pending(invitation):
        raise InvitationInvalid()
    return invitation


async def get_pending_invitation_by_id(db: AsyncSession, *, invitation_id: uuid.UUID) -> Invitation:
    """Look up a still-pending, unexpired invitation by id — the accept-without-a-token path a
    signed-in account uses when it never had the original link to begin with."""
    invitation = await db.get(Invitation, invitation_id)
    if invitation is None or not _is_pending(invitation):
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
