/* Read-only view over the current job snapshot. No framework, no build step,
   and no network beyond this origin. Two screens, routed on the hash so Back works and a company can be
   bookmarked:

     #/            companies: new since you last opened them first, then A–Z
     #/c/<slug>    that company's open matching reqs, newest poll first
     #/new         everything unseen, across companies

   Online, the snapshot is read from KV through api/jobs; the watcher updates
   that one KV value without deploying the website again. The home screen asks
   only for the index. A company screen asks only for that company's rows and renders
   them a page at a time — a phone should not
   be handed 900 rows to scroll, nor a 270KB download to read twenty.

   Opening index.html from disk against a local data/ works exactly like the
   deployed copy, minus sync.

   What you do here (ticks, "opened" marks, which poll each company was last
   opened at) follows you between devices through api/state, a Pages Function
   behind the same Cloudflare Access login. localStorage stays the working copy:
   the page renders from it, works offline, and merges whenever it reaches the
   endpoint. A local copy has no endpoint and says so once, with a link that
   carries its ticks to the synced one. */
"use strict";

var PAGE_SIZE = 25;               // rows per page; "Load more" advances the cursor
var STALE_AFTER = 24 * 3600;      // seconds before the page says so out loud
var STORAGE_KEY = "jobwatch:seenThrough";
var LEGACY_KEY = "jobwatch:lastVisit";
var TICKED_KEY = "jobwatch:ticked";     // legacy: a plain list of ticked keys, pre-sync
var TICKLOG_KEY = "jobwatch:tickLog";   // "company|job_id" -> [1 ticked / 0 unticked, ms]
var TICKED_CAP = 5000;            // plenty for years of ticking; bounds localStorage
var TOMBSTONE_TTL = 60 * 24 * 3600 * 1000;   // an untick only has to outlive a sync
var OPENED_KEY = "jobwatch:opened";
var OPENED_CAP = 200;             // only needs to outlive a round trip to an ATS tab
var VISITED_KEY = "jobwatch:visited";   // company -> the poll it was last opened at
var SCROLL_KEY = "jobwatch:homeScroll"; // sessionStorage: survives this tab reloading

var REFRESH_EVERY = 60000;        // how often to re-check the index while you are reading
var SYNC_URL = "api/state";
var JOBS_URL = "api/jobs";
var SYNC_DELAY = 800;             // coalesce a burst of ticks into one request

var state = {
  index: null,        // api/jobs online; data/jobs.json from a local file
  pending: null,      // a newer index, fetched but NOT applied until you tap
  view: "home",       // "home" | "company" | "new"
  company: null,      // index entry when view === "company"
  jobs: [],           // rows in play (one company, or everything unseen)
  shown: PAGE_SIZE,   // pagination cursor: how many of `jobs` are rendered
  seenThrough: 0,
  ticked: {},         // "company|job_id" -> 1, the jobs you have dealt with (derived)
  tickLog: {},        // "company|job_id" -> [1|0, ms]: ticks AND unticks, for merging
  opened: {},         // "company|job_id" -> when you tapped through to the posting
  firstRun: false,
  cache: {},          // file -> rows, so going back and forth refetches nothing
  visited: {},        // company -> poll timestamp you last opened it at
  anchors: {},        // company -> what THIS visit treats as already-seen
  homeScroll: 0,      // where you were in the company list
  restoreScroll: false,
  leavingHome: false, // a tap has fired; ignore the browser's own scroll
  toTop: false,
  sync: {
    mode: "unknown",  // "ok" | "off" (no endpoint: a local copy) | "signedout" | "error"
    inFlight: false,
    dirty: false,     // changed since the last request left
    timer: null,
    adopting: false   // writing merged state back: must not trigger another sync
  }
};

/* What counts as "seen" is a POLL timestamp from the data, not the wall-clock
   moment you opened the page: `first_seen` is the clock of the run that
   discovered the job. Anchoring to Date.now() meant a glance at a stale page
   buried jobs found before it but published after.

   "New" is per company, not global: a company is new only if it has gained
   something since YOU LAST OPENED THAT COMPANY. One global timestamp meant
   reading Adobe cleared the dot on Stripe. `seenThrough` survives as the
   baseline for companies you have never opened, so a fresh install does not
   badge all 58 at once. */
function visitAnchor(company) {
  var v = state.visited[company];
  return (typeof v === "number" && isFinite(v)) ? v : state.seenThrough;
}

/* Opening a company clears its dot straight away, even with every row still
   unticked — that is what was asked for. But the rows you opened it to read
   must stay marked for the duration of the visit, so the anchor is snapshotted
   on entry while the stored value moves on. */
function renderAnchor(company) {
  var a = state.anchors[company];
  return (typeof a === "number") ? a : visitAnchor(company);
}

