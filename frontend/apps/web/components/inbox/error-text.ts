"use client";

import { useTranslations } from "next-intl";

import { errorMessageKey } from "@wasla/i18n";

/** A code's translated sentence, the fallback when the catalogue has none (never a backend sentence). */
export function useCodeText(): (code: string | null | undefined) => string {
  const t = useTranslations();
  return (code) => t(errorMessageKey(code ?? "fallback"));
}
