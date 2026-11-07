import { getTranslations } from "next-intl/server";

import { EmptyState } from "@wasla/ui";

import { Shell } from "../components/shell";

/** The console's signed-out shell (FE-1): no API call, no cookie but the locale. */
export default async function Home() {
  const t = await getTranslations("shell");
  return (
    <Shell>
      <EmptyState headingLevel={1} title={t("signedOutTitle")} description={t("signedOutBody")} />
      <p className="text-center text-md text-text-muted">{t("consoleTagline")}</p>
    </Shell>
  );
}