function isNew(job) {
  var a = renderAnchor(job.company);
  return a > 0 && (job.first_seen || 0) > a;
}
function companyHasNew(c) {
  var a = visitAnchor(c.company);
  return a > 0 && (c.newest || 0) > a;
}

function readVisited() {
  try {
    var raw = window.localStorage.getItem(VISITED_KEY);
    var obj = raw ? JSON.parse(raw) : {};
    return (obj && typeof obj === "object") ? obj : {};
  } catch (e) { return {}; }
}
function writeVisited() {
  try { window.localStorage.setItem(VISITED_KEY, JSON.stringify(state.visited)); } catch (e) { /* fine */ }
  scheduleSync();
}

/* Called on entry to a company screen. Records the poll you have now seen it
   through, which is what makes the dot go away and stay away until the next
   run finds something. */
function markVisited(entry) {
  var ts = entry.newest || 0;
  if (!(state.visited[entry.company] >= ts)) {
    state.visited[entry.company] = ts;
    writeVisited();
  }
}

function readSeenThrough() {
  try {
    var v = window.localStorage.getItem(STORAGE_KEY);
    if (v === null) v = window.localStorage.getItem(LEGACY_KEY);
    var n = parseInt(v || "0", 10);
    return isFinite(n) ? n : 0;
  } catch (e) { return 0; }       // private mode, blocked storage: degrade, never throw
}
function writeSeenThrough(ts) {
  try { window.localStorage.setItem(STORAGE_KEY, String(ts)); } catch (e) { /* fine */ }
  scheduleSync();
}
/* Ticked-off jobs. Keyed on the store's own (company, job_id) — the only
   identifier every ATS supplies; over a thousand rows carry no requisition
   number, so req_id cannot be the key. */
function tickKey(job) { return job.company + "|" + job.job_id; }
function isTicked(job) { return state.ticked[tickKey(job)] === 1; }

/* A tick is stored with WHEN, and unticking leaves a [0, when] rather than
   deleting the key. Without that, syncing could not tell "unticked on the
   phone" from "never ticked here", and the laptop's older tick would come
   straight back. */
function readTickLog() {
  try {
    var raw = window.localStorage.getItem(TICKLOG_KEY);
    if (raw !== null) {
      var log = JSON.parse(raw);
      return (log && typeof log === "object") ? log : {};
    }
    /* Pre-sync storage: a bare list of ticked keys. Carried over as ticks from
       the dawn of time, so any real action on another device wins over them. */
    var list = JSON.parse(window.localStorage.getItem(TICKED_KEY) || "[]");
    var out = {};
    if (Object.prototype.toString.call(list) === "[object Array]") {
      list.forEach(function (k) { out[k] = [1, 1]; });
    }
    return out;
  } catch (e) { return {}; }
}
function tickedFrom(log) {
  var out = {};
  for (var k in log) {
    if (Object.prototype.hasOwnProperty.call(log, k) && log[k] && log[k][0] === 1) out[k] = 1;
  }
  return out;
}
function readTicked() {
  state.tickLog = readTickLog();
  return tickedFrom(state.tickLog);
}
function writeTicked() {
  state.ticked = tickedFrom(state.tickLog);
  try {
    var keys = Object.keys(state.tickLog);
    if (keys.length > TICKED_CAP) {
      keys.sort(function (a, b) { return state.tickLog[b][1] - state.tickLog[a][1]; });
      keys.slice(TICKED_CAP).forEach(function (k) { delete state.tickLog[k]; });
    }
    window.localStorage.setItem(TICKLOG_KEY, JSON.stringify(state.tickLog));
  } catch (e) { /* storage full or blocked: the tick lasts this session only */ }
  scheduleSync();
}
function setTick(key, on) { state.tickLog[key] = [on ? 1 : 0, Date.now()]; }
function toggleTicked(job) {
  var key = tickKey(job);
  if (state.ticked[key]) {
    setTick(key, false);
  } else {
    setTick(key, true);
    delete state.opened[key];      // dealt with: it no longer needs finding again
    writeOpened();
  }
  writeTicked();
  render();
}

/* Tapping a row opens the ATS posting in another tab. Coming back, the row you
   just applied through has to be findable — otherwise you are re-reading titles
   to work out where you were. Recorded on click and persisted, because a
   background tab can be discarded and reloaded while you are away. */
/* A ticked job is never shown as "opened": the mark is merged as a max across
   devices, so deleting it on tick would not stick; ticking has to outrank it. */
function isOpened(job) { return !!state.opened[tickKey(job)] && !isTicked(job); }

