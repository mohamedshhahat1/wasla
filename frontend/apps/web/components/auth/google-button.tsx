"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@wasla/ui";

import { googleEnabled } from "../../lib/env";
import { useSession } from "../session-provider";
import { FormAlert } from "./form-alert";

/**
 * APP-AUTH-03: shown only when the build says Google is configured (PUBLIC_GOOGLE_ENABLED, the
 * FEB-16 workaround). The binding cookie is relayed by /session/google/authorize (SR-07).
 */
export function GoogleButton() {
  const t = useTranslations("auth");
  const { session } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  if (!googleEnabled()) return null;
  return (
    <div className="flex flex-col gap-3">
      <Button
        variant="secondary"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          setError(null);
          session.googleAuthorizeUrl().then(
            (url) => window.location.assign(url),
            (caught: unknown) => {
              setError(caught);
              setBusy(false);
            },
          );
        }}
      >
        {t("google.continue")}
      </Button>
      <FormAlert error={error} />
    </div>
  );
}
