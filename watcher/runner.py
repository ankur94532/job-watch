"""One sweep: pick due companies, fetch them (one host at a time, hosts in parallel), diff, notify, exit.

No scheduler lives here — launchd starts the process every 15 minutes and
everything that must carry over (due times, backoff, validators, unfinished
sweeps) is read from and written to the store.
"""

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

import os

from . import config as config_mod
from . import notify
from .adapters import AdapterError, FetchContext, build_adapter
from .filters import FilterTooLoose, filter_for, sanity_check
from .http import HttpClient, HttpStatusError, NetworkError, NotModified, jitter, network_up
from .store import SENT

log = logging.getLogger(__name__)

RUN_BUDGET = 50            # seconds of fetching per run; the whole run should stay under ~60s
MIN_COMPANY_BUDGET = 6     # don't start a company with less than this left
# Single-request boards (Greenhouse, Lever, Ashby...) may run past the budget,
# up to this much, so every one that is due gets polled every run. They are
# the 15-minute boards and cost one request each; it is the multi-page sweeps
# the budget exists for, and those still pause on time. The ceiling only stops
# a stalled host from holding the run open indefinitely.
OVERRUN = 120
PAGE_DELAY = 0.5           # between requests within a company (±30% jitter)
COMPANY_DELAY = 2.0        # between companies on the same host (±30% jitter)
PARALLEL = 8               # hosts polled at once; 1 = the old strictly serial run
MAX_ENRICH = 10            # detail fetches per company per run
CURSOR_MAX_AGE = 3 * 3600  # an older unfinished sweep restarts from offset 0

# A company is starving when it falls this far behind its own interval. Both
# halves matter and they fail differently:
#   POLL     — never reached. A scheduling bug starved 17 companies for two days
#              and every run still said "ok"; the only visible symptom was the
#              deferred list, which looks like ordinary pacing.
#   COMPLETE — reached, but its sweep never finishes, so closed requisitions are
#              never retired and the phone view keeps linking to dead postings.
#              Walmart sat like this indefinitely while looking healthy.
# Generous multiples, because deferral is normal: these fire on a stuck company,
# not a busy one.
STARVING_POLLS = 4
STARVING_SWEEPS = 8
# HTML adapters: this many polls in a row with zero rows parsed is announced
# loudly. One zero is already a warning; three is a pattern someone must check.
ZERO_ROWS_LOUD = 3
BACKOFF_ALERT_AFTER = 2 * 3600


@dataclass
class Result:
    ok: list = field(default_factory=list)          # (company, fetched, new, new_matches)
    partial: list = field(default_factory=list)     # (company, fetched_this_run, new)
    incremental: list = field(default_factory=list) # (company, newest_rows_read, new, new_matches)
    unchanged: list = field(default_factory=list)   # company (HTTP 304)
    failed: list = field(default_factory=list)      # (company, error)
    not_due: list = field(default_factory=list)     # (company, due_at)
    backed_off: list = field(default_factory=list)  # (company, blocked_until, error)
    deferred: list = field(default_factory=list)    # company — out of time this run
    skipped: list = field(default_factory=list)     # (company, note) — unsupported / disabled
    starving: list = field(default_factory=list)    # (company, age, interval, kind)
    offline: bool = False
    sent: int = 0
    unsent: int = 0
    exported: object = None        # status string from the static export, if enabled

    @property
    def exit_code(self):
        if self.offline:
            return 0
        return 1 if self.failed and not (self.ok or self.partial or self.unchanged or self.incremental) else 0


def _check_starvation(cfg, store, res, now):
    """Say so, loudly, when a company has fallen far behind its own interval.

    This exists because the starvation bug was invisible: `run` reported `ok`
    every time while a third of the boards went unpolled for two days. Nothing
    here changes what is polled — it only makes the condition visible in the
    launchd log instead of waiting for someone to notice a dead link.
    """
    active, _ = config_mod.active_companies(cfg)
    defaults = cfg.get("defaults") or {}
    for name, ccfg in active:
        st = store.state(name)
        if st["blocked_until"] and st["blocked_until"] > now:
            continue                       # deliberately paused, not starving
        interval = config_mod.interval_for(ccfg, defaults)
        polled, complete = st["last_polled"], st["last_complete"]
        if polled and now - polled > interval * STARVING_POLLS:
            res.starving.append((name, now - polled, interval, "poll"))
            log.warning("%s has not been polled in %.1fh (%.0fx its %s interval)",
                        name, (now - polled) / 3600.0,
                        (now - polled) / interval, _fmt_interval(interval))
        elif complete and now - complete > sweep_threshold(ccfg, defaults):
            # Polled but never finishing: closed reqs are never retired.
            res.starving.append((name, now - complete, interval, "sweep"))
            log.warning("%s is polled but has not COMPLETED a sweep in %.1fh "
                        "(%.0fx its interval) — closed reqs cannot be retired",
                        name, (now - complete) / 3600.0, (now - complete) / interval)


