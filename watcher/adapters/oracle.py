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

from ..http import HttpStatusError
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

    def count_tolerance(self):
        """How many rows this tenant may over-declare before the sweep fails.

        Defaults to 0: a board that says 200 and hands over 199 is normally a
        parser losing a row, and silently accepting that would retire a real
        job. Oracle's OWN tenant is the exception — see the note on the
        `oracle` entry in config.yaml — so the allowance is opt-in per company
        and bounded, never a blanket relaxation for every Oracle board.
        """
        value = self.cfg.get("count_tolerance", 0)
        if type(value) is not int or value < 0:
            raise AdapterError(
                f"{self.company}: count_tolerance must be a non-negative integer")
        return value

    def url(self, location, offset):
        finder = f"findReqs;siteNumber={self.cfg['site']},limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
        if location:
            finder += f",location={quote(location)}"
        return (f"https://{self.cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        if cur and "loc_ids" not in cur:
            return FetchResult([], complete=False, restart=True)
        self.count_tolerance()      # refuse a misconfigured allowance up front
        locations = self.locations()
        first = min(cur.get("loc", 0), len(locations) - 1)
        ids = set(cur.get("ids", []))
        out, requests = [], 0

        for i in range(first, len(locations)):
            location = locations[i]
            offset = cur["offset"] if (i == first and cur.get("offset")) else 0
            loc_ids = set(cur.get("loc_ids", [])) if i == first else set()
            total = cur.get("total") if i == first else None
            if len(loc_ids) != offset:
                raise AdapterError(f"{self.company}: invalid Oracle resume count")

            def cursor():
                return {"loc": i, "offset": offset, "ids": sorted(ids),
                        "loc_ids": sorted(loc_ids), "total": total}

            pages = 0
            while True:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False,
                                       cursor=cursor())
                if requests:
                    self.pause()
                requests += 1
                try:
                    data = self.http.get_json(self.url(location, offset))
                except HttpStatusError as e:
                    if e.code in (403, 429):
                        return self.partial_on_throttle(e, out, cursor())
                    raise
                pages += 1
                items = data.get("items") or []
                if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                    raise AdapterError(f"{self.company}: no items in response — API shape changed?")
                count = items[0].get("TotalJobsCount")
                rows = items[0].get("requisitionList")
                if not isinstance(rows, list):
                    raise AdapterError(
                        f"{self.company}: response has no requisitionList "
                        f"(total={count}) — the expand parameter is required")
                if type(count) is not int or count < 0:
                    raise AdapterError(f"{self.company}: missing/invalid Oracle total")
                if total is not None and count != total:
                    log.warning("%s: Oracle count changed %d -> %d; restarting", self.company, total, count)
                    return FetchResult([], complete=False, restart=True)
                total = count
                postings = [self._posting(raw) for raw in rows]
                keys = [p.job_id for p in postings]
                if len(set(keys)) != len(keys):
                    raise AdapterError(f"{self.company}: duplicate Oracle page IDs")
                if loc_ids.intersection(keys):
                    return FetchResult([], complete=False, restart=True)
                # A page handing over more than the declared total is always
                # wrong, and no tolerance applies to it.
                if offset + len(rows) > count:
                    raise AdapterError(f"{self.company}: incomplete Oracle page at {offset}/{count}")
                if not rows and offset < count:
                    shortfall = count - offset
                    if shortfall > self.count_tolerance():
                        raise AdapterError(
                            f"{self.company}: incomplete Oracle page at {offset}/{count}")
                    # Loud on purpose: an allowance that stops being needed, or
                    # starts being needed for more rows, should be visible.
                    log.warning("%s: Oracle declared %d but served %d; accepting a "
                                "shortfall of %d within the configured tolerance of %d",
                                self.company, count, offset, shortfall, self.count_tolerance())
                if location and pages == 1 and rows:
                    self._warn_if_filter_ignored(location, rows)
                loc_ids.update(keys)
                for p in postings:
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
        if not any(needle in self._posting(r).location.lower() for r in sample):
            log.warning(
                "%s: location=%r matched none of the first %d rows (e.g. %r) — this tenant "
                "probably does not know that location and is returning the whole board",
                self.company, location, len(sample), (sample[0].get("PrimaryLocation") or ""))

    def _posting(self, raw):
        if not isinstance(raw, dict) or not raw.get("Id") or not (raw.get("Title") or "").strip():
            raise AdapterError(f"{self.company}: missing Oracle identity/title")
        secondary = [s.get("LocationName") or s.get("Location") or s.get("Name") or ""
                     for s in raw.get("secondaryLocations") or []]
        location = join_locations(raw.get("PrimaryLocation"), secondary)
        if not location:
            raise AdapterError(f"{self.company}: missing Oracle location")
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
