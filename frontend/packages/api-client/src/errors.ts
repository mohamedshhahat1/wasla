/**
 * One error shape for the whole frontend (AC-03). Every non-2xx answer, network failure and
 * unreadable body becomes an `ApiError` keyed by the envelope's `code`; the backend's English
 * sentence is kept for diagnostics only and is never a UI string - the UI resolves
 * `messageKey` ("errors.<code>") through packages/i18n.
 */
import type { components } from "./schema";

export type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
export type ValidationErrorEnvelope = components["schemas"]["ValidationErrorEnvelope"];

/** Codes the client itself produces, beside the backend's (plan Appendix C). */
export const CLIENT_ERROR_CODES = ["unexpected_response", "network_error"] as const;

export interface ApiErrorInit {
  status: number;
  code: string;
  details?: Record<string, unknown> | null;
  requestId?: string | null;
  fieldErrors?: Record<string, string>;
  diagnosticMessage?: string | null;
  retryAfterSeconds?: number | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** The catalogue key the UI shows: "errors.<code>"; i18n falls back to "errors.fallback". */
  readonly messageKey: string;
  readonly details: Record<string, unknown> | null;
  readonly requestId: string | null;
  /** 422 only: dotted form-field path → the validation error's type (e.g. "missing"). */
  readonly fieldErrors: Record<string, string> | undefined;
  /** The backend's English sentence. For error reports only; never rendered. */
  readonly diagnosticMessage: string | null;
  readonly retryAfterSeconds: number | null;

  constructor(init: ApiErrorInit) {
    // The Error message is the code, so an accidental render shows no backend sentence.
    super(init.code);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.messageKey = `errors.${init.code}`;
    this.details = init.details ?? null;
    this.requestId = init.requestId ?? null;
    this.fieldErrors = init.fieldErrors;
    this.diagnosticMessage = init.diagnosticMessage ?? null;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
  }
}

const CODE = /^[a-z][a-z0-9_]{0,63}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function fieldErrorsOf(
  details: Record<string, unknown> | null,
): Record<string, string> | undefined {
  const errors = details?.["errors"];
  if (!Array.isArray(errors)) return undefined;
  const fields: Record<string, string> = {};
  for (const item of errors) {
    if (!isRecord(item) || !Array.isArray(item["loc"])) continue;
    const loc = item["loc"] as unknown[];
    const path = loc
      .slice(1)
      .map((part) => String(part))
      .join(".");
    const type = typeof item["type"] === "string" ? item["type"] : "invalid";
    if (path && !(path in fields)) fields[path] = type;
  }
  return Object.keys(fields).length ? fields : undefined;
}

function retryAfterOf(response: Response | undefined): number | null {
  const header = response?.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number(header);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/**
 * Reads an error body into an ApiError. A body that is not the envelope (an HTML page from a
 * proxy, an empty body, a different JSON shape) becomes `unexpected_response`.
 */
export function normaliseError(body: unknown, response: Response | undefined): ApiError {
  const status = response?.status ?? 0;
  const headerRequestId = response?.headers.get("X-Request-ID") ?? null;
  const retryAfterSeconds = retryAfterOf(response);
  const envelope = isRecord(body) ? body["error"] : undefined;
  if (!isRecord(envelope) || typeof envelope["code"] !== "string" || !CODE.test(envelope["code"])) {
    return new ApiError({
      status,
      code: "unexpected_response",
      requestId: headerRequestId,
      retryAfterSeconds,
    });
  }
  const details = isRecord(envelope["details"]) ? envelope["details"] : null;
  const requestId =
    typeof envelope["request_id"] === "string" ? envelope["request_id"] : headerRequestId;
  const init: ApiErrorInit = {
    status,
    code: envelope["code"],
    details,
    requestId,
    diagnosticMessage: typeof envelope["message"] === "string" ? envelope["message"] : null,
    retryAfterSeconds,
  };
  const fieldErrors = status === 422 ? fieldErrorsOf(details) : undefined;
  if (fieldErrors) init.fieldErrors = fieldErrors;
  return new ApiError(init);
}

export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (isRecord(error) && error["name"] === "AbortError")
  );
}

/** A failure before any response: the network, CORS, DNS. Aborts are re-thrown as they are. */
export function networkError(cause: unknown): ApiError {
  if (isAbortError(cause)) throw cause;
  const error = new ApiError({ status: 0, code: "network_error" });
  Object.defineProperty(error, "cause", { value: cause });
  return error;
}
