"""Oracle Recruiting Cloud — the candidate-experience REST API (JPMorgan Chase).

`GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions`
`  ?onlyData=true&expand=requisitionList.secondaryLocations`
`  &finder=findReqs;siteNumber={site},limit=200,offset=N,sortBy=POSTING_DATES_DESC,location={location}`

Public and keyless, multi-tenant (every `*.fa.oraclecloud.com` careers site uses
the same shape), and the awkward part is the `finder=` pseudo-query: parameters
are comma-separated inside it, not normal query parameters.

Verified against `jpmc.fa.oraclecloud.com` / `CX_1001` on 2026-09-16:

* The response is `items[0]`, with `TotalJobsCount` and `requisitionList[]`.
* **`expand=requisitionList...` is required** — without it the count comes back
  but `requisitionList` is missing entirely, which looks exactly like an empty
  board. That is the trap here.
* `limit=200` works (7,394 reqs unfiltered → 37 requests; `location=India`
  narrows it to 314 → 2 requests, which is why the location filter is used).
* Per req: `Id` (the requisition number), `Title`, `PostedDate` (ISO),
  `PrimaryLocation` ("Mumbai, Maharashtra, India"), `secondaryLocations[]`,
  `PrimaryLocationCountry`, `JobFunction`, `ShortDescriptionStr`.
* **An unrecognised `location` is silently ignored**, not rejected: on Oracle's
  own site `location=Bengaluru` returns the whole 2,165-job global board while
  looking filtered. Only nodes the tenant knows (there, `India`) narrow it. The
  adapter samples the first page and warns when none of the rows mention the
  configured location.
* No ETag (`cache-control: no-store`), so this runs hourly.
* Apply URL: `https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{Id}`.
"""

import logging
import time
from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

log = logging.getLogger(__name__)

PAGE = 200
MAX_PAGES = 100


class OracleAdapter(Adapter):
    ats = "oracle"
    required = ("host", "site")
    paginated = True

    def locations(self):
        locs = self.cfg.get("locations") or [""]
        return [locs] if isinstance(locs, str) else list(locs)

    def url(self, location, offset):
        finder = f"findReqs;siteNumber={self.cfg['site']},limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
        if location:
            finder += f",location={quote(location)}"
        return (f"https://{self.cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        locations = self.locations()
        first = min(cur.get("loc", 0), len(locations) - 1)
        ids = set(cur.get("ids", []))
        out, requests = [], 0

        for i in range(first, len(locations)):
            location = locations[i]
            offset = cur["offset"] if (i == first and cur.get("offset")) else 0
            pages = 0
            while True:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False,
                                       cursor={"loc": i, "offset": offset, "ids": sorted(ids)})
                if requests:
                    self.pause()
                data = self.http.get_json(self.url(location, offset))
                requests += 1
                pages += 1
                items = data.get("items") or []
                if not items:
                    raise AdapterError(f"{self.company}: no items in response — API shape changed?")
                total = items[0].get("TotalJobsCount")
                rows = items[0].get("requisitionList")
                if rows is None:
                    raise AdapterError(
                        f"{self.company}: response has no requisitionList "
                        f"(total={total}) — the expand parameter is required")
                if location and pages == 1 and rows:
                    self._warn_if_filter_ignored(location, rows)
                for raw in rows:
                    p = self._posting(raw)
                    if p.job_id not in ids:
                        ids.add(p.job_id)
                        out.append(p)
                if not rows:
                    break
                offset += len(rows)
                if total is not None and offset >= total:
                    break
                if pages >= MAX_PAGES:
                    raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {offset}/{total}")
        return FetchResult(out)

    def _warn_if_filter_ignored(self, location, rows):
        """A location this tenant does not know returns the entire board, quietly."""
        needle = location.lower()
        sample = rows[:20]
        if not any(needle in (r.get("PrimaryLocation") or "").lower() for r in sample):
            log.warning(
                "%s: location=%r matched none of the first %d rows (e.g. %r) — this tenant "
                "probably does not know that location and is returning the whole board",
                self.company, location, len(sample), (sample[0].get("PrimaryLocation") or ""))

    def _posting(self, raw):
        secondary = [s.get("LocationName") or s.get("Location") or "" for s in raw.get("secondaryLocations") or []]
        location = join_locations(raw.get("PrimaryLocation"), secondary)
        job_id = str(raw.get("Id"))
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=(raw.get("Title") or "").strip(),
            location=location,
            remote="remote" in (raw.get("WorkplaceTypeCode") or "").lower() or "remote" in location.lower(),
            posted_at=(raw.get("PostedDate") or "")[:10],
            url=f"https://{self.cfg['host']}/hcmUI/CandidateExperience/en/sites/{self.cfg['site']}/job/{job_id}",
            raw=raw,
            req_id=job_id,
        )
