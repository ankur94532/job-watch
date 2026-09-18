// The sync endpoint, exercised in Node against a real RSA key and a fake KV:
//   node --test tests/site_state.test.mjs
import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";
import { onRequest, merge, normalise, resetKeyCache } from "../watcher/site/functions/api/state.js";

const TEAM = "https://team.example.cloudflareaccess.com";
const { publicKey, privateKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "k1" };
globalThis.fetch = async (url) => {
  assert.equal(url, TEAM + "/cdn-cgi/access/certs");
  return new Response(JSON.stringify({ keys: [jwk] }));
};

const b64 = (bytes) => Buffer.from(bytes).toString("base64url");
async function token(claims = {}, key = privateKey, kid = "k1") {
  const h = b64(JSON.stringify({ alg: "RS256", kid }));
  const p = b64(JSON.stringify({ iss: TEAM, email: "Me@Example.com", exp: Date.now() / 1000 + 600, ...claims }));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(h + "." + p));
  return h + "." + p + "." + b64(new Uint8Array(sig));
}

function kv() {
  const m = new Map();
  const store = { m, puts: 0, get: async (k) => (m.has(k) ? JSON.parse(m.get(k)) : null),
                  put: async (k, v) => { store.puts++; m.set(k, v); } };
  return store;
}
const env = () => ({ STATE: kv(), ACCESS_TEAM: TEAM });
async function call(e, method, body, tok) {
  const headers = tok ? { "Cf-Access-Jwt-Assertion": tok } : {};
  const r = await onRequest({ env: e, request: new Request("https://x/api/state",
    { method, headers, body: body && JSON.stringify(body) }) });
  return { status: r.status, body: await r.json() };
}

beforeEach(resetKeyCache);

test("a laptop tick shows up on the phone", async () => {
  const e = env(), t = await token();
  await call(e, "PUT", { ticks: { "acme|1": [1, 1000] }, visited: { acme: 50 } }, t);
  const phone = await call(e, "PUT", { ticks: {}, seenThrough: 10 }, t);   // an empty new device
  assert.equal(phone.status, 200);
  assert.deepEqual(phone.body.ticks, { "acme|1": [1, 1000] });
  assert.deepEqual(phone.body.visited, { acme: 50 });
  assert.equal(phone.body.seenThrough, 10);
});

test("an untick is not resurrected by the other device's older tick", async () => {
  const merged = merge(normalise({ ticks: { k: [1, 100] } }), normalise({ ticks: { k: [0, 200] } }), 300);
  assert.deepEqual(merged.ticks.k, [0, 200]);
  const again = merge(merged, normalise({ ticks: { k: [1, 100] } }), 300);   // stale device syncs later
  assert.deepEqual(again.ticks.k, [0, 200]);
});

test("merging is order-free", async () => {
  const a = normalise({ ticks: { x: [1, 5], y: [0, 9] }, visited: { c: 3 }, seenThrough: 7 });
  const b = normalise({ ticks: { x: [0, 6], z: [1, 1] }, visited: { c: 4, d: 1 }, seenThrough: 2 });
  assert.deepEqual(merge(a, b, 10), merge(b, a, 10));
});

test("old unticks are forgotten, ticks never are", () => {
  const day = 24 * 3600 * 1000, now = 100 * day;
  const m = merge(normalise({ ticks: { gone: [0, 0], kept: [1, 0] } }), normalise({}), now);
  assert.deepEqual(Object.keys(m.ticks), ["kept"]);
});

test("junk in the body is dropped, not stored", () => {
  const n = normalise({ ticks: { a: [2, 1], b: "x", c: [1, -1], d: [1, 3] }, visited: { e: "1" }, seenThrough: "9" });
  assert.deepEqual(n.ticks, { d: [1, 3] });
  assert.deepEqual(n.visited, {});
  assert.equal(n.seenThrough, 0);
});

test("state is kept per verified email, case-insensitively", async () => {
  const e = env();
  await call(e, "PUT", { seenThrough: 5 }, await token());
  assert.deepEqual([...e.STATE.m.keys()], ["state:me@example.com"]);
});

test("no token, a forged token, an expired one or another team's: all refused", async () => {
  const e = env();
  const { privateKey: other } = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["sign", "verify"]);
  for (const tok of [null, await token({}, other), await token({ exp: 1 }),
                     await token({ iss: "https://evil.cloudflareaccess.com" }), await token({}, privateKey, "nope")]) {
    const r = await call(e, "PUT", { seenThrough: 1 }, tok);
    assert.equal(r.status, 401, String(tok).slice(0, 20));
  }
  assert.equal(e.STATE.m.size, 0);                  // nothing was written
});

test("without the KV binding it says so instead of pretending", async () => {
  const r = await call({ ACCESS_TEAM: TEAM }, "GET", null, await token());
  assert.equal(r.status, 503);
  assert.match(r.body.error, /STATE/);
});

test("a sync with nothing new reads but does not write (free tier: 1,000 writes a day)", async () => {
  const e = env(), t = await token();
  const doc = { ticks: { "acme|1": [1, 1000], "acme|2": [0, 2000] }, visited: { acme: 50, beta: 7 }, seenThrough: 10 };
  await call(e, "PUT", doc, t);
  assert.equal(e.STATE.puts, 1);
  await call(e, "PUT", doc, t);                                   // same state again
  await call(e, "PUT", { visited: { beta: 7, acme: 50 } }, t);    // a subset, keys in another order
  await call(e, "PUT", {}, t);                                    // a fresh device with nothing
  assert.equal(e.STATE.puts, 1);
  await call(e, "PUT", { ticks: { "acme|3": [1, 3000] } }, t);    // something actually new
  assert.equal(e.STATE.puts, 2);
});
