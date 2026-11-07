import type { Decorator, Preview } from "@storybook/react-vite";
import { useEffect, type ReactNode } from "react";

import "../src/styles.css";
import "../src/fonts";
import { UiProvider, type Dir } from "../src/provider";

/** The four locales of IN-01 and their direction. ui does not import i18n (labels are props). */
const LOCALES: Record<string, Dir> = { en: "ltr", ar: "rtl", fr: "ltr", tr: "ltr" };

/** Sets <html lang dir> as the apps' server does before first paint (IN-04). */
function LocaleRoot({ locale, children }: { locale: string; children: ReactNode }) {
  const dir = LOCALES[locale] ?? "ltr";
  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = dir;
  }, [locale, dir]);
  return (
    <UiProvider dir={dir} locale={locale}>
      <main className="p-7">{children}</main>
    </UiProvider>
  );
}

const withUiProvider: Decorator = (Story, context) => (
  <LocaleRoot locale={String(context.globals["locale"] ?? "en")}>
    <Story />
  </LocaleRoot>
);

const preview: Preview = {
  decorators: [withUiProvider],
  globalTypes: {
    locale: {
      description: "Locale and direction",
      toolbar: {
        title: "Locale",
        icon: "globe",
        items: [
          { value: "en", title: "English (LTR)" },
          { value: "ar", title: "العربية (RTL)" },
          { value: "fr", title: "Français (LTR)" },
          { value: "tr", title: "Türkçe (LTR)" },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: { locale: "en" },
  parameters: {
    layout: "fullscreen",
    viewport: {
      options: {
        phone: { name: "Phone 390", styles: { width: "390px", height: "844px" } },
        desktop: { name: "Desktop 1280", styles: { width: "1280px", height: "800px" } },
      },
    },
  },
};

export default preview;
