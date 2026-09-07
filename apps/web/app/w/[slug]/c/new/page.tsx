import { NewChat } from "./new-chat";

// Resolves the dynamic route param before handing off to the client-rendered content.
export default async function NewChatPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <NewChat slug={slug} />;
}
