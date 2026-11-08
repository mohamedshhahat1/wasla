import { Suspense } from "react";

import { GoogleCallback } from "../../../../components/auth/google-callback";
import { Shell } from "../../../../components/shell";

/** APP-AUTH-03 Google's return (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <GoogleCallback />
      </Suspense>
    </Shell>
  );
}
