"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";

import { Button, ErrorState } from "@wasla/ui";

import { decideRoute, type PageNeed } from "../lib/routing";
import { useSession } from "./session-provider";

/** AU-03: shows the page when the session state allows it, else sends the visitor on. */
export function PageGate({ need, children }: { need: PageNeed; children: ReactNode }) {
  const { status, profile, profileLoaded, session } = useSession();
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const t = useTranslations("auth");
  const [retrying, setRetrying] = useState(false);
  const decision = decideRoute(
    need,
    {
      status,
      verified: profile ? Boolean(profile.email_verified_at) : profileLoaded ? false : null,
      workspaceCount: profile ? (profile.workspaces?.length ?? 0) : profileLoaded ? 0 : null,
    },
    pathname,
    search.get("next"),
  );
  const target = decision.kind === "go" ? decision.to : null;
  useEffect(() => {
    if (target) router.replace(target);
  }, [target, router]);

  if (status === "connection-lost") {
    return (
      <ErrorState
        headingLevel={1}
        title={t("connectionLostTitle")}
        description={t("connectionLostBody")}
        action={
          <Button
            disabled={retrying}
            onClick={() => {
              setRetrying(true);
              void session.restore().finally(() => setRetrying(false));
            }}
          >
            {t("retry")}
          </Button>
        }
      />
    );
  }
  if (decision.kind !== "show") {
    return (
      <p role="status" aria-live="polite" className="py-10 text-center text-md text-text-muted">
        {t("loading")}
      </p>
    );
  }
  return <>{children}</>;
}

/** The heading and body of one auth screen. */
export function AuthCard({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="mx-auto flex w-full max-w-prose flex-col gap-5 py-6">
      <h1 className="m-0 text-2xl font-semibold text-text">{title}</h1>
      {description ? <div className="m-0 text-md text-text-muted">{description}</div> : null}
      {children}
    </section>
  );
}
