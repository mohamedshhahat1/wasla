import { sessionRoute } from "../../../lib/server/session-handlers";

/** POST /session/register-login (S-2, FE-2): checks, then one signed API call; 405 for any other method. */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const route = sessionRoute("register-login");
export const POST = route.POST;
export const GET = route.GET;
export const PUT = route.PUT;
export const PATCH = route.PATCH;
export const DELETE = route.DELETE;
export const OPTIONS = route.OPTIONS;
