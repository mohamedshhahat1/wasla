"use client";

import { useTranslations } from "next-intl";

import { ApiError } from "@wasla/api-client";
import { errorMessageKey } from "@wasla/i18n";
import { Button, ErrorState, Link } from "@wasla/ui";

/**
 * APP-ERR: a translated sentence chosen by the error's code, the request id when an API error
 * carries one - never a stack, never the error's message, never a backend sentence (AP-02).
 */
export default function ErrorPage({ error, reset }: { error: Error; reset: () => void }) {
  const t = useTranslations();
  const apiError = error instanceof ApiError ? error : null;
  const description = apiError ? t(errorMessageKey(apiError.code)) : t("shell.errorBody");
  return (
    <main id="main" className="mx-auto w-full max-w-page px-5 py-8">
      <ErrorState
        headingLevel={1}
        title={t("shell.errorTitle")}
        description={description}
        requestId={apiError?.requestId ?? null}
        requestIdLabel={t("common.requestIdLabel")}
        action={
          <>
            <Button variant="secondary" onClick={reset}>
              {t("common.tryAgain")}
            </Button>
            <Link href="/">{t("common.goHome")}</Link>
          </>
        }
      />
    </main>
  );
}
