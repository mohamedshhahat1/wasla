export declare const ORIGIN_NAMES: readonly [
  "PUBLIC_API_ORIGIN",
  "PUBLIC_APP_ORIGIN",
  "PUBLIC_CONSOLE_ORIGIN",
  "PUBLIC_MARKETING_ORIGIN",
];
export type OriginName = (typeof ORIGIN_NAMES)[number];
export declare const ENVIRONMENTS: readonly ["development", "preview", "production"];
export type WaslaEnvironment = (typeof ENVIRONMENTS)[number];
export declare const ORIGIN_TABLE: Record<WaslaEnvironment, Partial<Record<OriginName, string>>>;
export declare function readOrigins(env: Record<string, string | undefined>): {
  environment: WaslaEnvironment;
  origins: Record<OriginName, string>;
};
