import { apiPublicUrl } from "./config";
import type { Role } from "./workspace-client";

export type Member = { user_id: string; email: string; role: Role; joined_at: string };
export type Invitation = { id: string; email: string; role: Role; expires_at: string; token?: string | null };
export type InvitePreview = { workspace_name: string; email: string; role: Role; expires_at: string };
export type PendingInvitation = { id: string; workspace_name: string; role: Role; expires_at: string };
export type AcceptedWorkspace = { id: string; slug: string; name: string; role: Role };

export class MemberError extends Error {}

async function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${apiPublicUrl}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  throw new MemberError(body?.detail ?? "Something went wrong.");
}

// Lists every member of a workspace, in the order they joined.
export async function listMembers(workspaceId: string): Promise<Member[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/members`);
  await throwIfNotOk(res);
  return (await res.json()) as Member[];
}

// Changes a member's role.
export async function changeMemberRole(workspaceId: string, userId: string, role: Role): Promise<Member> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/members/${userId}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Member;
}

// Removes a member from the workspace.
export async function removeMember(workspaceId: string, userId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/members/${userId}`, { method: "DELETE" });
  await throwIfNotOk(res);
}

// Lists invitations that haven't been accepted yet.
export async function listInvitations(workspaceId: string): Promise<Invitation[]> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/invitations`);
  await throwIfNotOk(res);
  return (await res.json()) as Invitation[];
}

// Invites someone to the workspace; the returned token is only ever shown this once.
export async function createInvitation(workspaceId: string, email: string, role: Role): Promise<Invitation> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/invitations`, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as Invitation;
}

// Permanently withdraws a pending invitation — its link stops working immediately.
export async function revokeInvitation(workspaceId: string, invitationId: string): Promise<void> {
  const res = await api(`/api/v1/workspaces/${workspaceId}/invitations/${invitationId}`, {
    method: "DELETE",
  });
  await throwIfNotOk(res);
}

// Lists every outstanding invite waiting for the signed-in account's email, across every
// workspace — independent of whether it ever followed the original invite link.
export async function listPendingInvitationsForMe(): Promise<PendingInvitation[]> {
  const res = await api("/api/v1/invitations/pending");
  await throwIfNotOk(res);
  return (await res.json()) as PendingInvitation[];
}

// Accepts a pending invite by id — no token needed, just being signed in as the invited email.
export async function acceptPendingInvitation(invitationId: string): Promise<AcceptedWorkspace> {
  const res = await api(`/api/v1/invitations/pending/${invitationId}/accept`, { method: "POST" });
  await throwIfNotOk(res);
  return (await res.json()) as AcceptedWorkspace;
}

// Previews an invitation by its token — reachable without being signed in.
export async function previewInvitation(token: string): Promise<InvitePreview> {
  const res = await api(`/api/v1/invitations/${token}`);
  await throwIfNotOk(res);
  return (await res.json()) as InvitePreview;
}

// Accepts an invitation, joining its workspace at the offered role.
export async function acceptInvitation(token: string): Promise<AcceptedWorkspace> {
  const res = await api(`/api/v1/invitations/${token}/accept`, { method: "POST" });
  await throwIfNotOk(res);
  return (await res.json()) as AcceptedWorkspace;
}
