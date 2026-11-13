import { ConnectionDetail } from "../../../../components/app/channels/connection-detail";

/** APP-CH-03: the id is the channel connection's - the WhatsApp account's own id. */
export default async function ConnectionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ConnectionDetail id={id} />;
}
