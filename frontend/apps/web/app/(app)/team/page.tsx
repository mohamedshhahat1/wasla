import { Suspense } from "react";

import { MembersPage } from "../../../components/team/members-page";

/** APP-TEAM-01: who is in the workspace (every member reads; the API's rules decide who acts). */
export default function TeamPage() {
  return (
    <Suspense>
      <MembersPage />
    </Suspense>
  );
}
