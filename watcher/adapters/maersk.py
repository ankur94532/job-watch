"""A.P. Moller--Maersk's official country-filtered careers API.

The public vacancies UI calls this endpoint with a ``Consumer-Key`` shipped in
its JavaScript. ``offset`` is a zero-based PAGE number, not a row offset, and
the service fixes pages at ten rows even when a larger ``limit`` is requested.
"""

import re
import time
from urllib.parse import urlencode, urlsplit

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

API = "https://api.maersk.com/careers/vacancies"
PAGE = 10
MAX_PAGES_PER_RUN = 100
WORKDAY_HOST = "maersk.wd3.myworkdayjobs.com"
WORKDAY_PATHS = ("/Maersk_Careers/job/", "/APMT_Careers/job/")
JOB_ID = re.compile(r"R\d+(?:-\d+)?")


class MaerskAdapter(RobotsAware):
    ats = "maersk"
    required = ("consumer_key", "country")
    paginated = True

    def country(self):
        country = self.cfg["country"]
        if not isinstance(country, str) or country.strip() != "India":
            raise AdapterError(f"{self.company}: Maersk country must be exactly 'India'")
        return country.strip()

    def max_pages_per_run(self):
        value = self.cfg.get("max_pages_per_run", MAX_PAGES_PER_RUN)
        if type(value) is not int or not 1 <= value <= MAX_PAGES_PER_RUN:
            raise AdapterError(
                f"{self.company}: Maersk max_pages_per_run must be an integer from 1 to "
                f"{MAX_PAGES_PER_RUN}")
        return value

    def url(self, page):
        return API + "?" + urlencode({
            "country": self.country(),
            "searchText": "",
            "city": "",
            "cityInput": "",
            "limit": str(PAGE),
            "offset": str(page),
            "language": "EN",
            "distance": "100",
        })

    def headers(self):
        key = self.cfg["consumer_key"]
        if not isinstance(key, str) or not key.strip():
            raise AdapterError(f"{self.company}: invalid Maersk consumer_key")
        return {"Consumer-Key": key.strip()}

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        page, total = cur.get("page", 0), cur.get("total")
        ids = set(cur.get("ids", []))
        if (type(page) is not int or page < 0 or len(ids) != page * PAGE
                or (total is not None and
                    (type(total) is not int or total < 0 or len(ids) >= total))):
            raise AdapterError(f"{self.company}: invalid Maersk cursor")

        out, requests = [], 0
        limit = self.max_pages_per_run()
        self.check_robots(self.url(page))

        def cursor():
            return {"page": page, "total": total, "ids": sorted(ids)}

        while True:
            if requests and (requests >= limit or
                    (ctx.deadline is not None and time.monotonic() >= ctx.deadline)):
                return FetchResult(out, complete=False, cursor=cursor())
            if requests:
                self.pause()
            try:
                payload = self.http.get_json(self.url(page), headers=self.headers())
            except HttpStatusError as error:
                if error.code in (403, 429):
                    return self.partial_on_throttle(error, out, cursor())
                raise
            requests += 1

            if (not isinstance(payload, dict) or payload.get("HasError") is not False
                    or type(payload.get("ResultCount")) is not int
                    or payload["ResultCount"] < 0
                    or payload.get("ResultsPerPage") != PAGE
                    or not isinstance(payload.get("Results"), list)):
                raise AdapterError(f"{self.company}: invalid Maersk result/count")
            count = payload["ResultCount"]
            if total is not None and count != total:
                return FetchResult([], complete=False, restart=True)
            total = count
            rows = [self._posting(row) for row in payload["Results"]]
            keys = [row.job_id for row in rows]
            if len(keys) != len(set(keys)):
                raise AdapterError(f"{self.company}: duplicate Maersk page identity")
            if ids.intersection(keys):
                return FetchResult([], complete=False, restart=True)
            expected = min(PAGE, total - len(ids))
            if expected < 0 or len(rows) != expected:
                raise AdapterError(f"{self.company}: incomplete Maersk result page")
            ids.update(keys)
            out.extend(rows)
            if len(ids) == total:
                return FetchResult(out)
            page += 1

    def _posting(self, row):
        if not isinstance(row, dict):
            raise AdapterError(f"{self.company}: invalid Maersk job")
        external_id = row.get("ExternalId")
        title = row.get("Title")
        url = row.get("Url")
        country = row.get("Country")
        posted = row.get("PostedDT")
        parts = urlsplit(url) if isinstance(url, str) else None
        slug = parts.path.rstrip("/").rsplit("/", 1)[-1] if parts else ""
        job_id = external_id.rsplit("_", 1)[-1] if isinstance(external_id, str) else ""
        if (not isinstance(external_id, str) or not external_id.strip()
                or not isinstance(title, str) or not title.strip()
                or country != self.country()
                or row.get("Brand") != "Maersk" or row.get("Platform") != "Workday"
                or not isinstance(posted, str) or not posted.strip()
                or parts is None or parts.scheme != "https" or parts.netloc != WORKDAY_HOST
                or not parts.path.startswith(WORKDAY_PATHS) or slug != external_id
                or not JOB_ID.fullmatch(job_id)):
            raise AdapterError(f"{self.company}: incomplete or mismatched Maersk job")
        location = join_locations(row.get("City"), row.get("NewCity"), country)
        flexibility = row.get("JobFlexibility") or ""
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=title.strip(),
            location=location,
            remote="remote" in flexibility.lower(),
            posted_at=posted.strip(),
            url=url,
            raw=row,
            req_id=re.sub(r"-\d+$", "", job_id),
        )
