"""One sweep: pick due companies, fetch serially, diff, notify, exit.

No scheduler lives here — launchd starts the process every 15 minutes and
everything that must carry over (due times, backoff, validators, unfinished
sweeps) is read from and written to the store.
"""

import logging
import time
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
PAGE_DELAY = 0.5           # between requests within a company (±30% jitter)
COMPANY_DELAY = 2.0        # between companies (±30% jitter)
MAX_ENRICH = 10            # detail fetches per company per run
CURSOR_MAX_AGE = 3 * 3600  # an older unfinished sweep restarts from offset 0
BACKOFF_ALERT_AFTER = 2 * 3600


@dataclass
class Result:
    ok: list = field(default_factory=list)          # (company, fetched, new, new_matches)
    partial: list = field(default_factory=list)     # (company, fetched_this_run, new)
    unchanged: list = field(default_factory=list)   # company (HTTP 304)
    failed: list = field(default_factory=list)      # (company, error)
    not_due: list = field(default_factory=list)     # (company, due_at)
    backed_off: list = field(default_factory=list)  # (company, blocked_until, error)
    deferred: list = field(default_factory=list)    # company — out of time this run
    skipped: list = field(default_factory=list)     # (company, note) — unsupported / disabled
    offline: bool = False
    sent: int = 0
    unsent: int = 0
    exported: object = None        # status string from the static export, if enabled

    @property
    def exit_code(self):
        if self.offline:
            return 0
        return 1 if self.failed and not (self.ok or self.partial or self.unchanged) else 0


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
    # Tunable, because the pacing decides how many companies fit in one run:
    # at 2s between companies only ~20 of 49 fit in the 50s budget, so each is
    # polled about every third run. The rest are deferred, least-recently-polled
    # first, so nothing starves — it just lengthens the effective interval.
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

    # Cheap single-request boards first so a long Workday sweep can't starve them,
    # then whoever has waited longest.
    plan.sort(key=lambda x: (x[3].paginated, x[2]["last_polled"] or 0))

    if plan and not online():
        log.info("offline — skipping run (%d companies due)", len(plan))
        res.offline = True
        return res

    deadline = None if seed else monotonic() + budget
    for i, (name, ccfg, st, adapter) in enumerate(plan):
        if deadline is not None and deadline - monotonic() < MIN_COMPANY_BUDGET:
            res.deferred.append(name)
            continue
        if i:
            sleep(jitter(company_delay))
        if _poll_company(name, ccfg, st, adapter, store, res, defaults, seed, deadline, clock, monotonic, online):
            break                                       # went offline mid-run

    if not seed and sink is not None and not res.offline:
        _notify(cfg, store, sink, res, clock)
    if not res.offline and res.exit_code == 0:
        res.exported = _export(cfg, store, clock)
    return res


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
    status = publish_mod.publish_quietly(
        repo, out, files, blob, os.path.join(os.path.dirname(store.path), "export.sha256"),
        branch=settings["branch"], remote=settings["remote"],
        message=f"site: update {time.strftime('%Y-%m-%d %H:%M')}",
    )
    log.info("site: %s", status)
    return status


def _poll_company(name, ccfg, st, adapter, store, res, defaults, seed, deadline, clock, monotonic, online):
    """Returns True if the network vanished and the run should stop."""
    now = int(clock())
    flt = filter_for(ccfg, defaults)
    cursor = st["cursor"]
    sweep_started = st["sweep_started"] if cursor else now
    ctx = FetchContext(cache=dict(st["cache"]), cursor=cursor, deadline=deadline)
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
            _back_off(name, e, store, res, defaults, now)
        else:
            store.poll_failed(name, now, str(e))
        res.failed.append((name, str(e)))
        return False
    except Exception as e:
        store.poll_failed(name, now, f"{e.__class__.__name__}: {e}")
        res.failed.append((name, f"{e.__class__.__name__}: {e}"))
        log.error("%s: FAILED: %s", name, e)
        return False

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
        _back_off(name, result.throttled, store, res, defaults, now, escalate=False)
        res.partial.append((name, len(result.postings), len(fresh)))
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


def _back_off(name, err, store, res, defaults, now, escalate=True):
    code = getattr(err, "code", 0)
    kind = "server" if code >= 500 else "blocked"
    until = store.back_off(name, kind, now, str(err), getattr(err, "retry_after", None), escalate=escalate)
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
