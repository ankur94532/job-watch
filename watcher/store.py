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

import json
import logging
import os
import sqlite3
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
  alert_state INTEGER DEFAULT 0
);
"""


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
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        _retry_locked(lambda: self.db.execute("PRAGMA journal_mode=WAL"))
        self.db.execute("PRAGMA busy_timeout=10000")
        _retry_locked(lambda: self.db.executescript(SCHEMA))

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
                self._exec(
                    "UPDATE seen SET last_seen = ?, title = ?, url = ? WHERE company = ? AND job_id = ?",
                    (now, p.title, p.url, company, p.job_id),
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

    def sweep_progress(self, company, now, sweep_started, cursor):
        self._set(company, last_polled=now, sweep_started=sweep_started, cursor=cursor, last_error=None)
        self.reset_backoff(company)

    def sweep_complete(self, company, now, sweep_started, count, cache):
        self._set(
            company, last_polled=now, last_complete=sweep_started, last_count=count,
            sweep_started=None, cursor=None, cache=cache or None, last_error=None,
        )
        self.reset_backoff(company)

    def drop_cursor(self, company):
        self._set(company, sweep_started=None, cursor=None)

    def poll_failed(self, company, now, error):
        """A non-throttling failure (4xx config error, bad JSON). Wait the normal interval."""
        self._set(company, last_polled=now, last_error=error[:500])

    def back_off(self, company, kind, now, error, retry_after=None, escalate=True):
        """Persist exponential backoff; returns blocked_until.

        `escalate=False` parks the company for the base wait without advancing the
        level: used when the sweep made real progress before being throttled, so a
        board that always throttles part-way does not climb to a 24-hour ceiling
        while it is in fact working.
        """
        st = self.state(company)
        first, ceiling = BACKOFF[kind]
        level = (st["backoff_level"] or 0) if (st["backoff_kind"] == kind and escalate) else 0
        wait = min(first * (2**level), ceiling)
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


def now_ts():
    return int(time.time())
