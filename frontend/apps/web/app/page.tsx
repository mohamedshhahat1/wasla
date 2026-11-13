import { Suspense } from "react";

import { PageGate } from "../components/page-gate";
import { Shell } from "../components/shell";
import { SignedInHome } from "../components/workspace/signed-in-home";

/**
 * "/" (FE-2): signed out → /login, unverified → /verify, no workspace → /workspaces/new (AU-03);
 * otherwise the signed-in placeholder that FE-3's shell replaces.
 */
export default function Home() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="workspace">
          <SignedInHome />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
