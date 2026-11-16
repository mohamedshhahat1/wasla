import { ContactDetail } from "../../../../components/customers/contact-detail";

/** APP-CUS-01, the detail: the id is the contact's. */
export default async function ContactPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ContactDetail id={id} />;
}
