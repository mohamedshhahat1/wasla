/** The backend profile must already be up and healthy; the harness never starts Docker (TE-02). */
import { API_ORIGIN, MAILBOX } from "./env";

export default async function globalSetup(): Promise<void> {
  for (const [name, url] of [
    ["api", `${API_ORIGIN}/health`],
    ["mailbox", `${MAILBOX}/health`],
  ] as const) {
    const response = await fetch(url).catch((error: unknown) => {
      throw new Error(
        `the e2e ${name} is not reachable at ${url} - start FE-0's profile with \`pnpm e2e:local\` (${String(error)})`,
      );
    });
    if (!response.ok) throw new Error(`the e2e ${name} answered ${response.status} at ${url}`);
  }
}
