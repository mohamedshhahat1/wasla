import { Suspense } from "react";

import { PageGate } from "../../../components/page-gate";
import { Shell } from "../../../components/shell";
import { CreateWorkspaceForm } from "../../../components/workspace/workspaces";

/** APP-WS-01 create a workspace (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="verified">
          <CreateWorkspaceForm />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