function readOpened() {
  try {
    var raw = window.localStorage.getItem(OPENED_KEY);
    var obj = raw ? JSON.parse(raw) : {};
    return (obj && typeof obj === "object") ? obj : {};
  } catch (e) { return {}; }
}
function writeOpened() {
  try {
    var keys = Object.keys(state.opened);
    if (keys.length > OPENED_CAP) {
      keys.sort(function (a, b) { return state.opened[a] - state.opened[b]; });
      keys.slice(0, keys.length - OPENED_CAP).forEach(function (k) { delete state.opened[k]; });
    }
    window.localStorage.setItem(OPENED_KEY, JSON.stringify(state.opened));
  } catch (e) { /* storage blocked: the highlight lasts this session only */ }
  scheduleSync();
}

/* A job you ticked is dealt with, so it should not keep the company badged as
   having something new, nor count towards "unseen". */
function countsAsUnseen(job) { return isNew(job) && !isTicked(job); }

/* How many of a company's open roles you have NOT ticked off yet — the number
   the home screen leads with, since it is the size of the pile still to look at.

   Derived from the tick keys rather than stored alongside them, so the count
   can never disagree with the ticks themselves. The home screen only has the
   index, which lists each company's open job ids for exactly this. */
function tickedCountFor(c) {
  /* Only ticks on jobs that are open NOW. A tick outlives its job: counted by
     "company|" prefix alone, ticks on closed jobs were subtracted too, and a
     company with one job left read "0 unticked of 29" until you opened it. */
  if (c.ids) {
    return c.ids.filter(function (id) { return state.ticked[c.company + "|" + id] === 1; }).length;
  }
  var prefix = c.company + "|", n = 0;         // an index exported before ids were added
  for (var k in state.ticked) {
    if (Object.prototype.hasOwnProperty.call(state.ticked, k) && k.lastIndexOf(prefix, 0) === 0) n++;
  }
  return n;
}
function unticked(c) { return Math.max(0, (c.count || 0) - tickedCountFor(c)); }

/* A job you ticked and that later closed leaves its key behind for ever, and
   the home count would drift below the real number of open roles. Whenever a
   company's rows are in hand, drop ticks for jobs no longer on its board.
   Guarded on a non-empty fetch: an empty or half-written file must not be
   read as "everything closed" and wipe the ticks. */
function pruneTicked(company, rows) {
  if (!rows || !rows.length) return;
  var live = {};
  rows.forEach(function (j) { live[tickKey(j)] = 1; });
  var prefix = company + "|", changed = false;
  Object.keys(state.ticked).forEach(function (k) {
    if (k.lastIndexOf(prefix, 0) === 0 && !live[k]) { setTick(k, false); changed = true; }
  });
  if (changed) writeTicked();
}

/* ---- sync across devices ------------------------------------------------ */

function localDoc() {
  return { v: 1, ticks: state.tickLog, opened: state.opened, visited: state.visited,
           seenThrough: state.seenThrough };
}

/* The same merge the endpoint runs (functions/api/state.js), so a response can
   be folded into whatever you did while the request was in flight instead of
   overwriting it. Order-free: timestamps take the max, ticks the newest action. */
function mergeDocs(a, b) {
  var ticks = {}, now = Date.now(), k;
  var at = a.ticks || {}, bt = b.ticks || {};
  for (k in at) if (Object.prototype.hasOwnProperty.call(at, k)) ticks[k] = at[k];
  for (k in bt) {
    if (!Object.prototype.hasOwnProperty.call(bt, k) || !bt[k]) continue;
    if (!ticks[k] || bt[k][1] > ticks[k][1]) ticks[k] = bt[k];
  }
  for (k in ticks) if (ticks[k][0] === 0 && now - ticks[k][1] > TOMBSTONE_TTL) delete ticks[k];
  return {
    v: 1,
    ticks: ticks,
    opened: maxMerge(a.opened, b.opened),
    visited: maxMerge(a.visited, b.visited),
    seenThrough: Math.max(a.seenThrough || 0, b.seenThrough || 0)
  };
}
function maxMerge(a, b) {
  var out = {}, k;
  a = a || {}; b = b || {};
  for (k in a) if (Object.prototype.hasOwnProperty.call(a, k)) out[k] = a[k];
  for (k in b) if (Object.prototype.hasOwnProperty.call(b, k)) out[k] = Math.max(out[k] || 0, b[k]);
  return out;
}

/* Fold another device's state in. Returns true if anything you would see changed. */
function adopt(doc) {
  var before = JSON.stringify(localDoc());
  var m = mergeDocs(localDoc(), doc || {});
  state.tickLog = m.ticks;
  state.opened = m.opened;
  state.visited = m.visited;
  state.seenThrough = m.seenThrough;
  if (JSON.stringify(localDoc()) === before) return false;
  state.sync.adopting = true;       // persist, but this is not a new local change
  try { writeTicked(); writeOpened(); writeVisited(); writeSeenThrough(state.seenThrough); }
  finally { state.sync.adopting = false; }
  return true;
}

