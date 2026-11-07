/** Where everything runs locally and on the CI runner (TE-02). */
export const API_PORT = Number(process.env["WASLA_E2E_API_PORT"] ?? 8000);
export const MAILBOX_PORT = Number(process.env["WASLA_E2E_MAILBOX_PORT"] ?? 8025);

/** The API origin the apps were built with (development table, packages/config/origins.js). */
export const API_ORIGIN = `http://localhost:${API_PORT}`;
export const MAILBOX = `http://127.0.0.1:${MAILBOX_PORT}`;

export const APPS = {
  web: { origin: "http://localhost:3000", noFlagOrigin: "http://localhost:3002" },
  console: { origin: "http://localhost:3001", noFlagOrigin: "http://localhost:3003" },
  marketing: { origin: "http://localhost:4321" },
} as const;

/** The e2e stack's PUBLIC test secret (docker-compose.e2e.yml), never a deployment's. */
export const E2E_FORWARD_SECRET = "e2e-not-a-secret-0123456789abcdef0123456789abcdef";
