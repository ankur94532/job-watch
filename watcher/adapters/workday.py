"""Workday CXS adapter (generalised from the original salesforce_jobs.py).

Behaviour confirmed against live tenants on 2026-09-16 — see adapters/README.md:

* limit > 20 answers HTTP 400.
* Pagination wraps: an offset past the end returns the first page again. The
  giveaway is a page whose rows are all already seen — but ONE such page is not
  proof. Walmart's ordering is not stable between requests (thousands of
  near-identical store roles), so a single all-duplicate page turns up in the
  middle of a healthy sweep. Treating that as the end stopped its sweep at
  360 of 2000, which failed the coverage check, so the sweep never completed
  and its closed requisitions were never retired. It now takes a run of
  DUP_PAGES_BEFORE_WRAP consecutive duplicate pages.
* `total` is only populated on the offset=0 response; later pages say 0.
* bulletFields order is tenant-specific. Salesforce puts the req ID first,
  NatWest puts "Hybrid" / "Closing Date: ..." first. Never take [0] blindly.
* Multi-location postings list as "3 Locations"; the detail endpoint has them.
* No ETag / Last-Modified (responses are `Cache-Control: no-store`), so every
  poll is a full sweep. That is why Workday defaults to an hourly interval.
"""

import logging
import re
import time

from ..http import HttpStatusError
from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

log = logging.getLogger(__name__)

PAGE = 20            # Workday hard-caps this server-side
MAX_PAGES = 200      # safety net against the wrap-around bug
DUP_PAGES_BEFORE_WRAP = 3   # see the wrap-around note in the module docstring
MIN_COVERAGE = 0.95  # below this share of `total`, treat the fetch as failed

MULTI_LOCATION = re.compile(r"^\d+\s+Locations?$", re.I)


class WorkdayAdapter(Adapter):
    ats = "workday"
    required = ("tenant", "shard", "sites")
    paginated = True

    def host(self):
        return f"https://{self.cfg['tenant']}.{self.cfg['shard']}.myworkdayjobs.com"

    def api(self, site):
        return f"{self.host()}/wday/cxs/{self.cfg['tenant']}/{site}"

    def sites(self):
        s = self.cfg["sites"]
        return [s] if isinstance(s, str) else list(s)

    def fetch(self, ctx=None):
        """Sweep every site, page by page, until each site's `total` is reached.

        The pagination WRAPS: an offset past the end returns rows again, not an
        empty page. Stop on `total` (read from the offset-0 page only — later
        pages report 0), stop when a page adds no new ids, and cap the page count.

        With `ctx.deadline` set the sweep pauses between pages once time is up and
        returns a cursor; the next process resumes one page early so postings that
        shifted across a page boundary in the meantime are not skipped.
        """
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        sites = self.sites()
        first_site = min(cur.get("site", 0), len(sites) - 1)
        ids = set(cur.get("ids", []))          # across sites: one posting can be on several
        out, requests = [], 0

        for i in range(first_site, len(sites)):
            site = sites[i]
            resuming = i == first_site and cur.get("offset", 0) > 0
            if resuming:
                offset = max(0, cur["offset"] - PAGE)
                total = cur.get("total")
                site_ids = set(cur.get("site_ids", []))
            else:
                offset, total, site_ids = 0, None, set()
            # Resuming deliberately re-reads the page before the cursor, so the
            # first page back is all duplicates by design, not by wrapping.
            first_after_resume = resuming
            dup_run = 0
            pages = 0

            while True:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    cursor = {"site": i, "offset": offset, "total": total,
                              "site_ids": sorted(site_ids), "ids": sorted(ids)}
                    return FetchResult(out, complete=False, cursor=cursor)
                if requests:
                    self.pause()
                try:
                    page = self.fetch_page(site, offset)
                except HttpStatusError as e:
                    if e.code in (403, 429) and out:
                        return self.partial_on_throttle(e, out, {
                            "site": i, "offset": offset, "total": total,
                            "site_ids": sorted(site_ids), "ids": sorted(ids)})
                    raise
                requests += 1
                pages += 1
                if offset == 0:
                    total = page.get("total") or None
                rows = page.get("jobPostings") or []
                added = 0
                for raw in rows:
                    p = self._posting(site, raw)
                    if p.job_id in site_ids:
                        continue
                    site_ids.add(p.job_id)
                    added += 1
                    if p.job_id not in ids:
                        ids.add(p.job_id)
                        out.append(p)
                if len(rows) < PAGE:
                    break
                if added == 0:
                    if first_after_resume:
                        first_after_resume = False
                    else:
                        dup_run += 1
                        if dup_run >= DUP_PAGES_BEFORE_WRAP:
                            break                    # genuinely wrapped
                else:
                    dup_run = 0
                    first_after_resume = False
                offset += PAGE
                if total and (len(site_ids) >= total or offset >= total):
                    break
                if pages >= MAX_PAGES:
                    raise AdapterError(f"{self.company}/{site}: hit {MAX_PAGES}-page cap at {len(site_ids)}/{total}")

            if total and len(site_ids) < total:
                msg = f"{self.company}/{site}: got {len(site_ids)} of {total} reported"
                if len(site_ids) < total * MIN_COVERAGE:
                    raise AdapterError(msg + " — incomplete sweep, not trusting it")
                log.warning(msg)
        return FetchResult(out)

    def fetch_page(self, site, offset):
        body = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}
        try:
            return self.http.post_json(f"{self.api(site)}/jobs", body)
        except HttpStatusError as e:
            if e.code in (400, 404, 422):
                raise AdapterError(
                    f"{self.company}: Workday HTTP {e.code} for site {site!r} at offset {offset} "
                    f"— check tenant/shard/site against a live job URL"
                ) from e
            raise

    def _posting(self, site, raw):
        path = raw.get("externalPath") or ""
        slug = path.rstrip("/").rsplit("/", 1)[-1]
        # Slug is "<Title-Slug>_<ReqID>[-N]"; the part after the last "_" is
        # unique per posting and is what Workday shows as the job's ID.
        job_id = slug.rsplit("_", 1)[-1] if "_" in slug else (slug or path)
        req_id = next((b for b in raw.get("bulletFields") or [] if b and b in slug), "") or job_id
        location = (raw.get("locationsText") or "").strip()
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=(raw.get("title") or "").strip(),
            location=location,
            remote="remote" in (raw.get("remoteType") or "").lower()
            or any("remote" in (b or "").lower() for b in raw.get("bulletFields") or []),
            posted_at=(raw.get("postedOn") or "").strip(),
            url=f"{self.host()}/en-US/{site}{path}",
            raw=dict(raw, _site=site),
            req_id=req_id,
        )

    def needs_enrich(self, posting):
        return bool(MULTI_LOCATION.match(posting.location))

    def enrich(self, posting):
        site = posting.raw.get("_site") or self.sites()[0]
        path = posting.raw.get("externalPath") or ""
        self.pause()
        info = self.http.get_json(f"{self.api(site)}{path}").get("jobPostingInfo") or {}
        location = join_locations(info.get("location"), info.get("additionalLocations") or [])
        if location:
            posting.location = location
        if info.get("jobReqId"):
            posting.req_id = info["jobReqId"]
        if info.get("startDate"):
            posting.posted_at = info["startDate"]
        if "remote" in (info.get("remoteType") or "").lower():
            posting.remote = True
        posting.raw["_detail"] = {k: v for k, v in info.items() if k != "jobDescription"}
        return posting
