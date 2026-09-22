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
RESYNC_REQUESTS = 6         # anchored sweeps: requests one re-sync may spend finding its boundary
MAX_RESYNCS = 12            # anchored sweeps: re-syncs per sweep before giving up and restarting
GROUP_ROWS = 200            # anchored sweeps: largest same-timestamp group read by count
GROUP_REPLAYS = 3           # anchored sweeps: re-reads of a group before giving up
RESYNC_BUDGET = 12          # anchored sweeps: requests a re-sync may use despite a smaller page cap
RESYNC_SLICES = 4           # anchored sweeps: slices a paused re-sync may take before restarting


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

    def cursor_scope(self):
        """Extra query identity that must remain fixed across a resumed sweep."""
        return None

    def fresh_cursor(self):
        """A validated zero-progress cursor for a replacement sweep.

        Keeping this cursor after count drift or page overlap lets a company
        with `resume_priority` retry promptly.  It does not preserve any rows
        from the incoherent sweep: the runner starts a new sweep epoch whenever
        `restart` is true.
        """
        if self.anchored():
            return self._anchor_cursor(0, None, set(), None, None, 0, scope=self.cursor_scope())
        value = {"loc": 0, "start": 0, "ids": [], "loc_ids": [], "total": None}
        scope = self.cursor_scope()
        if scope is not None:
            value["scope"] = scope
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
        scope = self.cursor_scope()
        if cur and scope is not None and cur.get("scope") != scope:
            return FetchResult([], complete=False, restart=True, cursor=self.fresh_cursor())
        if self.anchored():
            return self._fetch_anchored(ctx, cur, requests)
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
                value = {"loc": i, "start": start, "ids": sorted(ids),
                         "loc_ids": sorted(loc_ids), "total": total}
                if scope is not None:
                    value["scope"] = scope
                return value
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
                    return FetchResult([], complete=False, restart=True,
                                       cursor=self.fresh_cursor())
                total = count
                postings = [self._posting(raw) for raw in rows]
                keys = [p.job_id for p in postings]
                if len(set(keys)) != len(keys):
                    raise AdapterError(f"{self.company}: duplicate Eightfold page IDs")
                if loc_ids.intersection(keys):
                    log.warning("%s: Eightfold page overlaps prior IDs at offset %d; restarting",
                                self.company, start)
                    return FetchResult([], complete=False, restart=True,
                                       cursor=self.fresh_cursor())
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

    # ---- Anchored sweeps (opt-in: `anchor_resume: true`) --------------------
    #
    # A newest-first board that gains postings faster than a quota-bounded
    # sweep can finish never completes under the offset guards above: every
    # front insertion shifts seen rows onto the next page (overlap) or changes
    # the count, and both restart. Qualcomm did exactly that on 2026-09-22.
    #
    # In timestamp order, the only way a shifting list can hide a row from an
    # offset sweep is a row crossing the boundary between what was read and
    # what was not. So a page is accepted by offset only under the old strict
    # conditions (unchanged count, no repeated ID). Otherwise the sweep
    # re-syncs: it looks for a page whose FIRST row is strictly newer than the
    # oldest row read so far (`anchor_ts`), which proves the whole boundary,
    # including any tie at `anchor_ts`, lies inside that page. Continuity then
    # holds by the sort itself, and every row is validated against it:
    #
    # * rows newer than the anchor must already be seen, or be newer than the
    #   sweep's first row (`head_ts`: a posting made during the sweep);
    #   anything else is an older job re-listed behind us, so restart;
    # * rows older than the anchor must be unseen, or the order is inconsistent;
    # * every page must be non-increasing in postedTs, or restart.
    #
    # On completion, the newest pages are re-read down to `head_ts`, so a job
    # re-posted to the front mid-sweep is still seen and not falsely closed,
    # and the final count may not exceed the IDs read. Single location only.

    def anchored(self):
        if not self.cfg.get("anchor_resume"):
            return False
        if self.flavor() != "pcsx" or not self.incremental:
            raise AdapterError(f"{self.company}: anchor_resume needs pcsx with incremental timestamp order")
        if len(self.locations()) != 1:
            raise AdapterError(f"{self.company}: anchor_resume supports exactly one location")
        return True

    @staticmethod
    def _anchor_cursor(start, total, ids, anchor_ts, head_ts, resyncs, phase="sweep",
                       resync_slices=0, scope=None):
        value = {"mode": "anchor", "phase": phase, "start": start, "total": total,
                 "ids": sorted(ids), "anchor_ts": anchor_ts, "head_ts": head_ts,
                 "resyncs": resyncs, "resync_slices": resync_slices}
        if scope is not None:
            value["scope"] = scope      # the query identity must survive, or every resume restarts
        return value

    def resync_budget(self):
        """Requests a re-sync may use even when `max_pages_per_run` is smaller.

        A paused re-sync starts over, so too small a budget could stall it.
        Microsoft's API allows about two requests an hour, so it sets this to
        its page cap and allows more slices per re-sync instead."""
        return int(self.cfg.get("resync_budget") or RESYNC_BUDGET)

    def resync_slices(self):
        return int(self.cfg.get("resync_slices") or RESYNC_SLICES)

    def _anchored_page(self, location, offset):
        """One validated page: (postings, stamps, count). Raises HttpStatusError."""
        body = self.http.get_json(self.url(location, offset)) or {}
        data = body.get("data") or {}
        rows, count = data.get("positions"), data.get("count")
        if not isinstance(rows, list) or type(count) is not int or count < 0:
            raise AdapterError(f"{self.company}: missing Eightfold positions/count")
        if data.get("sortBy") not in (None, "timestamp"):
            raise AdapterError(f"{self.company}: anchored sweep needs timestamp order, got {data.get('sortBy')!r}")
        postings = [self._posting(raw) for raw in rows]
        stamps = [raw.get("postedTs") for raw in rows]
        if any(type(ts) is not int for ts in stamps):
            raise AdapterError(f"{self.company}: anchored sweep needs postedTs on every row")
        keys = [p.job_id for p in postings]
        if len(set(keys)) != len(keys):
            raise AdapterError(f"{self.company}: duplicate Eightfold page IDs")
        return postings, stamps, count

    def _fetch_anchored(self, ctx, cur, requests):
        if cur and cur.get("mode") != "anchor":
            if cur.get("start") or cur.get("ids"):
                return FetchResult([], complete=False, restart=True, cursor=self.fresh_cursor())
            cur = {}
        location = self.locations()[0]
        ids = set(cur.get("ids") or [])               # every row read by this sweep
        pass_ids = set(cur["pass_ids"] if "pass_ids" in cur else ids)   # rows read by this pass
        start = int(cur.get("start") or 0)
        total, anchor_ts, head_ts = cur.get("total"), cur.get("anchor_ts"), cur.get("head_ts")
        resyncs, phase = int(cur.get("resyncs") or 0), cur.get("phase") or "sweep"
        resync_slices = int(cur.get("resync_slices") or 0)
        # Pass 2 re-reads the newest rows down to pass 1's first row (stop_ts),
        # so a job re-posted ahead of the pass-1 cursor is still seen.
        sweep_pass, stop_ts = int(cur.get("pass") or 1), cur.get("stop_ts")
        if phase not in ("sweep", "resync") or sweep_pass not in (1, 2):
            return FetchResult([], complete=False, restart=True, cursor=self.fresh_cursor())
        if (start or pass_ids) and (anchor_ts is None or head_ts is None):
            raise AdapterError(f"{self.company}: invalid anchored resume point")
        if sweep_pass == 2 and stop_ts is None:
            raise AdapterError(f"{self.company}: invalid anchored resume point")
        out = []
        max_pages = self.max_pages_per_run()

        def cursor():
            value = self._anchor_cursor(start, total, ids, anchor_ts, head_ts, resyncs, phase,
                                        resync_slices, scope=self.cursor_scope())
            value.update({"pass": sweep_pass, "stop_ts": stop_ts})
            if sweep_pass == 2:
                value["pass_ids"] = sorted(pass_ids)
            return value

        def restart(why):
            log.warning("%s: anchored sweep %s at offset %d; restarting", self.company, why, start)
            return FetchResult([], complete=False, restart=True, cursor=self.fresh_cursor())

        def take(p):
            pass_ids.add(p.job_id)
            if p.job_id not in ids:
                ids.add(p.job_id)
                out.append(p)

        def get(offset):
            nonlocal requests
            if requests:
                self.pause()
            requests += 1
            return self._anchored_page(location, offset)

        def out_of_budget():
            limit = max_pages
            if limit is not None and phase == "resync":
                limit = max(limit, self.resync_budget())
            return ((limit is not None and requests >= limit)
                    or (requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline))

        def read_group(first, pos, ts_group, count0, members):
            """Read a same-timestamp group that spans pages, by count.

            `first` is the group's first position, witnessed by a strictly
            newer row just before it. Pages from `pos` are read until a
            strictly older row marks its end, which fixes the group's size.
            Its pages are then re-read, each time re-witnessing both edges,
            until the IDs collected equal that size. The board's count must
            not change throughout. Returns (new anchor_ts, next offset,
            whether the list ended), or a FetchResult to return (pause or
            restart).
            """
            end, below = None, []   # older rows count as read only once the group is proven
            while end is None:
                if pos - first > GROUP_ROWS:
                    return restart(f"found a same-timestamp group over {GROUP_ROWS} rows")
                if out_of_budget():
                    return FetchResult(out, complete=False, cursor=cursor())
                postings, stamps, count = get(pos)
                if count != count0:
                    return restart("saw the count change while reading a same-timestamp group")
                for p, ts in zip(postings, stamps):
                    if ts > ts_group:
                        return restart("found a newer row inside a same-timestamp group")
                    if ts == ts_group:
                        members.add(p.job_id)
                        take(p)
                    elif p.job_id in pass_ids:
                        return restart("found a seen job below the boundary")
                    else:
                        below.append(p)
                older = [i for i, ts in enumerate(stamps) if ts < ts_group]
                if older:
                    end = (pos + older[0], pos, stamps[-1], len(postings))
                elif len(postings) < PAGE:
                    end = (pos + len(postings), pos, stamps[-1] if stamps else ts_group, len(postings))
                else:
                    pos += len(postings)
            last, last_offset, last_ts, last_len = end
            size = last - first

            def proven():
                log.info("%s: read a %d-row same-timestamp group by count", self.company, size)
                for p in below:
                    take(p)
                return last_ts, last_offset + last_len, last_len < PAGE

            if len(members) == size:
                # Already complete: re-witness only the start edge, so the
                # size cannot come from edges read at shifted positions.
                if out_of_budget():
                    return FetchResult(out, complete=False, cursor=cursor())
                offset = max(0, first - 1)
                postings, stamps, count = get(offset)
                if count == count0 and all(
                        (first <= offset + i < last) == (ts == ts_group)
                        for i, ts in enumerate(stamps)):
                    return proven()
            for _ in range(GROUP_REPLAYS):
                witnessed = True
                offset = max(0, first - 1)
                while offset < last:
                    if out_of_budget():
                        return FetchResult(out, complete=False, cursor=cursor())
                    postings, stamps, count = get(offset)
                    if count != count0:
                        return restart("saw the count change while reading a same-timestamp group")
                    for i, (p, ts) in enumerate(zip(postings, stamps)):
                        inside = first <= offset + i < last
                        if inside != (ts == ts_group):
                            witnessed = False            # an edge moved; this pass proves nothing
                        if ts == ts_group:
                            members.add(p.job_id)
                            take(p)
                    if not postings:
                        break
                    offset += len(postings)
                if len(members) > size:
                    return restart(f"read {len(members)} rows for a {size}-row same-timestamp group")
                if witnessed and len(members) == size:
                    return proven()
            return restart(f"could not read a {size}-row same-timestamp group consistently")

        def pass_done(ended, last_ts):
            """Did the pass just reach its end? Then start pass 2 or finish."""
            nonlocal sweep_pass, stop_ts, start, total, anchor_ts, head_ts, phase, pass_ids
            if not (ended or start >= total or (sweep_pass == 2 and last_ts is not None
                                                  and last_ts <= stop_ts)):
                return False
            if sweep_pass == 2:
                return True
            sweep_pass, stop_ts = 2, head_ts
            start, total, anchor_ts, head_ts, phase, pass_ids = 0, None, None, None, "sweep", set()
            return False

        if phase == "sweep" and start:
            # A resumed slice follows a gap in which the list has usually moved
            # (Microsoft re-posts hourly). Its first page re-witnesses the
            # boundary instead: it costs two rows of overlap, but a plain
            # offset page would usually fail and waste the request.
            phase = "resync"

        count = total
        try:
            while True:
                count = total
                if phase == "sweep":
                    if out_of_budget():
                        return FetchResult(out, complete=False, cursor=cursor())
                    postings, stamps, count = get(start)
                    if any(a < b for a, b in zip(stamps, stamps[1:])):
                        return restart("found rows out of timestamp order")
                    if ((total is None or count == total)
                            and not any(p.job_id in pass_ids for p in postings)
                            and (anchor_ts is None or not stamps or stamps[0] <= anchor_ts)):
                        if start == 0 and head_ts is None:
                            head_ts = stamps[0] if stamps else 0
                        if start + len(postings) > count or (not postings and start < count):
                            raise AdapterError(f"{self.company}: incomplete Eightfold page at {start}/{count}")
                        for p in postings:
                            take(p)
                        if stamps:
                            anchor_ts = stamps[-1]
                        total = count
                        start += len(postings)
                        resyncs = 0                       # the limit is on re-syncs without progress
                        if pass_done(len(postings) < PAGE, stamps[-1] if stamps else None):
                            break
                        continue
                    resyncs += 1
                    if resyncs > MAX_RESYNCS:
                        return restart("needed too many re-syncs")
                    log.info("%s: Eightfold list moved (count %s -> %d) at offset %d; re-syncing",
                             self.company, total, count, start)
                    phase = "resync"

                # Re-sync: find a page that begins strictly above the anchor.
                # Until it succeeds the cursor stays in phase "resync", so a
                # slice that pauses here can never resume by plain offset.
                resync_slices += 1              # every slice that works on this re-sync
                if resync_slices > self.resync_slices():
                    return restart(f"could not finish a re-sync in {self.resync_slices()} slices")
                offset = max(0, start - 2 + max(0, (count or 0) - (total or count or 0)))
                for _ in range(RESYNC_REQUESTS):
                    if out_of_budget():
                        return FetchResult(out, complete=False, cursor=cursor())
                    postings, stamps, count = get(offset)
                    if any(a < b for a, b in zip(stamps, stamps[1:])):
                        return restart("found rows out of timestamp order")
                    if offset and (not postings or stamps[0] <= anchor_ts):
                        offset = max(0, offset - (PAGE - 1))   # boundary is earlier
                        continue
                    for p, ts in zip(postings, stamps):
                        if ts > anchor_ts:
                            if p.job_id in pass_ids or ts > head_ts:
                                take(p)
                                continue
                            return restart(f"found unseen older job {p.job_id} behind the boundary")
                        if ts < anchor_ts and p.job_id in pass_ids:
                            return restart("found a seen job below the boundary")
                        take(p)
                    if not postings or stamps[-1] > anchor_ts:
                        if len(postings) < PAGE:        # list ends above the anchor
                            anchor_ts, start, ended = (stamps[-1] if stamps else anchor_ts), count, True
                            break
                        offset += len(postings) - 1     # boundary is later; keep a row of overlap
                        continue
                    if len(postings) == PAGE and stamps[-1] == anchor_ts:
                        # The anchor's same-timestamp group runs past this
                        # page, and such groups are not returned in a stable
                        # order, so offsets inside it prove nothing. Read it by
                        # count instead.
                        group = read_group(offset + stamps.index(anchor_ts), offset + PAGE,
                                           anchor_ts, count,
                                           {p.job_id for p, ts in zip(postings, stamps)
                                            if ts == anchor_ts})
                        if isinstance(group, FetchResult):
                            return group
                        anchor_ts, start, ended = group
                    else:
                        anchor_ts = stamps[-1]
                        start = offset + len(postings)
                        ended = len(postings) < PAGE
                    break
                else:
                    return restart("could not find its boundary")
                total, phase, resync_slices = count, "sweep", 0
                if pass_done(ended, anchor_ts):
                    break
        except HttpStatusError as e:
            if e.code in (403, 429):
                return self.partial_on_throttle(e, out, cursor())
            raise
        if count > len(ids):
            return restart(f"read {len(ids)} IDs but the board declares {count}")
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
