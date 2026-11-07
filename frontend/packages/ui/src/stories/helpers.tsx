import type { StoryContext } from "@storybook/react-vite";

import type { Dir } from "../provider";
import { LABELS } from "./samples";

export function localeOf(context: Pick<StoryContext, "globals">): string {
  return String(context.globals["locale"] ?? "en");
}

export function dirOf(context: Pick<StoryContext, "globals">): Dir {
  return localeOf(context) === "ar" ? "rtl" : "ltr";
}

export function labelsOf(context: Pick<StoryContext, "globals">) {
  return localeOf(context) === "ar" ? LABELS.ar : LABELS.en;
}

/** The three texts every primitive is shown with (DS-05). */
export const TEXT_CASES = ["LongArabic", "LongTurkish", "MixedDirection"] as const;
