"use client";

import { ApiError } from "@wasla/api-client";

/** Throws, while rendering, the kind of error a screen gets from the API. */
export function Thrower(): never {
  throw new ApiError({
    status: 503,
    code: "dependency_unavailable",
    requestId: "e2e-request-id-0001",
    diagnosticMessage: "An English backend sentence that must never be shown.",
  });
}
