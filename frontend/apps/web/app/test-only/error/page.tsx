import { notFound } from "next/navigation";

import { Thrower } from "./thrower";

export const dynamic = "force-dynamic";

/**
 * A test-only route (TE-03): renders an error with a request id only when the server runs with
 * WASLA_E2E_ROUTES=1; everywhere else - every real deployment - it is a 404.
 */
export default function TestOnlyError() {
  if (process.env["WASLA_E2E_ROUTES"] !== "1") notFound();
  return <Thrower />;
}
