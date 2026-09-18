/* The changing job snapshot lives in KV, not in a new Pages deployment.

     GET /api/jobs                  -> the small company index
     GET /api/jobs?file=acme.json   -> one company's current matching jobs

   Cloudflare Access gates the hostname. We also verify its assertion here so
   a future dashboard mistake cannot silently expose the snapshot. */

import { verifiedEmail } from "../_lib/access.js";

const SNAPSHOT_KEY = "site:snapshot";

export async function onRequest({ request, env }) {
  if (!env.STATE) return json({ error: "KV namespace not bound as STATE" }, 503);
  try {
    await verifiedEmail(request, env.ACCESS_TEAM);
  } catch (e) {
    return json({ error: "unauthenticated" }, 401);
  }
  if (request.method !== "GET") return json({ error: "method not allowed" }, 405);

  const snapshot = await env.STATE.get(SNAPSHOT_KEY, "json");
  if (!snapshot || snapshot.v !== 1 || !snapshot.index || !snapshot.companies) {
    return json({ error: "job snapshot not available" }, 503);
  }
  const file = new URL(request.url).searchParams.get("file");
  if (!file) return json(snapshot.index);
  if (!/^[a-z0-9_-]+\.json$/.test(file) || !snapshot.companies[file]) {
    return json({ error: "company not found" }, 404);
  }
  return json(snapshot.companies[file]);
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