function scheduleSync() {
  if (state.sync.adopting || state.sync.mode === "off") return;
  state.sync.dirty = true;
  if (state.sync.timer) clearTimeout(state.sync.timer);
  state.sync.timer = setTimeout(function () { state.sync.timer = null; syncNow(); }, SYNC_DELAY);
}

/* One round trip: send everything, fold in what comes back. Resolves to true if
   the merged state changed what is on screen. Never rejects: offline, signed
   out and "no endpoint here" are all normal, and the local copy carries on. */
function syncNow() {
  var s = state.sync;
  if (s.mode === "off" || typeof fetch !== "function") return Promise.resolve(false);
  if (s.inFlight) { s.dirty = true; return Promise.resolve(false); }
  if (window.location.protocol === "file:") { s.mode = "off"; showCarry(); return Promise.resolve(false); }
  /* A tab in the background has nothing to show and nothing new to send,
     unless a change is still waiting to go. The minute timer keeps firing in
     hidden tabs, and a laptop tab left open all day was most of the KV
     traffic. Coming back to the tab syncs straight away. */
  if (typeof document !== "undefined" && document.hidden === true && !s.dirty) {
    return Promise.resolve(false);
  }
  s.inFlight = true;
  s.dirty = false;
  var reached = false;
  return fetch(SYNC_URL, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(localDoc()),
    credentials: "same-origin",
    cache: "no-store",
    redirect: "manual"              // an expired Access session answers with a redirect
  }).then(function (r) {
    if (r.type === "opaqueredirect" || r.status === 401) { s.mode = "signedout"; return false; }
    if (r.status === 404 || r.status === 405 || r.status === 501) { s.mode = "off"; return false; }
    if (!r.ok) { s.mode = "error"; s.dirty = true; return false; }
    return r.json().then(function (doc) { s.mode = "ok"; reached = true; return adopt(doc); });
  }, function () {
    s.mode = s.mode === "ok" ? "ok" : "error";    // offline: keep going locally
    s.dirty = true;
    return false;
  }).then(function (changed) {
    s.inFlight = false;
    renderFreshness();
    showCarry();
    /* Something changed while the request was out: send it now. After a
       failure, leave it for the minute timer rather than retry in a loop. */
    if (reached && s.dirty) scheduleSync();
    return changed;
  });
}

/* ---- carrying a local copy's ticks to the synced one ---------------------- */

