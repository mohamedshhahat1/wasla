/**
 * Only a path of this app (AU-03): "/x" - never "//host", "/\host", a URL, a scheme, or a path
 * with a control character or white space a browser would strip ("/\t/host" becomes "//host").
 */
export function safeNext(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/")) return "/";
  if (value.startsWith("//") || value.includes("\\")) return "/";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000- \u007f-\u009f]/.test(value)) return "/";
  let parsed: URL;
  try {
    parsed = new URL(value, "http://app.invalid");
  } catch {
    return "/";
  }
  if (parsed.origin !== "http://app.invalid") return "/";
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}
