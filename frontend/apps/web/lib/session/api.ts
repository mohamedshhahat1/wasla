/** The client pieces the session uses, in one place (AC-03). */
export { ApiError, createApiClient, normaliseError, type WaslaClient } from "@wasla/api-client";

import { ApiError, isAbortError } from "@wasla/api-client";

/** A session route that could not be reached at all. Aborts are re-thrown unchanged. */
export function networkErrorOf(cause: unknown): ApiError {
  if (isAbortError(cause)) throw cause;
  return new ApiError({ status: 0, code: "network_error" });
}
