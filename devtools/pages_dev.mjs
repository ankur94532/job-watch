// Local stand-in for Cloudflare Pages + Access, for driving the synced page in a
// browser. DEV-ONLY, like site_check.py.
//
//   node devtools/pages_dev.mjs [port]
//
// Serves docs/ as static files and routes /api/* to the REAL Pages Function
// (watcher/site/functions/api/state.js) with an in-memory KV. It plays Access
// too: every request gets a Cf-Access-Jwt-Assertion signed by a key generated
// at startup, exactly the header the function verifies in production.
// `?signedout` on the API path answers with Access's redirect instead.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { onRequest } from "../watcher/site/functions/api/state.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(here, "..", "docs");
const PORT = Number(process.argv[2] || 8792);
const TEAM = "https://team.localhost.cloudflareaccess.com";
const EMAIL = process.env.PAGES_DEV_EMAIL || "me@example.com";
let signedOut = false;

const { publicKey, privateKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "dev" };
const realFetch = globalThis.fetch;
globalThis.fetch = (url, ...rest) =>
  String(url) === TEAM + "/cdn-cgi/access/certs"
    ? Promise.resolve(new Response(JSON.stringify({ keys: [jwk] })))
    : realFetch(url, ...rest);

const b64 = (b) => Buffer.from(b).toString("base64url");
async function accessToken() {
  const h = b64(JSON.stringify({ alg: "RS256", kid: "dev" }));
  const p = b64(JSON.stringify({ iss: TEAM, email: EMAIL, exp: Math.floor(Date.now() / 1000) + 3600 }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, new TextEncoder().encode(h + "." + p));
  return h + "." + p + "." + b64(new Uint8Array(sig));
}

const store = new Map();
const env = {
  ACCESS_TEAM: TEAM,
  STATE: {
    get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null),
    put: async (k, v) => { store.set(k, v); },
  },
};
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };

http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/__signout") { signedOut = true; res.end("ok"); return; }
  if (url.pathname === "/__signin") { signedOut = false; res.end("ok"); return; }
  if (url.pathname === "/__state") { res.end(JSON.stringify(Object.fromEntries(store))); return; }
  if (url.pathname.startsWith("/api/")) {
    if (signedOut) {                                     // what Access does to an expired session
      res.writeHead(302, { Location: TEAM + "/cdn-cgi/access/login/x" }); res.end(); return;
    }
    const body = await new Promise((ok) => { let b = ""; req.on("data", (c) => (b += c)); req.on("end", () => ok(b)); });
    const headers = new Headers({ "Cf-Access-Jwt-Assertion": await accessToken() });
    const r = await onRequest({ env, request: new Request(url, { method: req.method, headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : body }) });
    res.writeHead(r.status, Object.fromEntries(r.headers));
    res.end(await r.text());
    return;
  }
  const file = path.join(ROOT, path.normalize(url.pathname === "/" ? "/index.html" : url.pathname));
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); res.end(); return;
  }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}).listen(PORT, "127.0.0.1", () => console.log(`pages_dev: http://127.0.0.1:${PORT}/ (Access as ${EMAIL})`));
