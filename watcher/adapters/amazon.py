"""amazon.jobs — self-hosted, but a public keyless JSON search endpoint.

Verified 2026-09-16:

* `GET https://www.amazon.jobs/en/search.json?result_limit=100&offset=N&sort=recent&country=IND`
* `result_limit` maxes at 100; 200 returns `{"jobs": null}` rather than an error.
* **`country=IND` filters; `country[]=IND` is silently ignored** and you get the
  whole global board (`hits: 10000`, a cap) with the filter apparently applied.
  The bracketed form is what the site's own UI sends, which makes this an easy
  mistake to ship. `normalized_country_code[]=IND` also works.
* `hits` caps at 10000; filtered to India it is a real count (2,325).
* ETag present and honoured, so a board that fits on one page revalidates cheaply.
* `id_icims` is the requisition number; `job_path` is relative.
* `posted_date` is "September 16, 2026" — normalised to ISO so alert ranking can
  sort by it.
"""

import time
from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting

API = ("https://www.amazon.jobs/en/search.json?result_limit={limit}&offset={offset}"
       "&sort=recent&country={country}")
VIEW = "https://www.amazon.jobs"

PAGE = 100
MAX_PAGES = 100


class AmazonAdapter(Adapter):
    ats = "amazon"
    paginated = True

    def countries(self):
        c = self.cfg.get("countries") or ["IND"]
        return [c] if isinstance(c, str) else list(c)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        countries = self.countries()
        first = min(cur.get("country", 0), len(countries) - 1)
        out, ids = [], set(cur.get("ids", []))
        pages = 0
        single = len(countries) == 1
        validators = dict(ctx.cache) if (single and not cur and ctx.cache.get("single_page")) else {}
        for i in range(first, len(countries)):
            country = countries[i]
            resuming = i == first and cur.get("offset")
            offset = cur["offset"] if resuming else 0
            hits = cur.get("hits") if resuming else None
            while True:
                # 2,300 reqs at 100 a page is ~25 requests; on a slow link that can
                # outrun the run budget, so it pauses and resumes like Workday does.
                if pages and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False, cursor={
                        "country": i, "offset": offset, "hits": hits, "ids": sorted(ids)})
                if pages >= MAX_PAGES:
                    raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {len(out)}/{hits}")
                if pages:
                    self.pause()
                url = API.format(limit=PAGE, offset=offset, country=quote(country))
                conditional = single and pages == 0 and not cur
                data = self.http.get_json(url, cache=validators) if conditional else self.http.get_json(url)
                pages += 1
                rows = data.get("jobs") or []
                if hits is None:
                    hits = data.get("hits") or 0
                for raw in rows:
                    p = self._posting(raw)
                    if p.job_id not in ids:
                        ids.add(p.job_id)
                        out.append(p)
                offset += len(rows)
                if not rows or offset >= hits:
                    break
        ctx.cache.clear()
        if single and pages == 1 and not cur:
            ctx.cache.update(validators, single_page=True)
        return FetchResult(out)

    def _posting(self, raw):
        path = raw.get("job_path") or ""
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=str(raw.get("id_icims") or raw.get("id") or path),
            title=(raw.get("title") or "").strip(),
            location=raw.get("location") or ", ".join(
                x for x in (raw.get("city"), raw.get("country_code")) if x),
            remote="virtual" in (raw.get("location") or "").lower(),
            posted_at=_iso(raw.get("posted_date")),
            url=VIEW + path if path.startswith("/") else path,
            raw=raw,
            req_id=str(raw.get("id_icims") or ""),
        )


def _iso(value):
    """"September 16, 2026" -> "2026-09-16"; anything else passes through."""
    value = (value or "").strip()
    try:
        return time.strftime("%Y-%m-%d", time.strptime(value, "%B %d, %Y"))
    except ValueError:
        return value
