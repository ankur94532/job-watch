"""SQLite store: dedup, first_seen / last_seen, closed reqs, per-company poll state.

Everything that has to survive launchd restarting the process every 15
minutes lives here: when each company was last polled, its HTTP validators,
an unfinished sweep's cursor, and its backoff.

`seen.notified`:
  0  pending    — new match, alert not yet delivered (retried next run)
  1  sent       — delivered, individually or as part of a digest
  2  suppressed — seeded, or didn't match filters when first seen

A requisition is *closed* when its last_seen is older than the start of the
company's most recent complete sweep.
"""

import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

PENDING, SENT, SUPPRESSED = 0, 1, 2
ALERT_NONE, ALERT_PENDING, ALERT_SENT = 0, 1, 2

DEFAULT_DB = "~/.local/share/jobswatch/jobs.db"

BACKOFF = {
    # kind: (first wait, ceiling), doubling in between
    "blocked": (30 * 60, 24 * 3600),   # 403 / 429
    "server": (30 * 60, 3600),         # 5xx that survived in-run retries
}
MAX_RETRY_AFTER = 7 * 24 * 3600
COMPACT_LOCATION = re.compile(r"^(?:\d+\s+Locations?|Multiple(?:\s+Locations?)?)(?:\s*\||$)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  company TEXT, job_id TEXT, title TEXT, location TEXT, url TEXT,
  first_seen INTEGER, last_seen INTEGER, notified INTEGER DEFAULT 0,
  req_id TEXT, posted_at TEXT, ats TEXT,
  PRIMARY KEY (company, job_id)
);
CREATE INDEX IF NOT EXISTS seen_notified ON seen (notified);
CREATE TABLE IF NOT EXISTS company_state (
  company TEXT PRIMARY KEY,
  last_polled INTEGER,        -- last attempt that reached the server (success or HTTP error)
  last_complete INTEGER,      -- start time of the last complete sweep; NULL = never seeded
  last_count INTEGER,
  sweep_started INTEGER,      -- start of the sweep `cursor` belongs to
  cursor TEXT,                -- JSON resume point of an unfinished sweep
  cache TEXT,                 -- JSON HTTP validators
  backoff_kind TEXT,
  backoff_level INTEGER DEFAULT 0,
  fail_since INTEGER,
  blocked_until INTEGER,
  last_error TEXT,
  alert_state INTEGER DEFAULT 0,
  zero_runs INTEGER DEFAULT 0 -- consecutive polls that parsed no rows (HTML adapters)
);
CREATE TABLE IF NOT EXISTS recheck (   -- `watcher recheck`: last fingerprint per URL
  url TEXT PRIMARY KEY,
  company TEXT,
  fingerprint TEXT,
  checked INTEGER
);
"""

# Columns added after databases already existed: CREATE TABLE IF NOT EXISTS
# leaves an existing table exactly as it was, so these are added on open.
ADDED_COLUMNS = {
    "company_state": [("zero_runs", "INTEGER DEFAULT 0")],
}


def db_path(env=None):
    env = os.environ if env is None else env
    return os.path.expanduser(env.get("JOBSWATCH_DB") or DEFAULT_DB)


def _retry_locked(fn, attempts=5, sleep=time.sleep):
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" not in msg and "busy" not in msg) or i == attempts - 1:
                raise
            wait = 0.5 * (2**i)
            log.warning("sqlite %s, retrying in %.1fs", e, wait)
            sleep(wait)


class Store:
    def __init__(self, path=None):
        path = path or db_path()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        # The runner polls different hosts from worker threads, all sharing this
        # one connection. Every method below runs under this lock (see the end of
        # the module), so one company's writes never interleave with another's
        # inside a transaction, and a commit never lands someone else's half-done
        # record().
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        _retry_locked(lambda: self.db.execute("PRAGMA journal_mode=WAL"))
        self.db.execute("PRAGMA busy_timeout=10000")
        _retry_locked(lambda: self.db.executescript(SCHEMA))
        _retry_locked(self._migrate)

    def _migrate(self):
        for table, columns in ADDED_COLUMNS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.db.commit()

    def close(self):
        self.db.close()

    def _exec(self, sql, args=()):
        return _retry_locked(lambda: self.db.execute(sql, args))

    def commit(self):
        _retry_locked(self.db.commit)

    # ---- postings -----------------------------------------------------------

    def known_ids(self, company):
        return {r[0] for r in self._exec("SELECT job_id FROM seen WHERE company = ?", (company,))}

    def record(self, company, postings, now, is_match, suppress=False):
        """Upsert postings from one fetch; return the ones never seen before.

        New matches go in PENDING unless `suppress` (seeding); everything else
        SUPPRESSED. Diffing is against stored ids only — never posted dates — so
        a poll 11 hours after the last one reports everything in between and
        nothing twice.
        """
        known = self.known_ids(company)
        fresh = []
        for p in postings:
            if p.job_id in known:
                location = p.location
                # Compact Workday rows omit the offices resolved by a previous
                # detail read. Preserve those offices, but retain an explicit
                # country guarantee from the current query (e.g. India facet).
                if COMPACT_LOCATION.match(location or ""):
                    previous = self._exec("SELECT location FROM seen WHERE company = ? AND job_id = ?",
                                          (company, p.job_id)).fetchone()[0]
                    if previous and not COMPACT_LOCATION.match(previous):
                        location = previous
                        hint = p.raw.get("_location_hint")
                        if hint and not re.search(r"(?<!\w)" + re.escape(hint) + r"(?!\w)", location, re.I):
                            location += " | " + hint
                self._exec(
                    "UPDATE seen SET last_seen = ?, title = ?, url = ?, location = ? WHERE company = ? AND job_id = ?",
                    (now, p.title, p.url, location, company, p.job_id),
                )
                continue
            state = PENDING if (not suppress and is_match(p)) else SUPPRESSED
            self._exec(
                "INSERT INTO seen (company, job_id, title, location, url, first_seen, last_seen,"
                " notified, req_id, posted_at, ats) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (company, p.job_id, p.title, p.location, p.url, now, now, state, p.req_id, p.posted_at, p.ats),
            )
            known.add(p.job_id)
            fresh.append(p)
        self.commit()
        return fresh

    def touch_unchanged(self, company, now):
        """304: everything open at the last complete sweep is still open."""
        st = self.state(company)
        if st["last_complete"] is not None:
            self._exec(
                "UPDATE seen SET last_seen = ? WHERE company = ? AND last_seen >= ?",
                (now, company, st["last_complete"]),
            )
        self._set(company, last_polled=now, last_complete=now if st["last_complete"] is not None else None)
        self.reset_backoff(company)

    def pending(self):
        return self._exec("SELECT * FROM seen WHERE notified = ? ORDER BY company, first_seen", (PENDING,)).fetchall()

    def mark(self, keys, state):
        for company, job_id in keys:
            self._exec("UPDATE seen SET notified = ? WHERE company = ? AND job_id = ?", (state, company, job_id))
        self.commit()

    def rows(self, company=None, since=None, closed=False):
        where, args = [], []
        if company:
            where.append("s.company = ?")
            args.append(company)
        if since is not None:
            where.append("s.first_seen >= ?")
            args.append(since)
        if closed:
            where.append("s.last_seen < c.last_complete")
        sql = "SELECT s.*, c.last_complete FROM seen s LEFT JOIN company_state c ON c.company = s.company"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._exec(sql + " ORDER BY s.first_seen DESC, s.company", args).fetchall()

    # ---- per-company poll state --------------------------------------------

    def state(self, company):
        row = self._exec("SELECT * FROM company_state WHERE company = ?", (company,)).fetchone()
        st = dict(row) if row else {"company": company, "backoff_level": 0, "alert_state": 0}
        for k in ("last_polled", "last_complete", "last_count", "sweep_started", "backoff_kind",
                  "fail_since", "blocked_until", "last_error"):
            st.setdefault(k, None)
        st["zero_runs"] = st.get("zero_runs") or 0
        st["cursor"] = json.loads(st["cursor"]) if st.get("cursor") else None
        st["cache"] = json.loads(st["cache"]) if st.get("cache") else {}
        return st

    def all_states(self):
        return {r["company"]: self.state(r["company"]) for r in self._exec("SELECT company FROM company_state")}

    def _set(self, company, **fields):
        for k in ("cursor", "cache"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        self._exec("INSERT OR IGNORE INTO company_state (company) VALUES (?)", (company,))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE company_state SET {cols} WHERE company = ?", (*fields.values(), company))
        self.commit()

    def clear_cache(self, companies=None):
        """Forget stored ETags so the next fetch gets a 200 body, not a 304.

        Needed after an adapter changes how it derives a field: a conditional
        GET that answers 304 skips the rows entirely, so the new code never
        runs and the stored rows keep the old value. Only the validators are
        dropped -- postings, first_seen and backoff are untouched, so nothing
        is re-alerted.
        """
        if companies:
            marks = ", ".join("?" for _ in companies)
            cur = self._exec(f"UPDATE company_state SET cache = NULL WHERE company IN ({marks})",
                             tuple(companies))
        else:
            cur = self._exec("UPDATE company_state SET cache = NULL")
        self.commit()
        return cur.rowcount

    def sweep_progress(self, company, now, sweep_started, cursor, clear_backoff=True):
        self._set(company, last_polled=now, sweep_started=sweep_started, cursor=cursor, last_error=None)
        if clear_backoff:
            self.reset_backoff(company)

    def sweep_complete(self, company, now, sweep_started, count, cache):
        self._set(
            company, last_polled=now, last_complete=sweep_started, last_count=count,
            sweep_started=None, cursor=None, cache=cache or None, last_error=None,
        )
        self.reset_backoff(company)

    def zero_rows(self, company):
        """One more consecutive poll that parsed nothing. Returns the new streak."""
        n = (self.state(company)["zero_runs"] or 0) + 1
        self._set(company, zero_runs=n)
        return n

    def rows_seen(self, company):
        """Rows parsed again: the zero streak is over."""
        if self.state(company)["zero_runs"]:
            self._set(company, zero_runs=0)

    def poll_incremental(self, company, now):
        """An incremental poll reached the board: it is polled, not swept."""
        self._set(company, last_polled=now, last_error=None)
        self.reset_backoff(company)

    def recheck_get(self, url):
        row = self._exec("SELECT fingerprint FROM recheck WHERE url = ?", (url,)).fetchone()
        return json.loads(row[0]) if row else None

    def recheck_put(self, url, company, fingerprint, now):
        self._exec("INSERT OR REPLACE INTO recheck (url, company, fingerprint, checked) VALUES (?,?,?,?)",
                   (url, company, json.dumps(fingerprint, sort_keys=True), now))
        self.commit()

    def drop_cursor(self, company):
        self._set(company, sweep_started=None, cursor=None)

    def poll_failed(self, company, now, error):
        """A non-throttling failure (4xx config error, bad JSON). Wait the normal interval."""
        self._set(company, last_polled=now, last_error=error[:500])

    def back_off(self, company, kind, now, error, retry_after=None, escalate=True, min_wait=0):
        """Persist exponential backoff; returns blocked_until.

        `escalate=False` parks the company for the base wait without advancing the
        level: used when the sweep made real progress before being throttled, so a
        board that always throttles part-way does not climb to a 24-hour ceiling
        while it is in fact working.
        """
        st = self.state(company)
        first, ceiling = BACKOFF[kind]
        level = (st["backoff_level"] or 0) if (st["backoff_kind"] == kind and escalate) else 0
        # A per-company floor is the starting delay, so consecutive failures
        # double it too (1h, 2h, 4h), rather than repeating the first 1h wait.
        first = max(first, min_wait or 0)
        wait = min(first * (2**level), max(ceiling, first))
        if retry_after:
            wait = max(wait, min(int(retry_after), MAX_RETRY_AFTER))
        until = now + wait
        self._set(
            company, last_polled=now, backoff_kind=kind, backoff_level=level + 1 if escalate else 0,
            fail_since=st["fail_since"] or now, blocked_until=until, last_error=error[:500],
        )
        return until

    def flag_alert(self, company):
        self._set(company, alert_state=ALERT_PENDING)

    def pending_alerts(self):
        return [self.state(r["company"]) for r in
                self._exec("SELECT company FROM company_state WHERE alert_state = ?", (ALERT_PENDING,))]

    def alert_sent(self, company):
        self._set(company, alert_state=ALERT_SENT)

    def reset_backoff(self, company):
        st = self._exec(
            "SELECT backoff_level, alert_state, blocked_until, fail_since, backoff_kind"
            " FROM company_state WHERE company = ?", (company,)).fetchone()
        # `blocked_until` is checked too: a park that did not escalate leaves the
        # level at 0, and only clearing on level would strand the timestamp.
        if st and any(st[k] for k in ("backoff_level", "alert_state", "blocked_until",
                                      "fail_since", "backoff_kind")):
            log.info("%s: backoff cleared", company)
            self._set(company, backoff_kind=None, backoff_level=0, fail_since=None,
                      blocked_until=None, alert_state=ALERT_NONE)


def _serialised(fn):
    @functools.wraps(fn)
    def locked(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return locked


for _name, _fn in list(vars(Store).items()):
    if callable(_fn) and not _name.startswith("__"):
        setattr(Store, _name, _serialised(_fn))


def now_ts():
    return int(time.time())
