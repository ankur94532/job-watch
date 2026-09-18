/* Cross-device state for the phone view: ticks, "opened" marks, which poll each
   company was last opened at, and the seen-through baseline. The one write path
   on an otherwise static site.

     GET /api/state   -> the stored document
     PUT /api/state   -> merge the body into it; returns the merged document

   Every device PUTs its whole local document and adopts what comes back, so
   the merge is the protocol. It is order-free: every field is either a max
   (timestamps) or last-writer-wins per key (ticks, which can be undone), so
   two devices syncing in any order converge on the same state.

   Cloudflare Access gates the whole hostname, so nothing reaches this without
   the owner's login. It still verifies the Access token itself rather than
   trusting that: a misconfigured Access application should fail closed here,
   not quietly open a write endpoint. The document is keyed by the verified
   email.

   Needs a KV namespace bound as STATE (Pages project -> Settings -> Bindings).

   Writes only when the merge changed something. The free tier allows 1,000
   KV writes a day against 100,000 reads, and every page syncs on load, on
   navigation and once a minute: writing unconditionally used half the daily
   allowance by the first evening. A sync with nothing new is now one read. */

import { verifiedEmail, resetKeyCache } from "../_lib/access.js";
export { verifiedEmail, resetKeyCache };

const TEAM = "https://white-river-f80d.cloudflareaccess.com";
const MAX_BODY = 512 * 1024;
const TOMBSTONE_TTL = 60 * 24 * 3600 * 1000;   // an untick is forgotten after 60 days
const OPENED_CAP = 200;
const TICKS_CAP = 5000;

export async function onRequest({ request, env }) {
  if (!env.STATE) return json({ error: "KV namespace not bound as STATE" }, 503);
  let email;
  try {
    email = await verifiedEmail(request, env.ACCESS_TEAM || TEAM);
  } catch (e) {
    return json({ error: "unauthenticated" }, 401);
  }
  const key = "state:" + email.toLowerCase();

  if (request.method === "GET") {
    return json(normalise(await env.STATE.get(key, "json")));
  }
  if (request.method !== "PUT") return json({ error: "method not allowed" }, 405);

  const text = await request.text();
  if (text.length > MAX_BODY) return json({ error: "too large" }, 413);
  let incoming;
  try { incoming = JSON.parse(text); } catch (e) { return json({ error: "bad json" }, 400); }

  const stored = normalise(await env.STATE.get(key, "json"));
  const merged = merge(stored, normalise(incoming), Date.now());
  if (canonical(merged) !== canonical(stored)) {
    await env.STATE.put(key, JSON.stringify(merged));
  }
  return json(merged);
}

/* ---- the document ------------------------------------------------------- */

export function normalise(doc) {
  const d = (doc && typeof doc === "object") ? doc : {};
  return {
    v: 1,
    ticks: pick(d.ticks, (x) => Array.isArray(x) && (x[0] === 0 || x[0] === 1) && num(x[1])),
    opened: pick(d.opened, num),
    visited: pick(d.visited, num),
    seenThrough: num(d.seenThrough) ? d.seenThrough : 0,
  };
}

export function merge(a, b, now) {
  const ticks = {};
  for (const k of keys(a.ticks, b.ticks)) {
    const x = a.ticks[k], y = b.ticks[k];
    ticks[k] = !x ? y : !y ? x : (y[1] > x[1] ? y : x);        // newest action wins
  }
  for (const k of Object.keys(ticks)) {                        // forget old unticks
    if (ticks[k][0] === 0 && now - ticks[k][1] > TOMBSTONE_TTL) delete ticks[k];
  }
  return {
    v: 1,
    ticks: cap(ticks, TICKS_CAP, (t) => t[1]),
    opened: cap(maxMerge(a.opened, b.opened), OPENED_CAP, (t) => t),
    visited: maxMerge(a.visited, b.visited),
    seenThrough: Math.max(a.seenThrough, b.seenThrough),
  };
}

function maxMerge(a, b) {
  const out = {};
  for (const k of keys(a, b)) out[k] = Math.max(a[k] || 0, b[k] || 0);
  return out;
}

function cap(obj, n, when) {
  const ks = Object.keys(obj);
  if (ks.length <= n) return obj;
  ks.sort((p, q) => when(obj[q]) - when(obj[p]));
  const out = {};
  for (const k of ks.slice(0, n)) out[k] = obj[k];
  return out;
}

/* Key order must not count as a change: the same state merged from two
   devices can come out with its keys in a different order. */
export function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
  }
  return JSON.stringify(value);
}

function pick(obj, ok) {
  const out = {};
  if (obj && typeof obj === "object") for (const k of Object.keys(obj)) if (ok(obj[k])) out[k] = obj[k];
  return out;
}
function keys(a, b) { return new Set([...Object.keys(a), ...Object.keys(b)]); }
function num(x) { return typeof x === "number" && isFinite(x) && x >= 0; }

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
