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

var state = {
  index: null,        // data/jobs.json
  view: "home",       // "home" | "company" | "new"
  company: null,      // index entry when view === "company"
  jobs: [],           // rows in play (one company, or everything unseen)
  shown: PAGE_SIZE,   // pagination cursor: how many of `jobs` are rendered
  seenThrough: 0,
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
function slug(name) { return String(name).toLowerCase().replace(/[^a-z0-9_-]+/g, "-"); }

function getJSON(url) {
  return fetch(url, { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  });
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

  var visible = state.jobs.filter(function (j) {
    if (newOnly && !isNew(j)) return false;
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
    if (isNew(job)) card.classList.add("is-new");

    var copy = el(".copy", node);
    if (job.req_id) {
      copy.textContent = "Copy req";
      copy.addEventListener("click", function (ev) { ev.preventDefault(); copyText(job.req_id, copy); });
    } else {
      copy.textContent = "no req id";
      copy.disabled = true;
    }
    frag.appendChild(node);
  });

  list.textContent = "";
  list.appendChild(frag);
  el("#empty").hidden = page.length !== 0;

  var remaining = visible.length - page.length;
  var more = el("#more");
  more.hidden = remaining <= 0;
  more.textContent = "Load " + Math.min(PAGE_SIZE, remaining) + " more (" + remaining + " left)";

  var unseen = state.jobs.filter(isNew).length;
  el("#heading").textContent = state.view === "new" ? "Unseen" : state.company.company;
  el("#back").hidden = false;
  el("#q").placeholder = "Filter titles…";
  el("#summary").textContent = "showing " + page.length + " of " + visible.length +
    (unseen ? " · " + unseen + " unseen" : "");
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

function route() {
  if (!state.index) return;
  var hash = (window.location.hash || "#/").replace(/^#/, "");
  state.shown = PAGE_SIZE;
  el("#q").value = "";

  var m = hash.match(/^\/c\/(.+)$/);
  if (m) {
    var wanted = decodeURIComponent(m[1]);
    var entry = (state.index.companies || []).filter(function (c) { return slug(c.company) === wanted; })[0];
    if (!entry) { window.location.hash = "#/"; return; }
    state.view = "company";
    state.company = entry;
    el("#newonly").checked = false;
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
  el("#newonly").checked = false;
  render();
}

function start() {
  state.seenThrough = readSeenThrough();
  state.firstRun = state.seenThrough === 0;

  el("#q").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#newonly").addEventListener("input", function () { state.shown = PAGE_SIZE; render(); });
  el("#markread").addEventListener("click", markAllRead);
  el("#more").addEventListener("click", function () { state.shown += PAGE_SIZE; render(); });
  window.addEventListener("hashchange", route);

  getJSON("data/jobs.json").then(function (index) {
    state.index = index;
    /* First ever visit: there is no "already seen", and badging every row would
       make the marker meaningless. Mark through the newest poll, count from the next. */
    if (state.firstRun) {
      state.seenThrough = newestPoll();
      writeSeenThrough(state.seenThrough);
    }
    renderFreshness();
    route();
  }).catch(function (err) {
    el("#freshness").textContent = "could not load data";
    failed(err);
  });
  setInterval(renderFreshness, 60000);
}

document.addEventListener("DOMContentLoaded", start);
