import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "astro/config";

import { readOrigins } from "@wasla/config/origins";
import { DEFAULT_LOCALE, LOCALES } from "@wasla/i18n";

// Fails the build on a missing or non-https origin (AP-03).
const { environment, origins } = readOrigins(process.env);

export default defineConfig({
  site: origins.PUBLIC_MARKETING_ORIGIN,
  output: "static",
  trailingSlash: "always",
  build: { format: "directory", inlineStylesheets: "never" },
  // No dev toolbar, no prefetch script, no telemetry: the built site ships no JavaScript.
  devToolbar: { enabled: false },
  prefetch: false,
  i18n: {
    locales: [...LOCALES],
    defaultLocale: DEFAULT_LOCALE,
    routing: { prefixDefaultLocale: true, redirectToDefaultLocale: true },
  },
  vite: {
    plugins: [tailwindcss()],
    define: {
      "import.meta.env.PUBLIC_WASLA_ENV": JSON.stringify(environment),
      "import.meta.env.PUBLIC_API_ORIGIN": JSON.stringify(origins.PUBLIC_API_ORIGIN),
      "import.meta.env.PUBLIC_APP_ORIGIN": JSON.stringify(origins.PUBLIC_APP_ORIGIN),
      "import.meta.env.PUBLIC_MARKETING_ORIGIN": JSON.stringify(origins.PUBLIC_MARKETING_ORIGIN),
    },
  },
});
