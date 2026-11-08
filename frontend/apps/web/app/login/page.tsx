import { Suspense } from "react";

import { LoginForm } from "../../components/auth/login-form";
import { PageGate } from "../../components/page-gate";
import { Shell } from "../../components/shell";

/** APP-AUTH-02 log in (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="signed-out">
          <LoginForm />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
