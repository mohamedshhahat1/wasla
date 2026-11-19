"use client";

import { usePathname } from "next/navigation";
import { useLocale } from "next-intl";
import { useEffect, useRef } from "react";

import { isLocale } from "@wasla/i18n";

import {
  alreadyReconciled,
  markReconciled,
  postDeviceLocale,
  reconcileLocale,
} from "../../lib/account/locale";
import { tabFlags } from "../../lib/account/tab-flags";
import { useSession } from "../session-provider";

/**
 * LC-06 (FEB-09, ADR-201), once per signed-in session in this tab: the device follows the
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
    const store = tabFlags();
    if (alreadyReconciled(store)) return;
    markReconciled(store);
    const step = reconcileLocale(profile.locale, requested);
    if (step.kind === "follow-account") {
      postDeviceLocale(step.locale, pathname);
    } else if (step.kind === "write-account") {
      void session.api.PATCH("/api/v1/auth/me", { body: { locale: step.locale } });
    }
  }, [profile, requested, pathname, session]);
  return null;
}
