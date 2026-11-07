/**
 * The workspace's one ESLint flat configuration. Every app and package calls
 * `waslaConfig({ kind, tsconfigRootDir })`; the kind decides which import directions
 * (plan section 17) and which of the workspace's own rules apply.
 */
import js from "@eslint/js";
import importX from "eslint-plugin-import-x";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

import wasla from "./plugin.js";

const APPS = ["web", "console", "marketing"];

/** The public origins: the only PUBLIC_ names allowed whatever they contain. */
export const PUBLIC_ORIGIN_NAMES = [
  "PUBLIC_API_ORIGIN",
  "PUBLIC_APP_ORIGIN",
  "PUBLIC_CONSOLE_ORIGIN",
  "PUBLIC_MARKETING_ORIGIN",
];

/** The one file allowed to touch web storage (it holds no token: S-2). */
export const STORAGE_ALLOW_FILES = ["packages/session/src/preferences-storage.ts"];

/** Files that always end up in a browser bundle, whatever their directive. */
export const CLIENT_FILE_FRAGMENTS = [
  "/packages/ui/",
  "/packages/i18n/",
  "/packages/api-client/src/",
  "/packages/session/src/browser/",
  "/apps/marketing/",
];

const APP_PACKAGE = (name) => [`@wasla/${name}`, `@wasla/${name}/*`];

/** Import restrictions per kind (plan section 17; enforced, not a convention). */
export function importRestrictions(kind) {
  const otherApps = (self) => APPS.filter((app) => app !== self);
  const appImports = (apps) =>
    apps.flatMap((app) => [
      { group: APP_PACKAGE(app), message: `No app or package imports the ${app} app.` },
      {
        regex: `(^|/)(apps/)?${app}/(app|src|components)(/|$)`,
        message: `No app or package imports the ${app} app's files.`,
      },
    ]);
  const packages = (...names) =>
    names.map((name) => ({
      group: [`@wasla/${name}`, `@wasla/${name}/*`],
      message: `This package may not import @wasla/${name} (plan section 17).`,
    }));
  const anyApp = [
    ...appImports(APPS),
    { regex: "(^|/)apps/", message: "Packages never import apps." },
  ];
  switch (kind) {
    case "app-web":
      return appImports(otherApps("web"));
    case "app-console":
      return appImports(otherApps("console"));
    case "app-marketing":
      return [...appImports(otherApps("marketing")), ...packages("session", "api-client")];
    case "pkg-ui":
      return [...anyApp, ...packages("session", "api-client")];
    case "pkg-i18n":
      return [...anyApp, ...packages("ui", "session", "api-client")];
    case "pkg-api-client":
      return [...anyApp, ...packages("ui", "i18n", "session")];
    case "pkg-session":
      return [...anyApp, ...packages("ui", "i18n")];
    case "pkg-config":
      return [...anyApp, ...packages("ui", "i18n", "session", "api-client")];
    case "e2e":
      return [];
    default:
      throw new Error(`unknown lint kind: ${kind}`);
  }
}

/** The workspace's own rules for one kind of package (WSP-04). */
export function waslaRules(kind) {
  const isApp = kind.startsWith("app-");
  const rules = {
    "wasla/no-public-secret-env": ["error", { allow: PUBLIC_ORIGIN_NAMES }],
    // The e2e harness reads cookies and storage to prove the apps leave them empty (AP-04).
    "wasla/no-web-storage": kind === "e2e" ? "off" : ["error", { allowFiles: STORAGE_ALLOW_FILES }],
    "wasla/no-server-only-in-client": ["error", { clientFiles: CLIENT_FILE_FRAGMENTS }],
    "no-restricted-imports": ["error", { patterns: importRestrictions(kind) }],
  };
  if (isApp || kind === "pkg-ui") {
    rules["wasla/no-physical-direction"] = "error";
    rules["wasla/no-uppercase"] = "error";
  }
  if (kind === "pkg-i18n") rules["wasla/no-uppercase"] = "error";
  if (isApp) rules["wasla/no-raw-design-values"] = "error";
  if (kind === "app-console") rules["wasla/console-platform-paths"] = "error";
  return rules;
}

/**
 * @param {{ kind: string, tsconfigRootDir?: string, typeChecked?: boolean, ignores?: string[] }} options
 */
export function waslaConfig({ kind, tsconfigRootDir, typeChecked = true, ignores = [] }) {
  const typed = typeChecked && tsconfigRootDir;
  return tseslint.config(
    {
      ignores: [
        "**/node_modules/**",
        "**/.next/**",
        "**/.open-next/**",
        "**/.astro/**",
        "**/.wrangler/**",
        "**/.turbo/**",
        "**/dist/**",
        "**/storybook-static/**",
        "**/playwright-report/**",
        "**/test-results/**",
        "**/coverage/**",
        "**/test-fixtures/**",
        "**/*.d.ts",
        "**/next-env.d.ts",
        ...ignores,
      ],
    },
    js.configs.recommended,
    ...(typed ? tseslint.configs.recommendedTypeChecked : tseslint.configs.recommended),
    {
      languageOptions: {
        ecmaVersion: 2024,
        sourceType: "module",
        globals: { ...globals.browser, ...globals.node },
        ...(typed
          ? { parserOptions: { projectService: true, tsconfigRootDir } }
          : { parserOptions: { ecmaFeatures: { jsx: true } } }),
      },
      settings: { react: { version: "19.3" } },
      plugins: {
        react,
        "react-hooks": reactHooks,
        "jsx-a11y": jsxA11y,
        "import-x": importX,
        wasla,
      },
      rules: {
        ...react.configs.flat.recommended.rules,
        ...react.configs.flat["jsx-runtime"].rules,
        ...jsxA11y.flatConfigs.recommended.rules,
        // Playwright fixtures call `use()`, which is not React's hook.
        "react-hooks/rules-of-hooks": kind === "e2e" ? "off" : "error",
        "react-hooks/exhaustive-deps": "error",
        "react/prop-types": "off",
        "react/no-danger": "error",
        "react/no-danger-with-children": "error",
        "import-x/order": [
          "error",
          {
            groups: ["builtin", "external", "internal", ["parent", "sibling", "index"]],
            pathGroups: [{ pattern: "@wasla/**", group: "internal" }],
            pathGroupsExcludedImportTypes: ["builtin"],
            "newlines-between": "always",
            alphabetize: { order: "asc", caseInsensitive: true },
          },
        ],
        "import-x/no-duplicates": "error",
        "@typescript-eslint/consistent-type-imports": "error",
        "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
        ...waslaRules(kind),
      },
    },
    {
      files: ["**/*.js", "**/*.mjs", "**/*.cjs"],
      ...(typed ? tseslint.configs.disableTypeChecked : {}),
    },
  );
}
