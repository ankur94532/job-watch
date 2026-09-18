/* Verify the Cloudflare Access assertion instead of trusting only the outer
   hostname rule. A mistaken Access configuration must fail closed for both
   the synced user state and the private job snapshot. */

const DEFAULT_TEAM = "https://white-river-f80d.cloudflareaccess.com";
let certs = null, certsAt = 0;

export async function verifiedEmail(request, team = DEFAULT_TEAM) {
  const token = request.headers.get("Cf-Access-Jwt-Assertion") || cookie(request, "CF_Authorization");
  if (!token) throw new Error("no token");
  const [h, p, s] = token.split(".");
  const header = JSON.parse(b64text(h));
  const payload = JSON.parse(b64text(p));
  if (header.alg !== "RS256") throw new Error("alg");
  if (payload.iss !== team) throw new Error("issuer");
  if (!(payload.exp * 1000 > Date.now())) throw new Error("expired");
  if (!payload.email) throw new Error("no email");

  const jwk = (await teamKeys(team)).find((k) => k.kid === header.kid);
  if (!jwk) throw new Error("unknown key");
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
                                            false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64bytes(s),
                                        new TextEncoder().encode(h + "." + p));
  if (!ok) throw new Error("signature");
  return payload.email;
}

async function teamKeys(team) {
  if (!certs || Date.now() - certsAt > 3600 * 1000) {
    const r = await fetch(team + "/cdn-cgi/access/certs");
    if (!r.ok) throw new Error("certs " + r.status);
    certs = (await r.json()).keys || [];
    certsAt = Date.now();
  }
  return certs;
}

export function resetKeyCache() { certs = null; certsAt = 0; }

function cookie(request, name) {
  const m = (request.headers.get("Cookie") || "").match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return m ? m[1] : null;
}
function b64bytes(s) {
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}
function b64text(s) { return new TextDecoder().decode(b64bytes(s)); }
