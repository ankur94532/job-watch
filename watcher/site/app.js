/* Read-only view over data/. No framework, no build step, no network beyond this
   origin. Two screens, routed on the hash so Back works and a company can be
   bookmarked:

     #/            companies, most recently active first
     #/c/<slug>    that company's open matching reqs, newest poll first
     #/new         everything unseen, across companies

   The home screen reads only data/jobs.json (a few KB). A company screen fetches
   only that company's file and renders it a page at a time — a phone should not
   be handed 900 rows to scroll, nor a 270KB download to read twenty.

   Opening index.html from disk against a local data/ works exactly like the
   deployed copy. */
"use strict";

var PAGE_SIZE = 25;               // rows per page; "Load more" advances the cursor
var STALE_AFTER = 24 * 3600;      // seconds before the page says so out loud
var STORAGE_KEY = "jobwatch:seenThrough";
var LEGACY_KEY = "jobwatch:lastVisit";
var TICKED_KEY = "jobwatch:ticked";
var TICKED_CAP = 5000;            // plenty for years of ticking; bounds localStorage
var OPENED_KEY = "jobwatch:opened";
var OPENED_CAP = 200;             // only needs to outlive a round trip to an ATS tab

var REFRESH_EVERY = 60000;        // how often to re-check the index while you are reading

var state = {
  index: null,        // data/jobs.json
  pending: null,      // a newer index, fetched but NOT applied until you tap
  view: "home",       // "home" | "company" | "new"
  company: null,      // index entry when view === "company"
  jobs: [],           // rows in play (one company, or everything unseen)
  shown: PAGE_SIZE,   // pagination cursor: how many of `jobs` are rendered
  seenThrough: 0,
  ticked: {},         // "company|job_id" -> 1, the jobs you have dealt with
  opened: {},         // "company|job_id" -> when you tapped through to the posting
  firstRun: false,
  cache: {}           // file -> rows, so going back and forth refetches nothing
};

/* What counts as "seen" is a POLL timestamp from the data, not the wall-clock
   moment you opened the page: `first_seen` is the clock of the run that
   discovered the job. Anchoring to Date.now() meant a glance at a stale page
   buried jobs found before it but published after. */
function isNew(job) { return state.seenThrough > 0 && (job.first_seen || 0) > state.seenThrough; }
function companyHasNew(c) { return state.seenThrough > 0 && (c.newest || 0) > state.seenThrough; }

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
}
/* Ticked-off jobs. Keyed on the store's own (company, job_id) — the only
   identifier every ATS supplies; over a thousand rows carry no requisition
   number, so req_id cannot be the key. */
function tickKey(job) { return job.company + "|" + job.job_id; }
function isTicked(job) { return state.ticked[tickKey(job)] === 1; }

