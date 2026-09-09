import { AssistantsSettings } from "./assistants-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function AssistantsSettingsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <AssistantsSettings slug={slug} />;
}
