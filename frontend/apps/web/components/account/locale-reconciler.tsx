"use client";

import { usePathname } from "next/navigation";
import { useLocale } from "next-intl";
import { useEffect, useRef } from "react";

import { isLocale } from "@wasla/i18n";

import { postDeviceLocale, reconcileLocale, reconcileMemo } from "../../lib/account/locale";
import { useSession } from "../session-provider";

const memo = reconcileMemo();

/**
 * LC-06 (FEB-09, ADR-201), once per page and person: the device follows the
 * account's language when they differ; an account with none takes the language in use (OQ-2).
 * Renders nothing.
 */
export function LocaleReconciler() {
  const { session, profile } = useSession();
  const requested = useLocale();
  const pathname = usePathname();
  const ran = useRef(false);
  useEffect(() => {
    if (!profile || ran.current || !isLocale(requested)) return;
    ran.current = true;
    if (!memo.take(profile.id)) return;
    const step = reconcileLocale(profile.locale, requested);
    if (step.kind === "follow-account") {
      postDeviceLocale(step.locale, pathname);
    } else if (step.kind === "write-account") {
      void session.api.PATCH("/api/v1/auth/me", { body: { locale: step.locale } });
    }
  }, [profile, requested, pathname, session]);
  return null;
}
