import { ChatThread } from "./chat-thread";

// Resolves the dynamic route params before handing off to the client-rendered content.
export default async function ChatPage({
  params,
}: {
  params: Promise<{ slug: string; conversationId: string }>;
}) {
  const { slug, conversationId } = await params;
  return <ChatThread slug={slug} conversationId={conversationId} />;
}
