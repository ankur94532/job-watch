"""Eightfold — the ATS behind Microsoft's and Netflix's boards.

Multi-tenant but much less uniform than Greenhouse: the host is per company,
`domain` identifies the tenant, and there are **two incompatible flavours** of
the same product, so `api:` selects one (verified 2026-09-16):

* `api: pcsx` (default) — `/api/pcsx/search`, results nested under `data`,
  fields `name` / `locations` / `postedTs` / `displayJobId` / `positionUrl`.
  Microsoft. Netflix's host answers "PCSX is not enabled for this user".
* `api: apply` — `/api/apply/v2/jobs`, results at the top level, fields
  `name` / `location(s)` / `t_update` / `display_job_id` / `canonicalPositionUrl`.
  Netflix. Microsoft's host answers "Not authorized for PCSX" for this one, and
  a host that is not the tenant's own answers "Tenant not identified".

Both cap `num` at 10 server-side, both count in `count`, and neither wraps.

Verified against apply.careers.microsoft.com on 2026-09-16 and rechecked from
scheduler evidence through 2026-09-19:

* `num` is capped at 10 server-side — asking for 50 or 100 still returns 10.
  Microsoft's latest complete India inventory was 239 positions, so 24 requests
  per sweep at that count.
* `start` past the end returns an empty list (no Workday-style wrap-around).
* `data.count` is the total for the query and is present on every page.
* No ETag; `cache-control: no-store`.
* `location` filters server-side. That matters: unfiltered, Microsoft's board is
  thousands of rows, and fetching all of them 10 at a time to throw away 95%
  would be exactly the traffic pattern worth avoiding. One query per configured
  location, deduped by position id.
"""

import logging
import time

from ..http import HttpStatusError
from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

log = logging.getLogger(__name__)

PAGE = 10             # server-side cap
MAX_PAGES = 200
FULL_SWEEP = 12 * 3600      # incremental boards: a complete sweep at least this often
INCREMENTAL_MAX_PAGES = 5   # more new than this and a sweep is the better use of the quota
KNOWN_PAGES_TO_STOP = 2     # consecutive all-known pages before an incremental poll stops