function readTicked() {
  try {
    var raw = window.localStorage.getItem(TICKED_KEY);
    var list = raw ? JSON.parse(raw) : [];
    var out = {};
    if (Object.prototype.toString.call(list) === "[object Array]") {
      list.forEach(function (k) { out[k] = 1; });
    }
    return out;
  } catch (e) { return {}; }
}
function writeTicked() {
  try {
    var keys = Object.keys(state.ticked);
    if (keys.length > TICKED_CAP) keys = keys.slice(keys.length - TICKED_CAP);
    window.localStorage.setItem(TICKED_KEY, JSON.stringify(keys));
  } catch (e) { /* storage full or blocked: the tick lasts this session only */ }
}
function toggleTicked(job) {
  var key = tickKey(job);
  if (state.ticked[key]) {
    delete state.ticked[key];
  } else {
    state.ticked[key] = 1;
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
function isOpened(job) { return !!state.opened[tickKey(job)]; }

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
}

/* A job you ticked is dealt with, so it should not keep the company badged as
   having something new, nor count towards "unseen". */
function countsAsUnseen(job) { return isNew(job) && !isTicked(job); }

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

/* A poll can land while you are reading. Replacing the list under your thumb
   would move the row you were about to tap, so a newer index is fetched,
   parked, and announced — you decide when it lands. */
function checkForUpdates() {
  if (!state.index || (typeof document !== "undefined" && document.hidden === true)) return;
  getJSON("data/jobs.json").then(function (fresh) {
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
  return getJSON("data/" + entry.file).then(function (d) {
    state.cache[entry.file] = d.jobs || [];
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
  fresh.textContent = "checked " + ago(age);
  if (age > STALE_AFTER) {
    stale.hidden = false;
    stale.textContent = "⚠ Boards last checked " + ago(age) + ". The watcher has not run since — " +
      "the laptop is probably asleep. Treat the list as out of date.";
  } else {
    stale.hidden = true;
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
  var companies = (state.index.companies || []);
  var frag = document.createDocumentFragment();
  var shown = 0, withNew = 0;

  companies.forEach(function (c) {
    if (companyHasNew(c)) withNew++;
    if (!matchesQuery(c.company)) return;
    if (newOnly && !companyHasNew(c)) return;
    shown++;
    var node = tpl.content.cloneNode(true);
    var row = el(".company-row", node);
    row.href = "#/c/" + slug(c.company);
    el(".company-name", node).textContent = c.company;
    el(".company-meta", node).textContent =
      c.count + (c.count === 1 ? " open role" : " open roles") + " · newest " + ago(now - c.newest);
    if (companyHasNew(c)) row.classList.add("is-new");
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
    shown + " of " + companies.length + " companies · " + state.index.count + " open roles" +
    (withNew ? " · " + withNew + " with new" : "");
  var mark = el("#markread");
  mark.hidden = withNew === 0;
  mark.textContent = "Mark all read";
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
}

function render() {
  if (!state.index) return;
  if (state.view === "home") renderHome(); else renderJobs();
}

/* Only ever advanced by an explicit tap: opening the page does not clear
   anything, so a quick look on the train cannot discard the backlog. */
function markAllRead() {
  state.seenThrough = Math.max(state.seenThrough, newestPoll());
  writeSeenThrough(state.seenThrough);
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
  }

  var m = hash.match(/^\/c\/(.+)$/);
  if (m) {
    var wanted = decodeURIComponent(m[1]);
    var entry = (state.index.companies || []).filter(function (c) { return slug(c.company) === wanted; })[0];
    if (!entry) { window.location.hash = "#/"; return; }   // e.g. its last role closed
    state.view = "company";
    state.company = entry;
    if (!preserve) el("#newonly").checked = false;
    loadCompany(entry).then(function (rows) { state.jobs = rows; render(); }, failed);
    return;
  }

  if (hash === "/new") {
    state.view = "new";
    state.company = null;
    el("#newonly").checked = true;
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

  state.view = "home";
  state.company = null;
  state.jobs = [];
  if (!preserve) el("#newonly").checked = false;
  render();
}

function start() {
  state.ticked = readTicked();
  state.opened = readOpened();
  state.seenThrough = readSeenThrough();
  state.firstRun = state.seenThrough === 0;

  el("#q").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#newonly").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#showhidden").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#markread").addEventListener("click", markAllRead);
  el("#more").addEventListener("click", function () { state.shown += PAGE_SIZE; render(); });
  el("#refresh").addEventListener("click", applyUpdate);
  window.addEventListener("hashchange", function () { route(false); });

  getJSON("data/jobs.json").then(function (index) {
    state.index = index;
    /* First ever visit: there is no "already seen", and badging every row would
       make the marker meaningless. Mark through the newest poll, count from the next. */
    if (state.firstRun) {
      state.seenThrough = newestPoll();
      writeSeenThrough(state.seenThrough);
    }
    renderFreshness();
    route(false);
  }).catch(function (err) {
    el("#freshness").textContent = "could not load data";
    failed(err);
  });
  setInterval(function () { renderFreshness(); checkForUpdates(); }, REFRESH_EVERY);
  /* Coming back to the tab is the moment you most want to know. */
  if (typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden !== true) checkForUpdates();
    });
  }
}

document.addEventListener("DOMContentLoaded", start);
