import { Suspense } from "react";

import { VerifyForm } from "../../components/auth/verify-form";
import { PageGate } from "../../components/page-gate";
import { Shell } from "../../components/shell";

/** APP-AUTH-04 verify by code (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="signed-in">
          <VerifyForm />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
