import { WorkspaceHome } from "./workspace-home";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function WorkspacePage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <WorkspaceHome slug={slug} />;
}
