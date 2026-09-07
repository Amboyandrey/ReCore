import { InviteAccept } from "./invite-accept";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function InvitePage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <InviteAccept token={token} />;
}
