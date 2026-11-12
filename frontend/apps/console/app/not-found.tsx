import { getTranslations } from "next-intl/server";

import { Button, ErrorState } from "@wasla/ui";

import { Shell } from "../components/shell";

/** APP-404: translated, a link home, nothing from the backend. */
export default async function NotFound() {
  const t = await getTranslations("shell");
  const common = await getTranslations("common");
  return (
    <Shell>
      <ErrorState
        headingLevel={1}
        title={t("notFoundTitle")}
        description={t("notFoundBody")}
        action={
          <Button asChild>
            <a href="/">{common("goHome")}</a>
          </Button>
        }
      />
    </Shell>
  );
}
