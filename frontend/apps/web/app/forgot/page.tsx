import { Suspense } from "react";

import { ForgotForm } from "../../components/auth/password-reset";
import { PageGate } from "../../components/page-gate";
import { Shell } from "../../components/shell";

/** APP-AUTH-05 forgot password (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="signed-out">
          <ForgotForm />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
