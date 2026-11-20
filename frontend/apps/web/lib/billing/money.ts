/**
 * Money as the API states it (UI8-04): the amount is the API's decimal string and the currency
 * its ISO code. The string is parsed only to be formatted - never added, compared or rounded here;
 * every total on a billing screen is one the API computed.
 */
export function formatMoney(
  amount: string | null | undefined,
  currency: string,
  locale: string,
): string {
  if (amount === null || amount === undefined || amount === "") return "";
  const value = Number(amount);
  if (!Number.isFinite(value)) return `${amount} ${currency}`;
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${amount} ${currency}`;
  }
}

/** Whether an amount the API gave is zero ("0", "0.00") - a free plan, or nothing owed. */
export function isZero(amount: string | null | undefined): boolean {
  if (amount === null || amount === undefined || amount === "") return true;
  return Number(amount) === 0;
}
