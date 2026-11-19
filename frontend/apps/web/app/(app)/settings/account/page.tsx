import { Suspense } from "react";

import { AccountPage } from "../../../../components/account/account-page";

/** APP-ACC-01: the signed-in person's own account (everybody). */
export default function AccountSettingsPage() {
  return (
    <Suspense>
      <AccountPage />
    </Suspense>
  );
}
