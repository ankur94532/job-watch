"""Instahyre's public job search, as a LABELLED SECONDARY source.

This is not an employer's own board. Instahyre is an Indian job site where a
company's recruiters post openings themselves, and it is watched here only for
employers whose own board is unreadable (Cloudflare-challenged, robots-
disallowed, or switched off). Config entries are named `<company>-instahyre`
so the site never presents these rows as the employer's own inventory — the
same convention already used for `levelai` / `levelai-lever`.

What that means, precisely:

* Coverage is PARTIAL with respect to the employer. Forty-eight Flipkart rows
  here are not Flipkart's whole hiring; they are what Flipkart posted to
  Instahyre. Never treat an empty or shrinking result as the employer closing
  roles.
* Coverage is COMPLETE with respect to Instahyre, which is what makes this
  safe to sweep: `meta.total_count` is authoritative and every page is checked
  against it, so rows that leave Instahyre are retired honestly. No
  `incremental` shortcut is needed or wanted.

Source, read off the site's own AngularJS bundle rather than guessed:

    GET /api/v1/job_search?limit=35&offset=N&companies=<employer>

`companies=` is the filter the frontend uses. A misspelled parameter such as
`company=` is SILENTLY IGNORED and returns the entire unfiltered 13k-row
inventory, which is why every row's employer identity is verified below rather
than trusted from the query. The page size is capped server-side at 35
regardless of the `limit` asked for, so pagination is by `offset`.

`https://www.instahyre.com/robots.txt` publishes a sitemap line and a bare
`User-agent: *` with no rules at all: nothing is disallowed. The human-facing
`/jobs-at-<company>/` page IS behind a Cloudflare challenge and is deliberately
never fetched; this reads only the JSON API, which answers honest clients.

Each row also has a detail endpoint, `/api/v1/employer_public_jobs/<job id>`,
carrying `hiring_company_name` and an `is_active` flag. It is not fetched per
sweep — that would be one request per job for data the search already filters —
but it is the way to confirm a single posting by hand.
"""

import time
from urllib.parse import quote, urlsplit

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

BASE = "https://www.instahyre.com"
# Asking for more is not refused, it is silently truncated to this, so the
# adapter must page rather than believe a larger `limit`.
PAGE = 35


