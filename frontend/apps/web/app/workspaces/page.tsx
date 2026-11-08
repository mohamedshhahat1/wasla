import { Suspense } from "react";

import { PageGate } from "../../components/page-gate";
import { Shell } from "../../components/shell";
import { WorkspaceList } from "../../components/workspace/workspaces";

/** APP-WS-02 choose or switch the workspace (FE-2). */
export default function Page() {
  return (
    <Shell>
      <Suspense>
        <PageGate need="verified">
          <WorkspaceList />
        </PageGate>
      </Suspense>
    </Shell>
  );
}
