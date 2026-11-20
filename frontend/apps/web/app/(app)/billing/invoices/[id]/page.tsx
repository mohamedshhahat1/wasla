import { InvoicePage } from "../../../../../components/billing/invoices";

/** APP-BILL-04: one invoice, its payments, print and the refund review (the owner's). */
export default async function BillingInvoicePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <InvoicePage id={id} />;
}