function hasLocalState() {
  return Object.keys(state.tickLog).length > 0 || Object.keys(state.visited).length > 0;
}
function b64urlEncode(text) {
  var bytes = unescape(encodeURIComponent(text));
  return btoa(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return decodeURIComponent(escape(atob(s)));
}

/* Shown only on a copy with no sync endpoint that has something worth keeping.
   The ticks ride in the URL fragment, which never leaves the browser. */
function showCarry() {
  var p = el("#carry");
  if (!p) return;
  var target = state.index && state.index.site_url;
  var show = state.sync.mode === "off" && !!target && hasLocalState();
  p.hidden = !show;
  if (show) el("#carrylink").href = target + "#/import/" + b64urlEncode(JSON.stringify(localDoc()));
}

function importFrom(encoded) {
  var doc;
  try { doc = JSON.parse(b64urlDecode(encoded)); } catch (e) { doc = null; }
  var n = doc && doc.ticks ? Object.keys(doc.ticks).filter(function (k) { return doc.ticks[k][0] === 1; }).length : 0;
  if (doc && window.confirm("Bring over " + n + " ticked job" + (n === 1 ? "" : "s") +
                            " and your read marks from the local copy?")) {
    state.sync.adopting = false;
    var m = mergeDocs(localDoc(), doc);
    state.tickLog = m.ticks; state.opened = m.opened; state.visited = m.visited;
    state.seenThrough = m.seenThrough;
    writeTicked(); writeOpened(); writeVisited(); writeSeenThrough(state.seenThrough);
    syncNow();
  }
  if (window.history && window.history.replaceState) {
    window.history.replaceState(null, "", window.location.pathname + "#/");   // drop the payload from the URL
    route(false);
  } else {
    window.location.hash = "#/";
  }
}

function newestPoll() {
  return ((state.index && state.index.companies) || []).reduce(
    function (m, c) { return Math.max(m, c.newest || 0); }, 0);
}

function ago(seconds) {
  if (!seconds || seconds < 0) seconds = 0;
  var m = Math.round(seconds / 60);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  var h = Math.round(m / 60);
  if (h < 24) return h + "h ago";
  var d = Math.round(h / 24);
  return d === 1 ? "yesterday" : d + "d ago";
}

function el(sel, root) { return (root || document).querySelector(sel); }
function now2() { return Math.floor(Date.now() / 1000); }
function slug(name) { return String(name).toLowerCase().replace(/[^a-z0-9_-]+/g, "-"); }

function getJSON(url) {
  return fetch(url, { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  });
}

/* A file:// copy remains self-contained for offline inspection. The deployed
   copy reads KV through the Pages Function, so every poll is fresh without a
   Pages deployment. Local HTTP test servers implement the same API shape. */
function indexURL() {
  return window.location.protocol === "file:" ? "data/jobs.json" : JOBS_URL;
}
function companyURL(entry) {
  return window.location.protocol === "file:"
    ? "data/" + entry.file
    : JOBS_URL + "?file=" + encodeURIComponent(entry.file);
}

/* A poll can land while you are reading. Replacing the list under your thumb
   would move the row you were about to tap, so a newer index is fetched,
   parked, and announced — you decide when it lands. */
function checkForUpdates() {
  if (!state.index || (typeof document !== "undefined" && document.hidden === true)) return;
  getJSON(indexURL()).then(function (fresh) {
    var mine = state.index.polled_at || state.index.generated_at || 0;
    var theirs = fresh.polled_at || fresh.generated_at || 0;
    if (theirs > mine) {
      state.pending = fresh;
      showUpdatePill(fresh);
    }
  }, function () { /* offline or a failed push: try again next tick */ });
}

function showUpdatePill(fresh) {
  var pill = el("#refresh");
  var added = fresh.count - (state.index.count || 0);
  pill.textContent = added > 0
    ? "↻ " + added + (added === 1 ? " new role" : " new roles") + " — tap to load"
    : "↻ New results — tap to load";
  pill.hidden = false;
}

function applyUpdate() {
  if (!state.pending) return;
  state.index = state.pending;
  state.pending = null;
  state.cache = {};                 // company files are stale too, not just the index
  el("#refresh").hidden = true;
  renderFreshness();
  route(true);                      // same screen, same filter, same page depth
}

function loadCompany(entry) {
  if (state.cache[entry.file]) return Promise.resolve(state.cache[entry.file]);
  return getJSON(companyURL(entry)).then(function (d) {
    state.cache[entry.file] = d.jobs || [];
    pruneTicked(entry.company, state.cache[entry.file]);
    return state.cache[entry.file];
  });
}

/* ---- freshness ---------------------------------------------------------- */

function renderFreshness() {
  var meta = state.index || {};
  var now = Math.floor(Date.now() / 1000);
  /* Age of the DATA, not of the file: rendering the page does not poll anything. */
  var stamp = meta.polled_at || meta.generated_at;
  var age = stamp ? now - stamp : null;
  var fresh = el("#freshness");
  var stale = el("#stale");
  if (age === null) { fresh.textContent = "unknown age"; return; }
  fresh.textContent = "checked " + ago(age) + syncLabel();
  if (age > STALE_AFTER) {
    stale.hidden = false;
    stale.textContent = "⚠ Boards last checked " + ago(age) + ". The watcher has not run since — " +
      "the laptop is probably asleep. Treat the list as out of date.";
  } else {
    stale.hidden = true;
  }
}

function syncLabel() {
  switch (state.sync.mode) {
    case "ok": return " · synced";
    case "signedout": return " · sync paused: reload to sign in";
    case "error": return " · not synced yet";
    default: return "";            // unknown (first request pending) or off (local copy)
  }
}

/* ---- rendering ---------------------------------------------------------- */

function matchesQuery(text) {
  var q = el("#q").value.trim().toLowerCase();
  return !q || String(text).toLowerCase().indexOf(q) !== -1;
}

function renderHome() {
  var list = el("#list");
  var tpl = el("#companyrow");
  var now = Math.floor(Date.now() / 1000);
  var newOnly = el("#newonly").checked;
  /* Only companies with something new since you last opened them sit at the
     top, newest first. Everything else is A–Z.

     The rest used to stay in most-recently-active order, and that defeated the
     point: the company you just opened is by definition the most recently
     active, so it dropped out of the new group straight into the row beneath
     it and looked as if it had never left the top. A–Z also means the list no
     longer reshuffles every poll, so a scroll position keeps meaning the same
     rows. */
  var companies = (state.index.companies || []).slice().sort(function (a, b) {
    var an = companyHasNew(a) ? 1 : 0, bn = companyHasNew(b) ? 1 : 0;
    if (an !== bn) return bn - an;
    if (an) return (b.newest || 0) - (a.newest || 0);
    var x = String(a.company).toLowerCase(), y = String(b.company).toLowerCase();
    return x < y ? -1 : x > y ? 1 : 0;
  });
  var frag = document.createDocumentFragment();
  var shown = 0, withNew = 0, leftTotal = 0;

  companies.forEach(function (c) {
    var isNewCo = companyHasNew(c);
    var left = unticked(c);
    if (isNewCo) withNew++;
    leftTotal += left;
    if (!matchesQuery(c.company)) return;
    if (newOnly && !isNewCo) return;
    shown++;
    var node = tpl.content.cloneNode(true);
    var row = el(".company-row", node);
    row.href = "#/c/" + slug(c.company);
    el(".company-name", node).textContent = c.company;
    /* Lead with what is left to look at, not the raw total: once you have
       ticked most of a board, "25 open roles" stops meaning anything. */
    var done = (c.count || 0) - left;
    el(".company-meta", node).textContent =
      c.count === 0 ? "No matching open roles" :
      (done > 0 ? left + " unticked of " + c.count : c.count + (c.count === 1 ? " open role" : " open roles")) +
      " · " + ago(now - c.newest);
    var dot = el(".dot", node);
    if (dot) {
      dot.hidden = !isNewCo;
      if (isNewCo) dot.setAttribute("title", "new since you last opened this company");
    }
    if (isNewCo) row.classList.add("is-new");
    if (left === 0) row.classList.add("is-clear");
    /* Where you were, captured at the tap — the last moment the number is
       yours rather than the browser's. */
    row.addEventListener("click", function () {
      state.homeScroll = window.pageYOffset || 0;
      state.leavingHome = true;
      writeHomeScroll(state.homeScroll);
    });
    frag.appendChild(node);
  });

  list.textContent = "";
  list.appendChild(frag);
  el("#empty").hidden = shown !== 0;
  el("#more").hidden = true;                 // one screenful of companies, no paging needed
  el("#showhiddenwrap").hidden = true;       // ticking is per job, so only on a job list
  el("#heading").textContent = "Job Watch";
  el("#back").hidden = true;
  el("#q").placeholder = "Filter companies…";
  el("#summary").textContent =
    shown + " of " + companies.length + " companies · " + leftTotal + " unticked" +
    (withNew ? " · " + withNew + " with new" : "");
  var mark = el("#markread");
  mark.hidden = withNew === 0;
  mark.textContent = "Mark all read";

  /* Back from a company lands you where you were in the list, not at the top of
     58 rows. Only on a navigation into home — re-rendering for a filter keystroke
     must not yank the page around. */
  if (state.restoreScroll) {
    state.restoreScroll = false;
    var y = state.homeScroll;
    if (y > 0) scrollSoon(y);
  }
  state.leavingHome = false;      // you are back on the list; track scrolling again
}

/* The position also goes to sessionStorage, because memory alone does not
   survive the tab reloading while you are away: Chrome's Memory Saver discards
   background tabs, and tapping a posting opens exactly such an away-tab. The
   reloaded company screen then sent you back to the top of the list.
   sessionStorage is per tab, so two open tabs keep their own places. */
function readHomeScroll() {
  try {
    var n = parseInt(window.sessionStorage.getItem(SCROLL_KEY) || "0", 10);
    return isFinite(n) && n > 0 ? n : 0;
  } catch (e) { return 0; }
}
function writeHomeScroll(y) {
  try { window.sessionStorage.setItem(SCROLL_KEY, String(Math.round(y))); } catch (e) { /* fine */ }
}

/* After paint, so the rows exist and the document is tall enough to scroll to.
   rAF twice because one frame is layout, the next is when it has a height. */
function scrollSoon(y) {
  var go = function () { window.scrollTo(0, y); };
  if (typeof window.requestAnimationFrame === "function") {
    window.requestAnimationFrame(function () { window.requestAnimationFrame(go); });
  } else {
    setTimeout(go, 0);
  }
}

function renderJobs() {
  var list = el("#list");
  var tpl = el("#row");
  var now = Math.floor(Date.now() / 1000);
  var newOnly = el("#newonly").checked;
  var frag = document.createDocumentFragment();

  var showTicked = el("#showhidden").checked;
  var tickedHere = state.jobs.filter(isTicked).length;
  var visible = state.jobs.filter(function (j) {
    if (!showTicked && isTicked(j)) return false;     // dealt with: gone until you ask
    if (newOnly && !countsAsUnseen(j)) return false;
    return matchesQuery(j.title);
  });
  /* Offset pagination over the rows already in memory: `shown` is the cursor and
     "Load more" advances it. The export hands them over newest-poll-first, so a
     later page is always older — nothing re-sorts and nothing jumps. */
  var page = visible.slice(0, state.shown);

  page.forEach(function (job) {
    var node = tpl.content.cloneNode(true);
    var card = el(".job", node);
    el(".hit", node).href = job.url || "#";
    el(".title", node).textContent = job.title;
    el(".company", node).textContent = job.company;
    el(".location", node).textContent = job.location || "—";
    el(".when", node).textContent = "found " + ago(now - job.first_seen);
    if (countsAsUnseen(job)) card.classList.add("is-new");
    if (isTicked(job)) card.classList.add("is-ticked");

    var openedLabel = el(".opened", node);
    var link = el(".hit", node);
    function showOpened() {
      card.classList.add("is-opened");
      openedLabel.hidden = false;
      openedLabel.textContent = "opened " + ago(now2() - state.opened[tickKey(job)]);
    }
    if (isOpened(job)) showOpened(); else openedLabel.hidden = true;
    /* Marked on the way out, without a re-render: replacing the node mid-click
       can cancel the navigation in some browsers. */
    link.addEventListener("click", function () {
      state.opened[tickKey(job)] = now2();
      writeOpened();
      showOpened();
    });

    var copy = el(".copy", node);
    if (job.req_id) {
      copy.textContent = "Copy req";
      copy.addEventListener("click", function (ev) { ev.preventDefault(); copyText(job.req_id, copy); });
    } else {
      copy.textContent = "no req id";
      copy.disabled = true;
    }

    var tick = el(".tick", node);
    tick.textContent = isTicked(job) ? "↩ Undo" : "✓ Done";
    tick.addEventListener("click", function (ev) { ev.preventDefault(); toggleTicked(job); });
    frag.appendChild(node);
  });

  list.textContent = "";
  list.appendChild(frag);
  el("#empty").hidden = page.length !== 0;

  var remaining = visible.length - page.length;
  var more = el("#more");
  more.hidden = remaining <= 0;
  more.textContent = "Load " + Math.min(PAGE_SIZE, remaining) + " more (" + remaining + " left)";

  var unseen = state.jobs.filter(countsAsUnseen).length;
  el("#heading").textContent = state.view === "new" ? "Unseen" : state.company.company;
  el("#back").hidden = false;
  el("#q").placeholder = "Filter titles…";
  el("#showhiddenwrap").hidden = tickedHere === 0;
  el("#summary").textContent = "showing " + page.length + " of " + visible.length +
    (unseen ? " · " + unseen + " unseen" : "") +
    (tickedHere ? " · " + tickedHere + " ticked off" : "");
  var mark = el("#markread");
  mark.hidden = unseen === 0;
  mark.textContent = "Mark " + unseen + " read";

  if (state.toTop) { state.toTop = false; scrollSoon(0); }
}

function render() {
  if (!state.index) return;
  if (state.view === "home") renderHome(); else renderJobs();
}

/* Only ever advanced by an explicit tap: opening the page does not clear
   anything, so a quick look on the train cannot discard the backlog.

   On a company screen it clears that company; on home it clears the lot. */
function markAllRead() {
  if (state.view === "company" && state.company) {
    var c = state.company;
    var ts = Math.max(state.visited[c.company] || 0, c.newest || 0);
    state.visited[c.company] = ts;
    state.anchors[c.company] = ts;      // drop the NEW marks in front of you too
    writeVisited();
  } else {
    state.seenThrough = Math.max(state.seenThrough, newestPoll());
    writeSeenThrough(state.seenThrough);
    (state.index.companies || []).forEach(function (co) {
      state.visited[co.company] = Math.max(state.visited[co.company] || 0, co.newest || 0);
    });
    writeVisited();
    state.anchors = {};
  }
  render();
}

/* navigator.clipboard is unavailable on http:// and in older iOS Safari, hence
   the execCommand fallback: copying the req id from a phone is the whole point. */
function copyText(text, button) {
  function done(ok) {
    button.textContent = ok ? "Copied ✓" : text;
    button.classList.toggle("done", ok);
    setTimeout(function () { button.textContent = "Copy req"; button.classList.remove("done"); }, 1600);
  }
  function fallback() {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      done(ok);
    } catch (e) { done(false); }
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
  } else {
    fallback();
  }
}

