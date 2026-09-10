import { AssistantMemories } from "./assistant-memories";

// Resolves the dynamic route params before handing off to the client-rendered content.
export default async function AssistantMemoriesPage({
  params,
}: {
  params: Promise<{ slug: string; assistantId: string }>;
}) {
  const { slug, assistantId } = await params;
  return <AssistantMemories slug={slug} assistantId={assistantId} />;
}
