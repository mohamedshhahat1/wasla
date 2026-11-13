import type { ReactNode } from "react";

import { AppFrame } from "../../components/app/app-frame";

/**
 * The signed-in pages (FE-3, SH-01): home and the channels, inside the app shell. The auth pages,
 * /verify, /workspaces and /workspaces/new keep FE-2's frame.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppFrame>{children}</AppFrame>;
}
