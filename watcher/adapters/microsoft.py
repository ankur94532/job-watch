"""Microsoft's official latest-jobs page plus a bounded Eightfold sweep.

The full India Eightfold query is too large for the host's observed allowance:
it needs about 24 ten-row requests while the host currently permits one or two
before 429. Microsoft's official ``Software Engineering`` profession filter reduces
that closure inventory to about nine pages. Between those slower complete
sweeps, the server-rendered India careers page exposes the three newest roles in
one request. Its cadence and backoff are independent, so new-role alerts do not
wait for a sweep to finish or for the closure API's cooldown.
"""

import logging
import re
import time
from urllib.parse import quote

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting
from .eightfold import EightfoldAdapter
from .embedded import RobotsAware
from .html import text

log = logging.getLogger(__name__)


class MicrosoftAdapter(RobotsAware, EightfoldAdapter):
    # The cheap latest-jobs page must not consume the one Eightfold tenant slot.
    # Closure API requests remain bounded and internally paced below.
    ats = "microsoft"
    window_limited = True
    LATEST = "https://careers.microsoft.com/professionals/us/en/l-india"

    TITLE = re.compile(r'<h3\b[^>]*class="[^"]*careers-joblistResponsive-subheading[^"]*"[^>]*>(.*?)</h3>', re.S)
    DATE = re.compile(r'<div\b[^>]*class="[^"]*careers-joblistResponsive-postdate[^"]*"[^>]*>(.*?)</div>', re.S)
    LOCATION = re.compile(r'<div\b[^>]*class="[^"]*careers-joblistResponsive-primarylocation[^"]*"[^>]*>(.*?)</div>', re.S)
    WORKSITE = re.compile(r'<div\b[^>]*class="[^"]*careers-joblistResponsive-worksiteflex[^"]*"[^>]*>(.*?)</div>', re.S)
    LINK = re.compile(r'<a\b[^>]*href="https://apply\.careers\.microsoft\.com/careers/job/(\d+)\?hl=en"[^>]*class="[^"]*careers-joblistResponsive-button[^"]*"', re.S)
    COUNT = re.compile(r'<div\b[^>]*id="openings"[^>]*>\s*(\d+)\s*</div>', re.S)

    @property
    def profession(self):
        value = (self.cfg.get("profession") or "").strip()
        if not value:
            raise AdapterError(f"{self.company}: microsoft profession is required")
        return value

    @property
    def incremental(self):
        return True

    def cursor_scope(self):
        return {"profession": self.profession, "locations": self.locations()}

    def closure_resume_interval(self):
        return int(self.cfg.get("closure_resume_interval") or 3600)

    def _fresh_cursor(self):
        return {
            "loc": 0, "start": 0, "ids": [], "loc_ids": [], "total": None,
            "scope": self.cursor_scope(),
        }

    def _defer_closure(self, cursor, now, throttled=False, progressed=False,
                       retry_after=None):
        """Persist API pacing inside the cursor without delaying latest-role polls."""
        cur = dict(cursor or self._fresh_cursor())
        base = self.closure_resume_interval()
        previous = int((cursor or {}).get("api_backoff_level") or 0)
        if throttled and not progressed:
            wait = min(base * (2 ** min(previous, 10)), max(4 * base, 24 * 3600))
            level = min(previous + 1, 10)
        else:
            wait, level = base, 0
        if retry_after:
            wait = max(wait, min(int(retry_after), 7 * 86400))
        cur["api_after"] = now + wait
        cur["api_backoff_level"] = level
        return cur, wait

    def url(self, location, start):
        return (super().url(location, start)
                + "&filter_profession=" + quote(self.profession))

    def fetch_incremental(self, ctx, requests=0):
        self.check_robots(self.LATEST)
        markup = self.http.get_text(self.LATEST)
        titles = list(map(text, self.TITLE.findall(markup)))
        dates = list(map(text, self.DATE.findall(markup)))
        locations = list(map(text, self.LOCATION.findall(markup)))
        worksites = list(map(text, self.WORKSITE.findall(markup)))
        ids = self.LINK.findall(markup)
        counts = self.COUNT.findall(markup)
        sizes = {len(titles), len(dates), len(locations), len(worksites), len(ids)}
        if sizes != {3} or len(set(ids)) != 3 or len(counts) != 1 or int(counts[0]) < 3:
            raise AdapterError(
                f"{self.company}: Microsoft latest page is incomplete or its layout changed")
        out = []
        for job_id, title, posted, location, worksite in zip(
                ids, titles, dates, locations, worksites):
            if not title or not posted or not location or "india" not in location.casefold():
                raise AdapterError(f"{self.company}: invalid Microsoft latest-job card")
            out.append(Posting(
                company=self.company, ats=self.ats, job_id=job_id, title=title,
                location=location, remote="remote" in worksite.casefold(),
                posted_at=posted,
                url=f"https://apply.careers.microsoft.com/careers/job/{job_id}?hl=en",
                raw={"source": "official-latest", "worksite": worksite},
            ))
        return FetchResult(out, complete=False, incremental=True), requests + 1

    def fetch(self, ctx=None):
        """Poll newest roles every interval while pacing closure API resumes hourly.

        Runner-level ``resume_interval`` normally makes an unfinished cursor
        suppress every other kind of poll. Microsoft needs two independent
        cadences, so the company remains due every 15 minutes and the cursor
        itself records when the quota-sensitive closure API may next be used.
        A 403/429 only postpones that API path; it must not hide the official
        three-role latest page for an hour (or longer after repeated throttles).
        """
        ctx = ctx or FetchContext()
        now = int(ctx.now if ctx.now is not None else time.time())
        cur = ctx.cursor or {}
        if cur and int(cur.get("api_after") or 0) > now:
            latest, _ = self.fetch_incremental(ctx)
            return latest

        try:
            result = EightfoldAdapter.fetch(self, ctx)
        except HttpStatusError as e:
            if e.code not in (403, 429):
                raise
            delayed, wait = self._defer_closure(
                cur, now, throttled=True, retry_after=e.retry_after)
            log.warning(
                "%s: closure API %s; retrying in %.1fh while latest-page polls continue",
                self.company, e, wait / 3600.0,
            )
            return FetchResult([], complete=False, cursor=delayed)

        if result.restart and not result.cursor:
            result.cursor, _ = self._defer_closure(self._fresh_cursor(), now)
            return result
        if result.complete or not result.cursor:
            return result

        throttled = result.throttled
        deferred_cursor = dict(result.cursor)
        if throttled is not None and not result.postings:
            # Eightfold's normalized cursor intentionally contains only paging
            # identity. Carry our separate API throttle level across it.
            deferred_cursor["api_backoff_level"] = cur.get("api_backoff_level", 0)
        delayed, wait = self._defer_closure(
            deferred_cursor, now, throttled=throttled is not None,
            progressed=bool(result.postings),
            retry_after=throttled.retry_after if throttled is not None else None,
        )
        result.cursor = delayed
        if throttled is not None:
            log.warning(
                "%s: closure API %s; retrying in %.1fh while latest-page polls continue",
                self.company, throttled, wait / 3600.0,
            )
            result.throttled = None
        return result
