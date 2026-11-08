import { Suspense } from "react";

import { AcceptInvitation } from "../../../components/auth/accept-invitation";
import { Shell } from "../../../components/shell";

/** APP-AUTH-06 accept an invitation; the token leaves the address bar first (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <AcceptInvitation />
      </Suspense>
    </Shell>
  );
}
