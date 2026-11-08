import { Suspense } from "react";

import { SignupForm } from "../../components/auth/signup-form";
import { PageGate } from "../../components/page-gate";
import { Shell } from "../../components/shell";

/** APP-AUTH-01 sign up (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="signed-out">
          <SignupForm />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
