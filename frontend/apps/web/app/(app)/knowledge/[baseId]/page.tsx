import { BaseDetail } from "../../../../components/knowledge/base-detail";

/** APP-KB-02: the id is the knowledge base's. */
export default async function KnowledgeBasePage({
  params,
}: {
  params: Promise<{ baseId: string }>;
}) {
  const { baseId } = await params;
  return <BaseDetail id={baseId} />;
}
