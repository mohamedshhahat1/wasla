import { ReturnPage } from "../../../../../components/billing/return-page";

/**
 * APP-BILL-02's return (RT-01): exactly this path, built by the backend. The page decides from the
 * payment the API reads and never from the address it was sent to.
 */
export default function BillingReturnPage() {
  return <ReturnPage />;
}