class EightfoldAdapter(Adapter):
    ats = "eightfold"
    required = ("host", "domain")
    paginated = True
    # One Eightfold board at a time, even across different hosts. Each tenant
    # has its own hostname, so parallel polling gave each its own lane, and on
    # 2026-09-18 Microsoft and Qualcomm were throttled (429) within a minute of
    # each other. Shared quota is a hypothesis, not confirmed by response headers.
    max_parallel = 1
    # Keep traffic conservative across tenants. launchd runs every 15 minutes,
    # so start one tenant per process and let oldest-first scheduling rotate them.
    max_per_run = 1

    def locations(self):
        locs = self.cfg.get("locations") or [""]
        return [locs] if isinstance(locs, str) else list(locs)

    def flavor(self):
        api = (self.cfg.get("api") or "pcsx").lower()
        if api not in ("pcsx", "apply"):
            raise AdapterError(f"{self.company}: eightfold api must be 'pcsx' or 'apply', got {api!r}")
        return api

    @property
    def incremental(self):
        # Newest-first needs `sort_by=timestamp`, which only pcsx honours: the
        # apply/v2 flavour (Netflix, NetApp) accepts it and returns rows unordered.
        return bool(self.cfg.get("incremental")) and self.flavor() == "pcsx"

    def full_sweep(self):
        return int(self.cfg.get("full_sweep") or FULL_SWEEP)

    def max_pages_per_run(self):
        value = self.cfg.get("max_pages_per_run")
        if value in (None, ""):
            return None
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise AdapterError(f"{self.company}: max_pages_per_run must be a positive integer")
        if value <= 0:
            raise AdapterError(f"{self.company}: max_pages_per_run must be a positive integer")
        return value

    def url(self, location, start):
        from urllib.parse import quote

        domain, host = quote(self.cfg["domain"]), self.cfg["host"]
        if self.flavor() == "apply":
            return (f"https://{host}/api/apply/v2/jobs?domain={domain}"
                    f"&location={quote(location)}&start={start}&num={PAGE}")
        # Incremental boards are swept in timestamp order too: the same order
        # every time is kinder to a paused-and-resumed sweep than relevance.
        order = "&sort_by=timestamp" if self.incremental or self.cfg.get("sort_by") == "timestamp" else ""
        return (f"https://{host}/api/pcsx/search?domain={domain}"
                f"&query={quote(self.cfg.get('query') or '')}&location={quote(location)}"
                f"&start={start}&num={PAGE}{order}")

    def fetch_incremental(self, ctx, requests=0):
        """The newest pages only; `(None, requests)` means "sweep instead".

        `sort_by=timestamp` produced newest-first results on Morgan Stanley in
        a 2026-09-18 check (ordered through offset 90 and identical across
        reads). Since other tenants have not honored the requested order,
        postedTs is re-checked on every page: it must never go UP, or the
        shortcut is abandoned. Stops after KNOWN_PAGES_TO_STOP pages in a row
        with nothing new. It cannot see closures, so the result is incremental
        and retires nothing; a complete sweep still runs every `full_sweep`
        seconds.
        """
        out, seen = [], set()
        max_pages = self.max_pages_per_run()
        for location in self.locations():
            known_run, prev, done = 0, None, False
            for n in range(INCREMENTAL_MAX_PAGES):
                if max_pages is not None and requests >= max_pages:
                    return FetchResult(out, complete=False, incremental=True), requests
                if n:
                    self.pause()
                try:
                    requests += 1
                    body = self.http.get_json(self.url(location, n * PAGE)) or {}
                except HttpStatusError as e:
                    if e.code in (403, 429) and out:
                        return self.partial_on_throttle(e, out, None), requests
                    raise
                data = body.get("data") or {}
                if data.get("sortBy") not in (None, "timestamp"):
                    log.warning("%s: asked for sort_by=timestamp, got %r; sweeping instead",
                                self.company, data.get("sortBy"))
                    return None, requests
                rows = data.get("positions") or []
                fresh = False
                for raw in rows:
                    ts = raw.get("postedTs")
                    if ts is not None:
                        if prev is not None and ts > prev:
                            log.warning("%s: not newest-first at offset %d; sweeping instead",
                                        self.company, n * PAGE)
                            return None, requests
                        prev = ts
                    p = self._posting(raw)
                    if p.job_id in seen:
                        continue
                    seen.add(p.job_id)
                    out.append(p)
                    fresh |= p.job_id not in ctx.known
                known_run = 0 if fresh else known_run + 1
                if known_run >= KNOWN_PAGES_TO_STOP or len(rows) < PAGE:
                    done = True
                    break
            if not done:
                log.info("%s: %d pages of new postings; sweeping instead", self.company, INCREMENTAL_MAX_PAGES)
                return None, requests
        return FetchResult(out, complete=False, incremental=True), requests

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        requests = 0
        if (self.incremental and not ctx.cursor and ctx.known and ctx.last_complete
                and ctx.now is not None and ctx.now - ctx.last_complete < self.full_sweep()):
            quick, requests = self.fetch_incremental(ctx, requests)
            if quick is not None:
                return quick
        cur = ctx.cursor or {}
        if cur and "loc_ids" not in cur:
            # Legacy cursors cannot prove per-location completeness. Restart
            # with a fresh sweep epoch, without declaring closures from old pages.
            return FetchResult([], complete=False, restart=True)
        locations = self.locations()
        first_loc = min(cur.get("loc", 0), len(locations) - 1)
        ids = set(cur.get("ids", []))
        out = []
        max_pages = self.max_pages_per_run()

        for i in range(first_loc, len(locations)):
            location = locations[i]
            start = cur["start"] if (i == first_loc and cur.get("start")) else 0
            loc_ids = set(cur.get("loc_ids", [])) if i == first_loc else set()
            total = cur.get("total") if i == first_loc else None
            if len(loc_ids) != start:
                raise AdapterError(f"{self.company}: invalid Eightfold resume count")
            def cursor():
                return {"loc": i, "start": start, "ids": sorted(ids),
                        "loc_ids": sorted(loc_ids), "total": total}
            pages = 0
            while True:
                if max_pages is not None and requests >= max_pages:
                    return FetchResult(out, complete=False,
                                       cursor=cursor())
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False,
                                       cursor=cursor())
                if requests:
                    self.pause()
                try:
                    requests += 1
                    body = self.http.get_json(self.url(location, start)) or {}
                except HttpStatusError as e:
                    # Keep rows already paid for and resume after the persisted backoff.
                    if e.code in (403, 429):
                        return self.partial_on_throttle(
                            e, out, cursor())
                    raise
                # pcsx nests everything under "data"; apply/v2 returns it at the top level.
                data = body if self.flavor() == "apply" else (body.get("data") or {})
                pages += 1
                rows = data.get("positions")
                count = data.get("count")
                if not isinstance(rows, list) or type(count) is not int or count < 0:
                    raise AdapterError(f"{self.company}: missing Eightfold positions/count")
                if total is not None and total != count:
                    log.warning("%s: Eightfold count changed %d -> %d at offset %d; restarting",
                                self.company, total, count, start)
                    return FetchResult([], complete=False, restart=True)
                total = count
                postings = [self._posting(raw) for raw in rows]
                keys = [p.job_id for p in postings]
                if len(set(keys)) != len(keys):
                    raise AdapterError(f"{self.company}: duplicate Eightfold page IDs")
                if loc_ids.intersection(keys):
                    log.warning("%s: Eightfold page overlaps prior IDs at offset %d; restarting",
                                self.company, start)
                    return FetchResult([], complete=False, restart=True)
                if start + len(rows) > count or (not rows and start < count):
                    raise AdapterError(f"{self.company}: incomplete Eightfold page at {start}/{count}")
                loc_ids.update(keys)
                for p in postings:
                    if p.job_id not in ids:
                        ids.add(p.job_id)
                        out.append(p)
                if not rows:
                    break
                # Advance by what came back, not by PAGE: a short page would
                # otherwise skip the records the server did not send.
                start += len(rows)
                if count is not None and start >= count:
                    break
                if pages >= MAX_PAGES:
                    raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {start}/{count}")
        return FetchResult(out)

    def _posting(self, raw):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            raise AdapterError(f"{self.company}: missing Eightfold identity/title")
        location = join_locations(raw.get("locations") or raw.get("location") or [],
                                  raw.get("standardizedLocations") or [])
        if not location:
            raise AdapterError(f"{self.company}: missing Eightfold location")
        posted = raw.get("postedTs") or raw.get("creationTs") or raw.get("t_update")
        path = raw.get("positionUrl") or raw.get("canonicalPositionUrl") or f"/careers/job/{raw.get('id')}"
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=str(raw.get("id")),
            title=(raw.get("name") or "").strip(),
            location=location,
            remote="remote" in (raw.get("workLocationOption") or "").lower(),
            posted_at=time.strftime("%Y-%m-%d", time.gmtime(posted)) if posted else "",
            url=f"https://{self.cfg['host']}{path}" if path.startswith("/") else path,
            raw=raw,
            # displayJobId / display_job_id is what the recruiter and the referral form use.
            req_id=str(raw.get("displayJobId") or raw.get("display_job_id") or raw.get("atsJobId") or ""),
        )