class InstahyreAdapter(RobotsAware):
    ats = "instahyre"
    required = ("employer",)
    paginated = True

    def employer(self):
        name = self.cfg.get("employer")
        if not isinstance(name, str) or not name.strip():
            raise AdapterError(f"{self.company}: instahyre employer must be a name")
        return name.strip()

    def employer_id(self):
        value = self.cfg.get("employer_id")
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise AdapterError(f"{self.company}: instahyre employer_id must be a positive integer")
        return value

    def url(self, offset):
        return (BASE + "/api/v1/job_search?limit=" + str(PAGE) + "&offset=" + str(offset) +
                "&companies=" + quote(self.employer()))

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        offset, total = cur.get("offset", 0), cur.get("total")
        ids = set(cur.get("ids", []))
        raw_employer_ids = cur.get("employer_ids", [])
        if (not isinstance(raw_employer_ids, list)
                or any(type(value) is not int or value <= 0 for value in raw_employer_ids)):
            raise AdapterError(f"{self.company}: invalid Instahyre cursor")
        employer_ids = set(raw_employer_ids)
        if (type(offset) is not int or offset < 0 or len(ids) != offset
                or (offset and (type(total) is not int or len(ids) >= total))
                or len(employer_ids) > 1):
            raise AdapterError(f"{self.company}: invalid Instahyre cursor")
        out, requests = [], 0
        limit = max(1, int(self.cfg.get("max_pages_per_run", 12)))

        def cursor():
            return {"offset": offset, "total": total, "ids": sorted(ids),
                    "employer_ids": sorted(employer_ids)}

        expected_employer_id = self.employer_id()  # validate before any request
        self.check_robots(self.url(offset))
        while True:
            if requests and (requests >= limit or
                    (ctx.deadline is not None and time.monotonic() >= ctx.deadline)):
                return FetchResult(out, complete=False, cursor=cursor())
            if requests:
                self.pause()
            try:
                payload = self.http.get_json(self.url(offset))
            except HttpStatusError as error:
                if error.code in (403, 429):
                    return self.partial_on_throttle(error, out, cursor())
                raise
            requests += 1
            meta = payload.get("meta") if isinstance(payload, dict) else None
            rows = payload.get("objects") if isinstance(payload, dict) else None
            if (not isinstance(meta, dict) or not isinstance(rows, list)
                    or type(meta.get("total_count")) is not int or meta["total_count"] < 0):
                raise AdapterError(f"{self.company}: invalid Instahyre objects/meta")
            count = meta["total_count"]
            # The employer's own count moving mid-sweep makes the remaining
            # offsets meaningless: start again rather than stitch two lists.
            if total is not None and total != count:
                return FetchResult([], complete=False, restart=True)
            total = count
            postings = [self._posting(row, expected_employer_id, employer_ids) for row in rows]
            keys = [p.job_id for p in postings]
            if len(keys) != len(set(keys)):
                raise AdapterError(f"{self.company}: duplicate Instahyre page identity")
            if ids.intersection(keys):
                return FetchResult([], complete=False, restart=True)
            if len(postings) != min(PAGE, total - len(ids)):
                raise AdapterError(f"{self.company}: incomplete Instahyre page at {offset}/{total}")
            ids.update(keys)
            out.extend(postings)
            offset += len(postings)
            if len(ids) == total:
                return FetchResult(out, declared=total)

    def _posting(self, row, expected_employer_id=None, employer_ids=None):
        if not isinstance(row, dict):
            raise AdapterError(f"{self.company}: invalid Instahyre job")
        job_id, title = row.get("id"), row.get("title")
        locations, url = row.get("locations"), row.get("public_url")
        employer = row.get("employer")
        if (type(job_id) is not int or job_id <= 0
                or not isinstance(title, str) or not title.strip()
                or not isinstance(locations, str) or not locations.strip()
                or not isinstance(url, str) or not isinstance(employer, dict)):
            raise AdapterError(f"{self.company}: missing Instahyre identity/title/location")
        # `companies=` being ignored on a typo returns the whole site, and those
        # rows would look like perfectly ordinary jobs. Identity is therefore
        # checked per row by name and, when it is known, numeric employer id.
        # Empty known employers expose no id to configure; in that case learn
        # it from the first future row and refuse if any page uses another id.
        row_employer_id = employer.get("id")
        if (employer.get("company_name") != self.employer()
                or type(row_employer_id) is not int or row_employer_id <= 0
                or (expected_employer_id is not None
                    and row_employer_id != expected_employer_id)):
            raise AdapterError(
                f"{self.company}: Instahyre row {job_id} is "
                f"{employer.get('company_name')!r}/{row_employer_id!r}, "
                f"not {self.employer()!r}/{expected_employer_id!r}")
        if employer_ids is not None:
            employer_ids.add(row_employer_id)
            if len(employer_ids) > 1:
                raise AdapterError(
                    f"{self.company}: Instahyre returned multiple employer ids "
                    f"for {self.employer()!r}: {sorted(employer_ids)}")
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "www.instahyre.com"
                or not parts.path.startswith("/job-%d-" % job_id)):
            raise AdapterError(f"{self.company}: mismatched Instahyre job link {url!r}")
        location = join_locations([p.strip() for p in locations.split(",")])
        # Instahyre publishes no posting date on the search row, and the detail
        # endpoint's dates look regenerated, so none is invented here.
        return Posting(self.company, self.ats, str(job_id), title.strip(), location,
                       "work from home" in location.lower(), "", url, req_id="")
