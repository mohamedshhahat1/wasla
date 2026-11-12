"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { publicEnv } from "../lib/env";

/**
 * Development builds only (AP-02): the API's /health from the app origin, to prove the origin
 * and CORS wiring locally. A production build renders nothing and calls nothing.
 */
export function ApiHealth() {
  if (process.env.NODE_ENV !== "development") return null;
  return <ApiHealthProbe />;
}

function ApiHealthProbe() {
  const t = useTranslations("shell");
  const { isSuccess, isError } = useQuery({
    queryKey: ["dev", 0, "health", {}],
    queryFn: async ({ signal }) => {
      const response = await fetch(`${publicEnv().apiOrigin}/health`, { signal });
      if (!response.ok) throw new Error(String(response.status));
      return true;
    },
    retry: false,
  });
  if (!isSuccess && !isError) return null;
  return (
    <p className="m-0 text-xs text-on-brand" data-testid="api-health">
      {isSuccess ? t("apiStatusUp") : t("apiStatusDown")}
    </p>
  );
}
