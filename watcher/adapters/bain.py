"""Bain's counted public search, using the request published by its careers UI.

The API requires the normal careers-page Referer. No cookies, login, or special
user agent are required. ``start`` is a zero-based PAGE number, not an offset.
Read all offices and retain every reported location for downstream filtering.
"""

import time
from urllib.parse import parse_qs, urljoin, urlsplit

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

BASE = "https://www.bain.com"
REFERER = BASE + "/careers/find-a-role/"
# Bain's default ten-row pages do not have a stable order: a row moved from
# page 29 to page 30 during validation even though totalResults stayed at 326.
# The public endpoint accepts a larger results value, and 400 returned the
# entire current inventory in one response. Keep pagination for future growth,
# but avoid page-boundary gaps and overlaps for the normal case.
DEFAULT_PAGE_SIZE = 400
MAX_PAGE_SIZE = 500


class BainAdapter(RobotsAware):
    ats = "bain"
    paginated = True

    def page_size(self):
        size = self.cfg.get("page_size", DEFAULT_PAGE_SIZE)
        if type(size) is not int or not 1 <= size <= MAX_PAGE_SIZE:
            raise AdapterError(
                f"{self.company}: Bain page_size must be an integer from 1 to {MAX_PAGE_SIZE}")
        return size

    def url(self, page):
        return (BASE + "/en/api/jobsearch/keyword/get?start=" + str(page) +
                "&results=" + str(self.page_size()) + "&filters=&searchValue=")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        page_size = self.page_size()
        page, total = cur.get("page", 0), cur.get("total")
        ids = set(cur.get("ids", []))
        if (type(page) is not int or page < 0 or len(ids) != page * page_size or
                (page and (type(total) is not int or len(ids) >= total))):
            raise AdapterError(f"{self.company}: invalid Bain cursor")
        out, requests = [], 0
        limit = max(1, int(self.cfg.get("max_pages_per_run", 12)))

        def cursor():
            return {"page": page, "total": total, "ids": sorted(ids)}

        self.check_robots(self.url(page))
        while True:
            if requests and (requests >= limit or
                    (ctx.deadline is not None and time.monotonic() >= ctx.deadline)):
                return FetchResult(out, complete=False, cursor=cursor())
            if requests:
                self.pause()
            try:
                payload = self.http.get_json(self.url(page), headers={"Referer": REFERER})
            except HttpStatusError as error:
                if error.code in (403, 429):
                    return self.partial_on_throttle(error, out, cursor())
                raise
            requests += 1
            if (not isinstance(payload, dict) or not isinstance(payload.get("results"), list)
                    or type(payload.get("totalResults")) is not int
                    or payload["totalResults"] < 0):
                raise AdapterError(f"{self.company}: invalid Bain result/count")
            count = payload["totalResults"]
            if total is not None and total != count:
                return FetchResult([], complete=False, restart=True)
            total = count
            rows = [self._posting(row) for row in payload["results"]]
            keys = [row.job_id for row in rows]
            if len(keys) != len(set(keys)):
                raise AdapterError(f"{self.company}: duplicate Bain page identity")
            if ids.intersection(keys):
                return FetchResult([], complete=False, restart=True)
            if len(rows) != min(page_size, total - len(ids)):
                raise AdapterError(f"{self.company}: incomplete Bain result page")
            ids.update(keys)
            out.extend(rows)
            if len(ids) == total:
                return FetchResult(out)
            page += 1

    def _posting(self, row):
        if not isinstance(row, dict):
            raise AdapterError(f"{self.company}: invalid Bain job")
        job_id, title, link = (row.get(key) for key in ("JobId", "JobTitle", "Link"))
        locations = row.get("Location")
        if (not isinstance(job_id, str) or not job_id.isdigit()
                or not isinstance(title, str) or not title.strip()
                or not isinstance(link, str) or not link.strip()
                or not isinstance(locations, list)
                or any(not isinstance(loc, str) or not loc.strip() for loc in locations)):
            raise AdapterError(f"{self.company}: missing Bain identity/title/link/location")
        url = urljoin(BASE, link)
        parts = urlsplit(url)
        position = parts.path == "/careers/find-a-role/position/"
        # Bain lists evergreen programme pages alongside real requisitions, and
        # it adds new KINDS of them: on 2026-10-11 a single row,
        # "Undergraduate exploratory programs" (id 2791789), arrived under
        # /careers/recruiting/ and blocked the whole 298-row sweep for days
        # because the adapter only knew the two prefixes below. These pages
        # carry no jobid, which is why only `position` is checked against one.
        program = parts.path.startswith(("/careers/work-with-us/internships-programs/",
                                         "/careers/recruiting/"))
        if (parts.scheme != "https" or parts.netloc != "www.bain.com" or
                not (position or program) or
                (position and parse_qs(parts.query).get("jobid") != [job_id])):
            raise AdapterError(f"{self.company}: mismatched Bain job link")
        # The source explicitly leaves some program locations empty. Retain
        # these jobs in the counted inventory without inventing eligibility;
        # a configured India/city filter excludes the empty location.
        location = join_locations(locations)
        return Posting(self.company, self.ats, job_id, title.strip(), location,
                       "remote" in location.lower(), "", url, req_id=job_id)
