"use client";

import { useTranslations } from "next-intl";

import { ApiError } from "@wasla/api-client";
import { errorMessageKey } from "@wasla/i18n";

/** What a form shows for an error: a translated sentence by code, and the reference (AU-01, AU-04). */
export function useErrorText() {
  const t = useTranslations();
  return (error: unknown): { text: string; requestId: string | null; code: string } => {
    const apiError =
      error instanceof ApiError ? error : new ApiError({ status: 0, code: "unexpected_response" });
    if (apiError.code === "rate_limited" && apiError.retryAfterSeconds !== null) {
      return {
        text: `${t("errors.rate_limited")} ${t("common.retryIn", { seconds: Math.ceil(apiError.retryAfterSeconds) })}`,
        requestId: apiError.requestId,
        code: apiError.code,
      };
    }
    return {
      text: t(errorMessageKey(apiError.code)),
      requestId: apiError.requestId,
      code: apiError.code,
    };
  };
}

/** The form-level error: announced (role="alert"), the reference beside it, never a backend sentence. */
export function FormAlert({ error, override }: { error: unknown; override?: string }) {
  const t = useTranslations("common");
  const describe = useErrorText();
  if (!error) return null;
  const { text, requestId, code } = describe(error);
  return (
    <div
      role="alert"
      data-error-code={code}
      className="rounded-md border border-danger bg-danger-subtle p-4 text-sm text-text"
    >
      <p className="m-0">{override ?? text}</p>
      {requestId ? (
        <p className="m-0 mt-2 text-text-muted" data-testid="request-id">
          {t("requestIdLabel")} <bdi className="font-medium text-text">{requestId}</bdi>
        </p>
      ) : null}
    </div>
  );
}

/** A success or information note on an auth page (polite live region). */
export function FormNote({ children }: { children: React.ReactNode }) {
  return (
    <p
      role="status"
      className="m-0 rounded-md border border-border bg-surface-sunken p-4 text-sm text-text"
    >
      {children}
    </p>
  );
}
