import { MembersSettings } from "./members-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function MembersSettingsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <MembersSettings slug={slug} />;
}
