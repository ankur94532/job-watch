"""Jibe career sites (`/api/jobs`) — the platform behind DocuSign's board.

DocuSign runs iCIMS underneath (`*-docusign.icims.com`), and as with Atlassian
iCIMS itself is not queryable. Its careers front end is Jibe (assets served from
`app.jibecdn.com`), and that exposes a plain JSON API on the careers host:

`GET https://{host}/api/jobs?limit=100&page=N&location={location}`

Verified against `careers.docusign.com` on 2026-09-16: `totalCount` 245 board
wide, 77 with `location=India`, `limit=100` honoured, `page` is 1-based.

Per job: `slug`, `req_id` (the requisition number), `title`, `location_name`
("IN-Bangalore"), `country`, `posted_date`, `categories[]`, and a very large
`description` — dropped rather than carried in memory for every row.

`apply_url` points at an iCIMS **login wall** (`indiacareers-docusign.icims.com/
jobs/29563/login`), so the link used is `{host}/careers-home/jobs/{slug}`, which
is the readable posting.
"""

import time

from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting

PAGE = 100
MAX_PAGES = 50


class JibeAdapter(Adapter):
    ats = "jibe"
    required = ("host",)
    paginated = True

    def locations(self):
        locs = self.cfg.get("locations") or [""]
        return [locs] if isinstance(locs, str) else list(locs)

    def url(self, location, page):
        url = f"https://{self.cfg['host']}/api/jobs?limit={PAGE}&page={page}"
        return url + (f"&location={quote(location)}" if location else "")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        locations = self.locations()
        first = min(cur.get("loc", 0), len(locations) - 1)
        ids = set(cur.get("ids", []))
        out, requests = [], 0

        for i in range(first, len(locations)):
            location = locations[i]
            page = cur["page"] if (i == first and cur.get("page")) else 1
            seen_here = 0
            while True:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False,
                                       cursor={"loc": i, "page": page, "ids": sorted(ids)})
                if requests:
                    self.pause()
                data = self.http.get_json(self.url(location, page))
                requests += 1
                rows = data.get("jobs") or []
                total = data.get("totalCount")
                for row in rows:
                    p = self._posting(row.get("data") or row)
                    if p.job_id not in ids:
                        ids.add(p.job_id)
                        out.append(p)
                seen_here += len(rows)
                if not rows or (total is not None and seen_here >= total):
                    break
                page += 1
                if page > MAX_PAGES:
                    raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {seen_here}/{total}")
        return FetchResult(out)

    def _posting(self, raw):
        job_id = str(raw.get("req_id") or raw.get("slug") or "")
        location = (raw.get("location_name") or raw.get("country") or "").strip()
        slug = raw.get("slug") or job_id
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=(raw.get("title") or "").strip(),
            location=location,
            remote="remote" in location.lower(),
            posted_at=(raw.get("posted_date") or raw.get("create_date") or "")[:10],
            # `apply_url` goes straight to an iCIMS login wall; the careers-home page is
            # the readable posting, which is what you want to open from a phone.
            url=f"https://{self.cfg['host']}/careers-home/jobs/{slug}",
            # `description` is several KB per row and is never displayed or exported.
            raw={k: v for k, v in raw.items() if k != "description"},
            req_id=str(raw.get("req_id") or ""),
        )
