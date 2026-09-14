import { WorkflowRunsPage } from "./workflow-runs";

// Resolves the dynamic route params before handing off to the client-rendered content.
export default async function RunsPage({
  params,
}: {
  params: Promise<{ slug: string; workflowId: string }>;
}) {
  const { slug, workflowId } = await params;
  return <WorkflowRunsPage slug={slug} workflowId={workflowId} />;
}
