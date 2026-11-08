import { notFound } from "next/navigation";

import { SessionHooks } from "./hooks";

export const dynamic = "force-dynamic";

/**
 * A test-only route (JT-03, FX-07): exposes the tab's session to Playwright - refresh now, the
 * token's fingerprint, the J6 control without the lock, the stream helper - only when the server
 * runs with WASLA_E2E_ROUTES=1 on a development build; everywhere else a 404.
 */
export default function TestOnlySession() {
  if (
    process.env["WASLA_E2E_ROUTES"] !== "1" ||
    process.env["PUBLIC_WASLA_ENV"] !== "development"
  ) {
    notFound();
  }
  return <SessionHooks />;
}
