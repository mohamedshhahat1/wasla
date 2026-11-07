/** Only a path of this app: "/x" - never "//host", "/\host", a URL or a header break. */
export function safeNext(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/")) return "/";
  if (value.startsWith("//") || value.startsWith("/\\") || /[\r\n]/.test(value)) return "/";
  return value;
}
