import { defineCloudflareConfig } from "@opennextjs/cloudflare";

/** OpenNext on Cloudflare Workers (ADR-189): no incremental cache, no queue - nothing is cached. */
export default defineCloudflareConfig();
