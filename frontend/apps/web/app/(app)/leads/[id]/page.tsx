import { LeadDetail } from "../../../../components/customers/lead-detail";

/** APP-CUS-03: the id is the lead's. */
export default async function LeadPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <LeadDetail id={id} />;
}
