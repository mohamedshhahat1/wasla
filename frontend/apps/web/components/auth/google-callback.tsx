"use client";

import NextLink from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { unwrap } from "@wasla/api-client";
import { linkClasses } from "@wasla/ui";

import { afterSignIn } from "../../lib/routing";
import { AuthCard } from "../page-gate";
import { useSession } from "../session-provider";
import { FormAlert } from "./form-alert";
import { useParamsFromAddress } from "./use-token-from-address";

/**
 * APP-AUTH-03's return: code and state leave the address bar first, then go to
 * /session/google/callback with the relayed binding cookie (SR-07). A cancelled consent
 * (error=access_denied) goes back to /login with a neutral note.
 */
export function GoogleCallback() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const router = useRouter();
  const params = useParamsFromAddress(["code", "state", "error"] as const);
  const [error, setError] = useState<unknown>(null);
  const started = useRef(false);

  useEffect(() => {
    if (!params || started.current) return;
    started.current = true;
    if (params.error || !params.code || !params.state) {
      router.replace("/login?note=google_cancelled");
      return;
    }
    void (async () => {
      try {
        await session.googleCallback(params.code!, params.state!);
        const me = await unwrap(session.api.GET("/api/v1/auth/me"));
        router.replace(
          afterSignIn({
            verified: Boolean(me.email_verified_at),
            workspaceCount: me.workspaces?.length ?? 0,
          }),
        );
      } catch (caught) {
        setError(caught);
      }
    })();
  }, [params, router, session]);

  return (
    <AuthCard title={t("google.title")}>
      {error ? (
        <>
          <FormAlert error={error} />
          <NextLink href="/login" className={linkClasses("text-sm")}>
            {t("signup.toLogin")}
          </NextLink>
        </>
      ) : (
        <p role="status" className="m-0 text-md text-text-muted">
          {t("loading")}
        </p>
      )}
    </AuthCard>
  );
}
