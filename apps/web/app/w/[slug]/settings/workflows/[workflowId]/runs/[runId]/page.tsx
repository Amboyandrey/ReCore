import { WorkflowRunDetailPage } from "./run-detail";

// Resolves the dynamic route params before handing off to the client-rendered content.
export default async function RunDetailRoute({
  params,
}: {
  params: Promise<{ slug: string; workflowId: string; runId: string }>;
}) {
  const { slug, workflowId, runId } = await params;
  return <WorkflowRunDetailPage slug={slug} workflowId={workflowId} runId={runId} />;
}
