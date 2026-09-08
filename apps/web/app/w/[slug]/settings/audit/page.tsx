import { AuditSettings } from "./audit-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function AuditSettingsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <AuditSettings slug={slug} />;
}
