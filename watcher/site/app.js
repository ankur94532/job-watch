/* Read-only view over data/jobs.json. No framework, no build step, no network
   beyond this origin. Opening index.html from disk against a local jobs.json
   works exactly the same as the deployed copy. */
"use strict";

/* What counts as "seen" is a POLL timestamp from the data, not the wall-clock
   moment you opened the page. `first_seen` is the clock of the run that
   discovered the job, so marking through it is stable: glancing at a stale page
   at 08:00 cannot bury jobs the watcher found at 07:00 but had not published
   yet. Anchoring to Date.now() did exactly that, and those jobs were never
   shown as new again. */
var STORAGE_KEY = "jobwatch:seenThrough";
var LEGACY_KEY = "jobwatch:lastVisit";
var STALE_AFTER = 24 * 3600;      // seconds before the page says so out loud
var state = { jobs: [], meta: null, seenThrough: 0, firstRun: false };

function isNew(job) {
  return state.seenThrough > 0 && (job.first_seen || 0) > state.seenThrough;
}

function newestPoll() {
  return state.jobs.reduce(function (max, j) {
    return Math.max(max, j.first_seen || 0);
  }, 0);
}

/* localStorage throws in private mode and in some embedded webviews; a missing
   marker must never take the page down with it. */
function readSeenThrough() {
  try {
    var v = window.localStorage.getItem(STORAGE_KEY);
    if (v === null) v = window.localStorage.getItem(LEGACY_KEY);   // pre-upgrade stamp
    var n = parseInt(v || "0", 10);
    return isFinite(n) ? n : 0;
  } catch (e) { return 0; }
}
function writeSeenThrough(ts) {
  try { window.localStorage.setItem(STORAGE_KEY, String(ts)); } catch (e) { /* fine */ }
}

/* Only ever advanced by an explicit "Mark all read". Opening the page does not
   clear anything, so a quick look on the train cannot silently discard the
   backlog you meant to come back to. */
function markAllRead() {
  state.seenThrough = Math.max(state.seenThrough, newestPoll());
  writeSeenThrough(state.seenThrough);
  render();
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

function renderFreshness() {
  var meta = state.meta || {};
  var now = Math.floor(Date.now() / 1000);
  /* Age of the DATA, not of the file. Rendering the page does not poll anything,
     so an export run against an untouched store must not read as fresh. */
  var stamp = meta.polled_at || meta.generated_at;
  var age = stamp ? now - stamp : null;
  var fresh = el("#freshness");
  var stale = el("#stale");
  if (age === null) {
    fresh.textContent = "unknown age";
    return;
  }
  fresh.textContent = "checked " + ago(age);
  if (age > STALE_AFTER) {
    /* A dashboard that looks live while the laptop is shut is worse than none. */
    stale.hidden = false;
    stale.textContent = "⚠ Boards last checked " + ago(age) + ". The watcher has not run since — " +
      "the laptop is probably asleep. Treat the list as out of date.";
  } else {
    stale.hidden = true;
  }
}

function matches(job) {
  var q = el("#q").value.trim().toLowerCase();
  var company = el("#company").value;
  var newOnly = el("#newonly").checked;
  if (company && job.company !== company) return false;
  if (newOnly && !isNew(job)) return false;
  if (q && job.title.toLowerCase().indexOf(q) === -1) return false;
  return true;
}

function render() {
  var list = el("#list");
  var tpl = el("#row");
  var now = Math.floor(Date.now() / 1000);
  var shown = 0;
  var frag = document.createDocumentFragment();

  state.jobs.forEach(function (job) {
    if (!matches(job)) return;
    shown++;
    var node = tpl.content.cloneNode(true);
    var card = el(".job", node);
    var link = el(".hit", node);
    link.href = job.url || "#";
    el(".title", node).textContent = job.title;
    el(".company", node).textContent = job.company;
    el(".location", node).textContent = job.location || "—";
    el(".when", node).textContent = "seen " + ago(now - job.first_seen);
    if (isNew(job)) card.classList.add("is-new");

    var copy = el(".copy", node);
    if (job.req_id) {
      copy.textContent = "Copy req";
      copy.addEventListener("click", function (ev) {
        ev.preventDefault();
        copyText(job.req_id, copy);
      });
    } else {
      copy.textContent = "no req id";
      copy.disabled = true;
    }
    frag.appendChild(node);
  });

  list.textContent = "";
  list.appendChild(frag);
  el("#empty").hidden = shown !== 0;

  var unseen = 0;
  state.jobs.forEach(function (j) { if (isNew(j)) unseen++; });
  var meta = state.meta || {};
  el("#summary").textContent = shown + " of " + state.jobs.length + " open matches" +
    (unseen ? " · " + unseen + " unseen" : "") +
    (meta.companies ? " · " + meta.companies + " companies watched" : "");

  var mark = el("#markread");
  mark.hidden = unseen === 0;
  mark.textContent = "Mark " + unseen + " read";
}

/* navigator.clipboard is unavailable on http:// and in older iOS Safari, hence
   the execCommand fallback: copying the req id from a phone is the whole point. */
function copyText(text, button) {
  function done(ok) {
    button.textContent = ok ? "Copied ✓" : text;
    button.classList.toggle("done", ok);
    setTimeout(function () { button.textContent = "Copy req"; button.classList.remove("done"); }, 1600);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, function () { fallback(); });
  } else {
    fallback();
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
}

function fillCompanies() {
  var select = el("#company");
  var counts = {};
  state.jobs.forEach(function (j) { counts[j.company] = (counts[j.company] || 0) + 1; });
  Object.keys(counts).sort().forEach(function (name) {
    var opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name + " (" + counts[name] + ")";
    select.appendChild(opt);
  });
}

function getJSON(url) {
  return fetch(url, { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  });
}

function load() {
  return getJSON("data/jobs.json").then(function (data) {
    state.meta = data;
    if (!data.shards) return data.jobs || [];
    /* Sharded payload: one file per company, fetched in sequence so a phone on a
       slow connection isn't opening a dozen requests at once. */
    return data.shards.reduce(function (chain, shard) {
      return chain.then(function (acc) {
        return getJSON("data/" + shard.file).then(function (part) {
          return acc.concat(part.jobs || []);
        }, function () { return acc; });
      });
    }, Promise.resolve([]));
  }).then(function (jobs) {
    jobs.sort(function (a, b) { return (b.first_seen || 0) - (a.first_seen || 0); });
    state.jobs = jobs;
  });
}

function start() {
  state.seenThrough = readSeenThrough();
  state.firstRun = state.seenThrough === 0;
  ["#q", "#company", "#newonly"].forEach(function (sel) {
    el(sel).addEventListener("input", render);
  });
  el("#markread").addEventListener("click", markAllRead);
  load().then(function () {
    fillCompanies();
    renderFreshness();
    /* First ever visit: there is no "already seen", and badging 900 rows would
       make the marker meaningless. Mark through the newest poll and start
       counting from the next one. */
    if (state.firstRun) {
      state.seenThrough = newestPoll();
      writeSeenThrough(state.seenThrough);
    } else if (state.jobs.some(isNew)) {
      /* Land on the unseen ones instead of the full list — not scrolling past
         jobs you have already read is the whole point. */
      el("#newonly").checked = true;
    }
    render();
  }).catch(function (err) {
    el("#freshness").textContent = "could not load data";
    el("#summary").textContent = String(err.message || err);
  });
  setInterval(renderFreshness, 60000);
}

document.addEventListener("DOMContentLoaded", start);
