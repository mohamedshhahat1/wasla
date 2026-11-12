"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ReactNode } from "react";

import { AuthLayout, BrandPanel, Button, ErrorState } from "@wasla/ui";

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

  // FEF-17: an auth page needs no session, so it keeps its form when the session routes cannot
  // answer (no secret on a preview, an API outage); a submit then shows that error by code.
  if (status === "connection-lost" && need !== "signed-out") {
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
  const common = useTranslations("common");
  return (
    <AuthLayout brand={<BrandPanel promise={common("brandPromise")} />}>
      <section className="flex w-full flex-col gap-6">
        <div className="flex flex-col gap-2">
          <h1 className="m-0 text-3xl font-semibold leading-tight tracking-tight text-text">
            {title}
          </h1>
          {description ? <div className="m-0 text-md text-text-muted">{description}</div> : null}
        </div>
        {children}
      </section>
    </AuthLayout>
  );
}
