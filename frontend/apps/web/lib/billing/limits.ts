/**
 * An entitlement's amount in words a person reads (FEF-47): the screen's locale groups the digits
 * (Latin digits, the app's rule), and a byte limit is stated in gigabytes - never "53687091200".
 * Only formatting: the number is the API's, never added to, compared or rounded for a decision.
 */
export const BYTES_PER_GIGABYTE = 1024 ** 3;

export function isByteLimit(key: string): boolean {
  return key.endsWith("_bytes");
}

export function limitAmount(key: string, value: number, locale: string): string {
  if (isByteLimit(key)) {
    return new Intl.NumberFormat(locale, {
      style: "unit",
      unit: "gigabyte",
      maximumFractionDigits: 1,
      numberingSystem: "latn",
    }).format(value / BYTES_PER_GIGABYTE);
  }
  return new Intl.NumberFormat(locale, { numberingSystem: "latn" }).format(value);
}