def sweep_threshold(ccfg, defaults):
    """How old the last COMPLETE sweep may get before it counts as starving.

    Normally STARVING_SWEEPS intervals. An incremental board completes a sweep
    only every `full_sweep` seconds by design, so its allowance starts there.
    """
    interval = config_mod.interval_for(ccfg, defaults)
    expected = interval
    if ccfg.get("incremental"):
        from .adapters.workday import FULL_SWEEP
        expected = int(ccfg.get("full_sweep") or FULL_SWEEP)
    return expected + (STARVING_SWEEPS - 1) * interval


def _fmt_interval(seconds):
    return f"{seconds // 60}m" if seconds < 3600 else f"{seconds / 3600:.0f}h"


def _due(st, interval, now):
    if st["cursor"]:
        return True, now
    if st["last_polled"] is None:
        return True, now
    due_at = st["last_polled"] + interval - config_mod.DUE_SLACK
    return now >= due_at, due_at


def run(cfg, store, sink=None, seed=False, only=None, http=None, budget=RUN_BUDGET,
        page_delay=PAGE_DELAY, company_delay=COMPANY_DELAY, clock=time.time,
        monotonic=time.monotonic, sleep=time.sleep, online=network_up):
    """`seed` fetches everything configured (ignores due times and the time budget) and alerts nothing."""
    http = http or HttpClient(online=online)
    active, skipped = config_mod.active_companies(cfg, only)
    defaults = cfg.get("defaults") or {}
    # Tunable, because the pacing decides how many companies fit in one run.
    # Pacing holds per host (see _Dispatcher). The budget bounds multi-page
    # sweeps; single-request boards may overrun it (OVERRUN) so the busiest
    # host, ~22 Greenhouse boards at ~2-3s each, finishes every run. Anything
    # that still does not fit is deferred and goes early next run.
    budget = float(defaults.get("run_budget") or budget)
    if budget <= MIN_COMPANY_BUDGET:
        # Below this nothing would ever start: every company defers immediately.
        log.warning("run_budget %.0fs is too small to poll anything; using %ds",
                    budget, MIN_COMPANY_BUDGET * 2)
        budget = MIN_COMPANY_BUDGET * 2
    company_delay = float(defaults.get("company_delay") or company_delay)
    page_delay = float(defaults.get("page_delay") or page_delay)
    res = Result(skipped=skipped)
    now = int(clock())

    plan = []
    for name, ccfg in active:
        st = store.state(name)
        if st["blocked_until"] and st["blocked_until"] > now:
            res.backed_off.append((name, st["blocked_until"], st["last_error"] or ""))
            continue
        if st["cursor"] and st["sweep_started"] and now - st["sweep_started"] > CURSOR_MAX_AGE:
            log.info("%s: unfinished sweep is stale, restarting it", name)
            store.drop_cursor(name)
            st = store.state(name)
        due, due_at = _due(st, config_mod.interval_for(ccfg, defaults), now)
        if not due and not seed:
            res.not_due.append((name, due_at))
            continue
        try:
            adapter = build_adapter(name, ccfg, http=http,
                                    page_delay=float(ccfg.get("page_delay") or page_delay))
            sanity_check(filter_for(ccfg, defaults), name)   # never poll behind an open filter
        except (AdapterError, FilterTooLoose) as e:
            res.failed.append((name, str(e)))
            log.error("%s: %s", name, e)
            continue
        plan.append((name, ccfg, st, adapter))

    # Longest-waiting first, always. This used to sort on `paginated` FIRST, to
    # stop a long Workday sweep starving the cheap single-request boards. It did
    # the opposite: every paginated board sat permanently behind every cheap one,
    # the ~50 cheap boards ate the whole 50s budget, and the Workday tenants were
    # never reached at all. Seventeen companies went unpolled for two days, and
    # their exports kept advertising requisitions that had long since closed.
    #
    # Ageing is what prevents starvation: whoever has waited longest goes first,
    # so a company that misses a run moves up the order until it cannot miss
    # another. Cost is only a tie-break, between companies that are equally
    # stale. A big sweep can still take most of a run, but it yields at the
    # deadline with a cursor, and it is no longer the stalest next time.
    plan.sort(key=lambda x: (x[2]["last_polled"] or 0, x[3].paginated))

    if plan and not online():
        log.info("offline — skipping run (%d companies due)", len(plan))
        res.offline = True
        return res

    parallel = max(1, int(defaults.get("parallel") or PARALLEL))
    deadline = None if seed else monotonic() + budget
    overrun = float(defaults.get("overrun", OVERRUN))
    hard_deadline = None if seed else deadline + overrun
    queue = _Dispatcher(plan, deadline, hard_deadline, monotonic)

    def worker():
        while True:
            got = queue.take()
            if got is None:
                return
            (name, ccfg, st, adapter), lane, lane_used = got
            try:
                if lane_used:                       # same host as a poll just now: be polite
                    sleep(jitter(company_delay))
                if _poll_company(name, ccfg, st, adapter, store, res, defaults, seed,
                                 queue.deadline_for(adapter), clock, monotonic, online):
                    queue.halt()                    # went offline mid-run
            except Exception as e:                  # a thread must never die silently
                log.exception("%s: poll crashed", name)
                res.failed.append((name, f"{e.__class__.__name__}: {e}"))
            finally:
                queue.done(lane, adapter)

    threads = min(parallel, queue.lanes())
    if threads <= 1:
        worker()                                    # serial: no threads, same code path
    else:
        pool = [threading.Thread(target=worker, name=f"poll-{i}", daemon=True) for i in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
    if not res.offline:
        res.deferred.extend(name for name, *_ in queue.pending)

    if not res.offline:
        _check_starvation(cfg, store, res, clock())

    if not seed and sink is not None and not res.offline:
        _notify(cfg, store, sink, res, clock)
    if not res.offline and res.exit_code == 0:
        res.exported = _export(cfg, store, clock)
    return res


class _Dispatcher:
    """Hands the plan to worker threads in priority order, one company per lane.

    A worker takes the first company whose lane (host) is idle, so a busy host
    never holds up the rest, and a company is only ever skipped over for one
    that is further down the list AND on a free host. The priority order still
    decides who goes first on every host.

    Two deadlines: multi-page sweeps stop at the run budget, single-request
    boards carry on to `hard_deadline` (see OVERRUN).
    """

    def __init__(self, plan, deadline, hard_deadline, monotonic):
        self.pending = list(plan)
        self.deadline = deadline
        self.hard_deadline = hard_deadline
        self.monotonic = monotonic
        self.busy = set()                 # lanes with a poll in flight
        self.used = set()                 # lanes polled already this run
        self.in_flight = Counter()        # ats -> polls in flight, for max_parallel
        self.stopped = False
        self.cond = threading.Condition()

    def lanes(self):
        return len({a.lane() for *_, a in self.pending})

    def deadline_for(self, adapter):
        return self.deadline if adapter.paginated else self.hard_deadline

    def _out_of_time(self, adapter):
        dl = self.deadline_for(adapter)
        return dl is not None and dl - self.monotonic() < MIN_COMPANY_BUDGET

    def take(self):
        """The next company to poll, or None when the run is over for this worker."""
        with self.cond:
            while not self.stopped and self.pending:
                if all(self._out_of_time(item[3]) for item in self.pending):
                    break
                for i, item in enumerate(self.pending):
                    adapter = item[3]
                    if self._out_of_time(adapter):
                        continue
                    lane = adapter.lane()
                    if lane in self.busy:
                        continue
                    if adapter.max_parallel and self.in_flight[adapter.ats] >= adapter.max_parallel:
                        continue
                    del self.pending[i]
                    self.busy.add(lane)
                    self.in_flight[adapter.ats] += 1
                    used = lane in self.used
                    self.used.add(lane)
                    return item, lane, used
                # Everything left is on a host that is busy right now. The timeout
                # re-checks the deadline even if no poll finishes to wake us.
                self.cond.wait(0.5)
            return None

    def done(self, lane, adapter):
        with self.cond:
            self.busy.discard(lane)
            self.in_flight[adapter.ats] -= 1
            self.cond.notify_all()

    def halt(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()


def _export(cfg, store, clock):
    """Static export + publish. Never fails the run: the poll already succeeded."""
    settings = config_mod.export_settings(cfg)
    if not settings.get("enabled"):
        return None
    from . import export as export_mod
    from . import publish as publish_mod

    try:
        out = os.path.abspath(os.path.expanduser(settings["out"]))
        files, blob = export_mod.write(cfg, store, out, now=int(clock()))
    except Exception as e:                      # a broken export must not sink a good run
        log.warning("export failed: %s", e)
        return f"export failed: {e}"
    if not settings.get("publish"):
        return f"exported to {out}"
    repo = os.path.abspath(settings.get("repo") or os.path.dirname(out) or ".")
    status = publish_mod.publish_export(
        repo, out, files, blob, os.path.dirname(store.path), settings,
        message=f"site: update {time.strftime('%Y-%m-%d %H:%M')}")
    log.info("site: %s", status)
    return status


def _poll_company(name, ccfg, st, adapter, store, res, defaults, seed, deadline, clock, monotonic, online):
    """Returns True if the network vanished and the run should stop."""
    now = int(clock())
    flt = filter_for(ccfg, defaults)
    cursor = st["cursor"]
    sweep_started = st["sweep_started"] if cursor else now
    ctx = FetchContext(cache=dict(st["cache"]), cursor=cursor, deadline=deadline,
                       known=store.known_ids(name) if getattr(adapter, "incremental", False) else None,
                       last_complete=st["last_complete"], now=now)
    try:
        result = adapter.fetch(ctx)
    except NotModified:
        store.touch_unchanged(name, now)
        res.unchanged.append(name)
        log.info("%s: unchanged (304)", name)
        return False
    except NetworkError as e:
        if not online():
            log.info("offline — stopping run at %s", name)
            res.offline = True
            return True
        store.poll_failed(name, now, str(e))
        res.failed.append((name, str(e)))
        log.warning("%s: network error: %s", name, e)
        return False
    except HttpStatusError as e:
        if e.code in (403, 429) or e.code >= 500:
            _back_off(name, e, store, res, defaults, now, ccfg=ccfg)
        else:
            store.poll_failed(name, now, str(e))
        res.failed.append((name, str(e)))
        return False
    except Exception as e:
        store.poll_failed(name, now, f"{e.__class__.__name__}: {e}")
        res.failed.append((name, f"{e.__class__.__name__}: {e}"))
        log.error("%s: FAILED: %s", name, e)
        return False

    html_problem = _html_checks(name, result, st, store) if getattr(adapter, "html", False) else None

    # Never alert on a board we've never completed a sweep of: that's its whole backlog.
    silent = seed or st["last_complete"] is None
    if silent and not seed:
        log.info("%s: not seeded yet, recording silently", name)
    if not silent:
        known = store.known_ids(name)
        enriched = 0
        for p in result.postings:
            if enriched >= MAX_ENRICH or (deadline is not None and monotonic() >= deadline):
                break
            if p.job_id not in known and flt.title_ok(p.title) and adapter.needs_enrich(p):
                enriched += 1
                try:
                    adapter.enrich(p)
                except Exception as e:
                    log.warning("%s: detail fetch for %s failed: %s", name, p.job_id, e)

    def is_match(p):
        # A vague location we couldn't resolve ("3 Locations") counts as a match: better a
        # stray alert than a silently dropped India req.
        return flt.title_ok(p.title) and (flt.location_ok(p.location) or adapter.needs_enrich(p))

    fresh = store.record(name, result.postings, now, is_match, suppress=silent)
    matched = 0 if silent else sum(1 for p in fresh if is_match(p))

    # Everything matching is possible on a tiny, tightly-targeted board, but on
    # anything else it means the filter stopped filtering. Say so out loud.
    if len(result.postings) >= 5 and all(is_match(p) for p in result.postings):
        log.warning("%s: ALL %d rows match the filters — verify config; a filter that matches "
                    "everything looks identical to one that works", name, len(result.postings))

    # A board we can only see one page of (Google: robots forbids pagination) has
    # no way to show us what fell off the end. If the whole window is new, it
    # probably overflowed between polls.
    if getattr(adapter, "window_limited", False) and not silent and result.postings \
            and len(fresh) == len(result.postings):
        log.warning("%s: every one of the %d rows in the visible window is new — the window "
                    "likely overflowed since the last poll and postings were missed. "
                    "Consider a shorter interval or a narrower query.", name, len(result.postings))
    if result.throttled is not None:
        # Partial sweep cut short by 403/429: the rows are kept, the cursor is kept,
        # and the company is parked exactly as if the fetch had failed outright.
        store.sweep_progress(name, now, sweep_started, result.cursor)
        # Progress was made, so park for the base wait without escalating.
        _back_off(name, result.throttled, store, res, defaults, now, escalate=False, ccfg=ccfg)
        res.partial.append((name, len(result.postings), len(fresh)))
        return False
    if html_problem:
        # Rows that were parsed are kept, but the parse is not trusted as the
        # whole board: nothing is retired, and the poll shows as failed.
        store.poll_failed(name, now, html_problem)
        res.failed.append((name, html_problem))
        return False
    if result.incremental:
        store.poll_incremental(name, now)
        res.incremental.append((name, len(result.postings), len(fresh), matched))
        log.info("%s: incremental, read the %d newest rows, %d new, %d new matches",
                 name, len(result.postings), len(fresh), matched)
        return False
    if result.complete:
        count = len(result.postings) + (len(cursor.get("ids", [])) if cursor else 0)
        store.sweep_complete(name, now, sweep_started, count, ctx.cache)
        res.ok.append((name, count, len(fresh), matched))
        log.info("%s: %d open, %d new, %d new matches", name, count, len(fresh), matched)
    else:
        store.sweep_progress(name, now, sweep_started, result.cursor)
        res.partial.append((name, len(result.postings), len(fresh)))
        # Adapters name their position differently (offset / start), so show whichever.
        where = next((result.cursor[k] for k in ("offset", "start") if result.cursor.get(k) is not None), "?")
        log.info("%s: sweep paused at %s (%d fetched this run), resumes next run",
                 name, where, len(result.postings))
    return False


def _html_checks(name, result, st, store):
    """None if an HTML adapter's result can stand as a complete sweep, else why not.

    The failure these exist for is not a crash: it is a selector that quietly
    matches nothing, so every poll reports "no new postings" and it looks like
    a quiet week. So zero rows is never a normal result here, the count is
    logged every run so a slide (40 -> 3) is visible, and a count the page
    states itself is held against what was parsed.
    """
    n, declared = len(result.postings), result.declared
    log.info("%s: HTML adapter parsed %d rows%s", name, n,
             "" if declared is None else f" (page declares {declared})")
    if n == 0:
        streak = store.zero_rows(name)
        confirmed = bool(result.empty_evidence) and not declared
        why = (f'page confirms no openings: "{result.empty_evidence}"' if confirmed else
               "NO empty-state text on the page, so the selector has probably stopped matching")
        log.warning("%s: 0 rows parsed; %s", name, why)
        if streak >= ZERO_ROWS_LOUD:
            log.warning("HTML adapter %s returned 0 rows for %d runs — verify manually (%s)",
                        name, streak, why)
        return None if confirmed else f"0 rows parsed and {why}"
    store.rows_seen(name)
    if declared is not None and n != declared:
        why = f"parsed {n} rows but the page declares {declared}"
        log.warning("%s: %s; some rows are not being matched, so nothing is retired", name, why)
        return why
    prev = st["last_count"]
    if prev and prev >= 6 and n < prev / 2:
        log.warning("%s: row count fell from %d to %d; the page agrees, but check it is a real drop",
                    name, prev, n)
    return None


def _back_off(name, err, store, res, defaults, now, escalate=True, ccfg=None):
    code = getattr(err, "code", 0)
    kind = "server" if code >= 500 else "blocked"
    # `backoff_min` (seconds): a board whose quota refills slowly gets a longer
    # first pause. Microsoft's does: retrying it every 30 minutes spent each new
    # allowance on a few requests and was throttled again, most of a day.
    until = store.back_off(name, kind, now, str(err), getattr(err, "retry_after", None), escalate=escalate,
                           min_wait=int((ccfg or {}).get("backoff_min") or 0))
    st = store.state(name)
    alert_after = int(defaults.get("backoff_alert_after") or BACKOFF_ALERT_AFTER)
    if until - (st["fail_since"] or now) >= alert_after and not st["alert_state"]:
        store.flag_alert(name)
    res.backed_off.append((name, until, str(err)))
    log.warning("%s: %s — backing off until %s", name, err,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(until)))


def _notify(cfg, store, sink, res, clock):
    now = int(clock())
    defaults = cfg.get("defaults") or {}
    filters = {}
    alerts = []
    for row in store.pending():
        a = dict(row)
        ccfg = cfg["companies"].get(a["company"]) or {}
        flt = filters.setdefault(a["company"], filter_for(ccfg, defaults))
        a["score"] = flt.score(a["title"], a["location"])
        alerts.append(a)

    messages = notify.compose(
        sink, alerts, now=now,
        digest_threshold=int(defaults.get("digest_threshold") or notify.DIGEST_THRESHOLD),
    )
    for text, keys in messages:
        if sink.send(text):
            store.mark(keys, SENT)
            res.sent += len(keys)
        else:
            log.warning("notification not delivered; %d matches stay pending for next run", len(alerts) - res.sent)
            res.unsent = len(alerts) - res.sent
            break

    flagged = store.pending_alerts()
    for text, companies in notify.compose_backoff(sink, flagged, now=now):
        if not sink.send(text):
            break
        for c in companies:
            store.alert_sent(c)