/* ---- routing ------------------------------------------------------------ */

function failed(err) {
  el("#summary").textContent = String((err && err.message) || err);
  el("#list").textContent = "";
}

function route(preserve) {
  if (!state.index) return;
  var hash = (window.location.hash || "#/").replace(/^#/, "");
  if (!preserve) {                  // a refresh keeps your place; a navigation does not
    state.shown = PAGE_SIZE;
    el("#q").value = "";
    el("#showhidden").checked = false;
    state.anchors = {};             // a fresh visit re-reads what counts as seen
  }

  var imp = hash.match(/^\/import\/(.+)$/);
  if (imp) { importFrom(imp[1]); return; }

  var m = hash.match(/^\/c\/(.+)$/);
  if (m) {
    var wanted = decodeURIComponent(m[1]);
    var entry = (state.index.companies || []).filter(function (c) { return slug(c.company) === wanted; })[0];
    if (!entry) { window.location.hash = "#/"; return; }   // e.g. its last role closed
    state.view = "company";
    state.company = entry;
    /* Snapshot first, then mark: the dot goes out now, but this visit still
       shows you which rows caused it. */
    if (!(entry.company in state.anchors)) state.anchors[entry.company] = visitAnchor(entry.company);
    markVisited(entry);
    if (!preserve) { el("#newonly").checked = false; state.toTop = true; }
    loadCompany(entry).then(function (rows) { state.jobs = rows; render(); }, failed);
    return;
  }

  if (hash === "/new") {
    state.view = "new";
    state.company = null;
    if (!preserve) state.toTop = true;
    el("#newonly").checked = true;
    /* Snapshot every badged company, but do NOT mark them visited: this is a
       cross-company skim, and clearing a dot is what opening the company does. */
    (state.index.companies || []).forEach(function (c) {
      if (!(c.company in state.anchors)) state.anchors[c.company] = visitAnchor(c.company);
    });
    /* Sequentially, so a phone on a slow link isn't opening a dozen requests. */
    (state.index.companies || []).filter(companyHasNew).reduce(function (chain, c) {
      return chain.then(function (acc) {
        return loadCompany(c).then(function (rows) { return acc.concat(rows); },
                                   function () { return acc; });
      });
    }, Promise.resolve([])).then(function (rows) {
      rows.sort(function (a, b) { return (b.first_seen || 0) - (a.first_seen || 0); });
      state.jobs = rows;
      render();
    }, failed);
    return;
  }

  if (state.view !== "home" && !preserve) state.restoreScroll = true;
  state.view = "home";
  state.company = null;
  state.jobs = [];
  if (!preserve) el("#newonly").checked = false;
  render();
}

function start() {
  state.ticked = readTicked();
  state.opened = readOpened();
  state.visited = readVisited();
  state.seenThrough = readSeenThrough();
  state.firstRun = state.seenThrough === 0;
  state.homeScroll = readHomeScroll();

  /* We put you back on the company list ourselves. Left on "auto", the browser
     also restores a scroll position on hash navigation and the two fight. */
  try {
    if (window.history && "scrollRestoration" in window.history) {
      window.history.scrollRestoration = "manual";
    }
  } catch (e) { /* not fatal: worst case the browser also has a guess */ }

  /* Tracks where you are in the list. It deliberately stops listening the moment
     you tap a company: following a fragment link makes the browser scroll the
     page toward the top BEFORE hashchange fires, so anything recorded after the
     tap is the browser's scroll, not yours. That is what made "back" land at an
     arbitrary offset instead of where you left. */
  window.addEventListener("scroll", function () {
    if (state.view === "home" && !state.leavingHome) state.homeScroll = window.pageYOffset || 0;
  }, false);

  el("#q").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#newonly").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#showhidden").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#markread").addEventListener("click", markAllRead);
  el("#more").addEventListener("click", function () { state.shown += PAGE_SIZE; render(); });
  el("#refresh").addEventListener("click", applyUpdate);
  /* Opening a screen is when you want the other device's ticks, so every
     navigation syncs too; a re-render then lands within the first moment. */
  window.addEventListener("hashchange", function () {
    route(false);
    syncNow().then(function (changed) { if (changed) render(); });
  });

  getJSON(indexURL()).then(function (index) {
    state.index = index;
    /* First ever visit: there is no "already seen", and badging every row would
       make the marker meaningless. Mark through the newest poll, count from the next. */
    if (state.firstRun) {
      state.seenThrough = newestPoll();
      writeSeenThrough(state.seenThrough);
    }
    renderFreshness();
    route(false);
    /* Render from the local copy first, then fold in the other devices. */
    syncNow().then(function (changed) { if (changed) render(); });
  }).catch(function (err) {
    el("#freshness").textContent = "could not load data";
    failed(err);
  });
  /* Background syncs update state but do not re-render: the list must not move
     under your thumb. Coming back to the tab is when other devices show up. */
  setInterval(function () { renderFreshness(); checkForUpdates(); syncNow(); }, REFRESH_EVERY);
  /* Coming back to the tab is the moment you most want to know. */
  if (typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden !== true) {
        checkForUpdates();
        syncNow().then(function (changed) { if (changed) render(); });
      }
    });
  }
}

document.addEventListener("DOMContentLoaded", start);
