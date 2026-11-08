import { Suspense } from "react";

import { ResetForm } from "../../components/auth/password-reset";
import { Shell } from "../../components/shell";

/** APP-AUTH-05 reset password; the token leaves the address bar first (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <ResetForm />
      </Suspense>
    </Shell>
  );
}
