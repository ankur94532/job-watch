"""Server-rendered careers pages that ship their search results as embedded JSON.

Apple and Google were both written off earlier because guessed `/api/` paths
404'd. That was the wrong test: the response we already had *was* machine
readable. Neither of these parses rendered markup — each pulls one JSON document
out of the page and reads it. If the shape changes, they raise rather than
quietly returning nothing, so a silent mass-closure is not possible.

Apple (`jobs.apple.com`)
-----------------------
`GET https://jobs.apple.com/en-in/search?location=india-INDC&page=N`

`window.__staticRouterHydrationData` is a JSON.parse("...") of the whole search
state: `loaderData.search.totalRecords` and `searchResults[]`, 20 per page.
Verified 2026-09-17: 176 India postings, 9 requests per sweep.

* **The location token is a trap.** `location=india-IND` is silently rewritten to
  `indianapolis-IND` — Indianapolis, Indiana — and returns 24 US rows that look
  perfectly plausible. The country-level token is `india-INDC`; the response
  echoes `requestUrl`, which the adapter checks, so a rewritten filter fails
  loudly instead of quietly watching the wrong country.
* `jobs.apple.com/robots.txt` 301s to a not-found page: no robots file, nothing
  disallowed.

Google (`google.com/about/careers`)
----------------------------------
`GET .../jobs/results?location=India&sort_by=date`

Results are in an `AF_initDataCallback` block: `data[0]` is an array of rows,
each `[id, title, ..., brand, ..., [[full location, ...]], ..., [epoch, ...]]`.

* **robots.txt forbids pagination**: `Disallow: /about/careers/applications/jobs/
  results?page=` (and the `?*&page=` variants) for `User-agent: *`. Page one is
  allowed, so this fetches exactly that, sorted by date — for a new-posting
  watcher the newest 20 is the part that matters. It will miss anything beyond
  20 new India postings inside one poll interval; `sort_by=date` plus an hourly
  interval keeps that unlikely, and it is the price of honouring robots.
"""

import json
import re
import time
from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

APPLE_PAGE = 20
APPLE_MAX_PAGES = 60

APPLE_HYDRATION = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);?\s*</script>', re.S)
GOOGLE_BLOCK = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
GOOGLE_DATA = re.compile(r"data:(\[.*\])\s*,\s*sideChannel", re.S)


class AppleAdapter(Adapter):
    ats = "apple"
    # NOT "location": that key is the per-company filter override everywhere else
    # in this config, and a string there is iterated into single characters.
    required = ("location_id",)       # e.g. india-INDC — see the docstring's warning
    paginated = True

    def url(self, page):
        base = ("https://jobs.apple.com/" + (self.cfg.get("locale") or "en-in") +
                "/search?location=" + quote(self.cfg["location_id"]))
        return base + ("&page=%d" % page if page > 1 else "")

    def page(self, page):
        body, _ = self.http.request("GET", self.url(page), headers={"Accept": "text/html"})
        html = body.decode("utf-8", "replace")
        m = APPLE_HYDRATION.search(html)
        if not m:
            raise AdapterError(f"{self.company}: no __staticRouterHydrationData on {self.url(page)}")
        try:
            data = json.loads(m.group(1).encode().decode("unicode_escape"))
        except ValueError as e:
            raise AdapterError(f"{self.company}: hydration payload is not JSON: {e}") from None
        search = (data.get("loaderData") or {}).get("search")
        if not search:
            raise AdapterError(f"{self.company}: hydration payload has no search state")
        # Apple rewrites an unknown location instead of rejecting it; catch that.
        echoed = search.get("requestUrl") or ""
        if page == 1 and self.cfg["location_id"].split("-")[0] not in echoed:
            raise AdapterError(
                f"{self.company}: asked for location={self.cfg['location_id']!r} but Apple answered "
                f"{echoed!r} — it rewrote the filter, so these are the wrong jobs")
        return search

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        page = cur.get("page", 1)
        ids = set(cur.get("ids", []))
        out, total, requests = [], cur.get("total"), 0

        while True:
            if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                return FetchResult(out, complete=False,
                                   cursor={"page": page, "total": total, "ids": sorted(ids)})
            if requests:
                self.pause()
            search = self.page(page)
            requests += 1
            if page == 1:
                total = search.get("totalRecords")
            rows = search.get("searchResults") or []
            for raw in rows:
                p = self._posting(raw)
                if p.job_id not in ids:
                    ids.add(p.job_id)
                    out.append(p)
            if not rows or (total is not None and len(ids) >= total):
                break
            page += 1
            if page > APPLE_MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {APPLE_MAX_PAGES}-page cap at {len(ids)}/{total}")
        return FetchResult(out)

    def _posting(self, raw):
        locations = [l.get("name") for l in raw.get("locations") or []]
        pid = str(raw.get("positionId") or raw.get("id"))
        slug = raw.get("transformedPostingTitle") or ""
        locale = self.cfg.get("locale") or "en-in"
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=pid,
            title=(raw.get("postingTitle") or "").strip(),
            location=join_locations(locations),
            remote=bool(raw.get("homeOffice")),
            posted_at=(raw.get("postDateInGMT") or "")[:10],
            url=f"https://jobs.apple.com/{locale}/details/{pid}/{slug}",
            raw=raw,
            req_id=str(raw.get("reqId") or ""),
        )


class GoogleAdapter(Adapter):
    ats = "google"

    def url(self):
        q = self.cfg.get("query")
        return ("https://www.google.com/about/careers/applications/jobs/results"
                "?location=" + quote(self.cfg.get("location_id") or "India") +
                "&sort_by=date" + ("&q=" + quote(q) if q else ""))
        # No &page=: robots.txt disallows paginated results for every user-agent.

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        body, _ = self.http.request("GET", self.url(), headers={"Accept": "text/html"})
        html = body.decode("utf-8", "replace")
        rows = self._rows(html)
        if rows is None:
            raise AdapterError(f"{self.company}: no AF_initDataCallback job payload in the results page")
        out, ids = [], set()
        for raw in rows:
            p = self._posting(raw)
            if p.job_id and p.job_id not in ids:
                ids.add(p.job_id)
                out.append(p)
        return FetchResult(out)

    def _rows(self, html):
        for block in GOOGLE_BLOCK.findall(html):
            m = GOOGLE_DATA.search(block)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if not isinstance(data, list) or not data:
                continue
            first = data[0]
            # data[0] is the result array: each row is [id, title, ...]
            if isinstance(first, list) and first and isinstance(first[0], list) \
                    and len(first[0]) > 9 and isinstance(first[0][1], str):
                return first
        return None

    def _posting(self, raw):
        def at(i, default=None):
            return raw[i] if len(raw) > i else default
        locs = []
        for entry in (at(9) or []):
            if isinstance(entry, list) and entry:
                locs.append(entry[0])
        posted = 0
        stamps = at(12) or []
        if isinstance(stamps, list) and stamps and isinstance(stamps[0], (int, float)):
            posted = int(stamps[0])
        jid = str(at(0) or "")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=jid,
            title=(at(1) or "").strip(),
            location=join_locations(locs),
            remote=any("remote" in (l or "").lower() for l in locs),
            posted_at=time.strftime("%Y-%m-%d", time.gmtime(posted)) if posted else "",
            url=f"https://www.google.com/about/careers/applications/jobs/results/{jid}",
            raw={"row": raw[:14], "brand": at(7)},
            req_id="",                 # Google publishes no requisition number
        )
