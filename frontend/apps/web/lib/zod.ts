/**
 * zod for the forms (AU-01), configured once: jitless, because zod 4's fast path probes
 * `new Function`, which the nonce CSP refuses (script-src without 'unsafe-eval') and reports
 * as a violation on every page with a form.
 */
import { z } from "zod";

z.config({ jitless: true });

export { z };
