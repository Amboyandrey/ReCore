import { ProvidersSettings } from "./providers-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function ProvidersSettingsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <ProvidersSettings slug={slug} />;
}
