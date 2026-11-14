import type { ReactNode } from "react";

import { InboxScreen } from "../../../components/inbox/inbox-screen";

/**
 * /inbox and /inbox/[id] (APP-INBOX-01 .. 03, IL-01): one screen kept mounted across both, so
 * opening another conversation never drops the tab's pending sends or media.
 */
export default function InboxLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <InboxScreen />
      {children}
    </>
  );
}
