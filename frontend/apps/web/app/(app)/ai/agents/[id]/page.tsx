import { AgentDetail } from "../../../../../components/ai/agent-detail";

/** APP-AI-01: the id is the agent's. */
export default async function AgentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AgentDetail id={id} />;
}
