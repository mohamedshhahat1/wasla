"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Badge, SwitcherMenu, Toast } from "@wasla/ui";

import { switchTo } from "../../lib/switch";
import { useErrorText } from "../auth/form-alert";
import { useSession } from "../session-provider";
import { useWorkspace } from "./workspace-context";

/**
 * The workspace switcher in the top bar (SH-05, FEF-19): every workspace of /auth/me with its
 * role and, when suspended, a "Suspended" badge (text and icon). A suspended workspace stays
 * selectable - the shell then shows its state (GT-03). Switching runs FE-2's switch after this
 * workspace's stream is closed (ST-05); the other tabs follow with FE-2's notice (WK-02).
 */
export function WorkspaceSwitcher() {
  const t = useTranslations("app");
  const auth = useTranslations("auth");
  const describe = useErrorText();
  const { session, profile, reloadProfile } = useSession();
  const { workspaceId, leaveWorkspace, rejoinWorkspace } = useWorkspace();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const workspaces = profile?.workspaces ?? [];
  const toEntry = (item: (typeof workspaces)[number]) => ({
    value: item.slug,
    name: item.name,
    detail: auth(`roles.${item.role}`),
    ...(item.status === "suspended"
      ? { badge: <Badge tone="warning">{t("switcher.suspended")}</Badge> }
      : {}),
  });
  const current = workspaces.find((item) => item.id === workspaceId);
  return (
    <>
      <SwitcherMenu
        label={t("switcher.label")}
        current={current ? toEntry(current) : null}
        workspaces={workspaces.map(toEntry)}
        disabled={busy}
        onSelect={(slug) => {
          setBusy(true);
          setFailure(null);
          void switchTo(slug, {
            leave: leaveWorkspace,
            switchWorkspace: (target) => session.switchWorkspace(target),
            rejoin: rejoinWorkspace,
          })
            .then(() => reloadProfile())
            .then(() => router.push("/"))
            .catch((error: unknown) => setFailure(describe(error).text))
            .finally(() => setBusy(false));
        }}
        actions={[
          {
            key: "create",
            label: t("switcher.create"),
            onSelect: () => router.push("/workspaces/new"),
          },
          { key: "all", label: t("switcher.all"), onSelect: () => router.push("/workspaces") },
        ]}
      />
      <Toast
        open={failure !== null}
        onOpenChange={(open) => {
          if (!open) setFailure(null);
        }}
        tone="danger"
        title={failure ?? ""}
        closeLabel={t("close")}
      />
    </>
  );
}
