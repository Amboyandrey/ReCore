import { UsageSettings } from "./usage-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function UsageSettingsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <UsageSettings slug={slug} />;
}
