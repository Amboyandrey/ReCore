import { KnowledgeSettingsPage } from "./knowledge-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function KnowledgePage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <KnowledgeSettingsPage slug={slug} />;
}
