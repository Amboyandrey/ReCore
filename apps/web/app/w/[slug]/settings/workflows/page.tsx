import { WorkflowsSettingsPage } from "./workflows-settings";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function WorkflowsPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <WorkflowsSettingsPage slug={slug} />;
}
