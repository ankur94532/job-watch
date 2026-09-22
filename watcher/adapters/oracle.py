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
from itertools import product
from urllib.parse import quote

from ..http import HttpStatusError
from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, as_list, join_locations

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
        job. A measured tenant-specific over-declaration is the exception, so
        the allowance is opt-in per company and bounded, never a blanket
        relaxation for every Oracle board.
        """
        value = self.cfg.get("count_tolerance", 0)
        if type(value) is not int or value < 0:
            raise AdapterError(
                f"{self.company}: count_tolerance must be a non-negative integer")
        return value

    def partition_scopes(self):
        workplaces = as_list(self.cfg.get("workplace_types"), [None])
        dates = as_list(self.cfg.get("posting_date_facets"), [None])
        return list(product(self.locations(), workplaces, dates))

    def partitioned(self):
        return bool(self.cfg.get("workplace_types") or self.cfg.get("posting_date_facets"))

    def partition_signature(self):
        return ["|".join("" if v is None else str(v) for v in scope)
                for scope in self.partition_scopes()]

    def url(self, location, offset, workplace=None, posting_date=None, limit=PAGE):
        finder = f"findReqs;siteNumber={self.cfg['site']},limit={limit},offset={offset},sortBy=POSTING_DATES_DESC"
        if location:
            finder += f",location={quote(location)}"
        if workplace is not None:
            finder += f",selectedWorkplaceTypesFacet={quote(str(workplace))}"
        if posting_date is not None:
            finder += f",selectedPostingDatesFacet={quote(str(posting_date))}"
        return (f"https://{self.cfg['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")

    def _item(self, data):
        items = data.get("items") or []
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise AdapterError(f"{self.company}: no items in response — API shape changed?")
        return items[0]

    def _validate_partition_cover(self, item, total):
        """Prove the configured facet product is an exact disjoint cover.

        Workplace type is an enum, while posting-date facet 30/31 means
        less-than-30-days / greater-than-30-days. The API may also advertise
        the overlapping seven-day shortcut; only the configured disjoint pair
        is summed.
        """
        checks = (
            ("workplace_types", "workplaceTypesFacet"),
            ("posting_date_facets", "postingDatesFacet"),
        )
        for cfg_key, response_key in checks:
            configured = [str(v) for v in as_list(self.cfg.get(cfg_key))]
            if not configured:
                continue
            raw = item.get(response_key)
            if not isinstance(raw, list):
                raise AdapterError(f"{self.company}: missing Oracle {response_key}")
            counts = {str(v.get("Id")): v.get("TotalCount") for v in raw
                      if isinstance(v, dict)}
            if any(type(counts.get(v)) is not int or counts[v] < 0 for v in configured):
                raise AdapterError(f"{self.company}: Oracle {response_key} does not contain configured cover")
            if sum(counts[v] for v in configured) != total:
                raise AdapterError(
                    f"{self.company}: Oracle {response_key} cover sums to "
                    f"{sum(counts[v] for v in configured)}, expected {total}")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        partitioned = self.partitioned()
        signature = self.partition_signature()

        def restart_cursor():
            if not partitioned:
                return None
            return {"loc": 0, "offset": 0, "ids": [], "loc_ids": [], "total": None,
                    "partition_signature": signature, "partition_counts": [],
                    "partition_declared": [], "inventory_total": None}

        if cur and "loc_ids" not in cur:
            return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
        if partitioned and cur and cur.get("partition_signature") != signature:
            return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
        self.count_tolerance()      # refuse a misconfigured allowance up front
        scopes = self.partition_scopes()
        first = min(cur.get("loc", 0), len(scopes) - 1)
        ids = set(cur.get("ids", []))
        out, requests = [], 0
        inventory_total = cur.get("inventory_total")
        partition_counts = list(cur.get("partition_counts") or [])
        partition_declared = list(cur.get("partition_declared") or [])
        if len(partition_counts) > len(scopes):
            raise AdapterError(f"{self.company}: invalid Oracle partition counts")
        if len(partition_declared) > len(scopes):
            raise AdapterError(f"{self.company}: invalid Oracle partition declarations")
        partition_counts.extend([None] * (len(scopes) - len(partition_counts)))
        partition_declared.extend([None] * (len(scopes) - len(partition_declared)))

        if partitioned:
            requests += 1
            meta = self._item(self.http.get_json(self.url(self.locations()[0], 0, limit=1)))
            current_total = meta.get("TotalJobsCount")
            if type(current_total) is not int or current_total < 0:
                raise AdapterError(f"{self.company}: missing/invalid Oracle total")
            self._validate_partition_cover(meta, current_total)
            if inventory_total is not None and current_total != inventory_total:
                log.warning("%s: Oracle inventory count changed %d -> %d; restarting",
                            self.company, inventory_total, current_total)
                return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
            inventory_total = current_total

        for i in range(first, len(scopes)):
            location, workplace, posting_date = scopes[i]
            offset = cur["offset"] if (i == first and cur.get("offset")) else 0
            loc_ids = set(cur.get("loc_ids", [])) if i == first else set()
            total = cur.get("total") if i == first else None
            if len(loc_ids) != offset:
                raise AdapterError(f"{self.company}: invalid Oracle resume count")

            def cursor():
                return {"loc": i, "offset": offset, "ids": sorted(ids),
                        "loc_ids": sorted(loc_ids), "total": total,
                        **({"partition_signature": signature,
                            "partition_counts": partition_counts,
                            "partition_declared": partition_declared,
                            "inventory_total": inventory_total} if partitioned else {})}

            pages = 0
            while True:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False,
                                       cursor=cursor())
                if requests:
                    self.pause()
                requests += 1
                try:
                    data = self.http.get_json(
                        self.url(location, offset, workplace, posting_date))
                except HttpStatusError as e:
                    if e.code in (403, 429):
                        return self.partial_on_throttle(e, out, cursor())
                    raise
                pages += 1
                item = self._item(data)
                count = item.get("TotalJobsCount")
                rows = item.get("requisitionList")
                if not isinstance(rows, list):
                    raise AdapterError(
                        f"{self.company}: response has no requisitionList "
                        f"(total={count}) — the expand parameter is required")
                if type(count) is not int or count < 0:
                    raise AdapterError(f"{self.company}: missing/invalid Oracle total")
                if total is not None and count != total:
                    log.warning("%s: Oracle count changed %d -> %d; restarting", self.company, total, count)
                    return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
                total = count
                if workplace is not None and str(item.get("SelectedWorkplaceTypesFacet")) != str(workplace):
                    raise AdapterError(f"{self.company}: Oracle ignored workplace partition")
                if posting_date is not None and str(item.get("SelectedPostingDatesFacet")) != str(posting_date):
                    raise AdapterError(f"{self.company}: Oracle ignored posting-date partition")
                postings = [self._posting(raw) for raw in rows]
                keys = [p.job_id for p in postings]
                if len(set(keys)) != len(keys):
                    raise AdapterError(f"{self.company}: duplicate Oracle page IDs")
                overlap = loc_ids.intersection(keys)
                if overlap:
                    prospective = loc_ids.union(keys)
                    shortfall = count - len(prospective)
                    terminal_duplicate = offset + len(rows) >= count
                    if not (partitioned and terminal_duplicate
                            and 0 <= shortfall <= self.count_tolerance()):
                        return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
                    log.warning("%s: Oracle partition declared %d but exposes %d unique; "
                                "accepting a stable boundary duplicate within tolerance %d",
                                self.company, count, len(prospective), self.count_tolerance())
                if partitioned and (ids - loc_ids).intersection(keys):
                    log.warning("%s: Oracle facet partitions overlap; restarting", self.company)
                    return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
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
            if partitioned:
                shortfall = total - len(loc_ids)
                if shortfall < 0 or shortfall > self.count_tolerance():
                    raise AdapterError(
                        f"{self.company}: Oracle partition returned {len(loc_ids)} unique, "
                        f"expected {total}")
                partition_counts[i] = len(loc_ids)
                partition_declared[i] = total

        if partitioned:
            if (any(type(n) is not int for n in partition_counts)
                    or any(type(n) is not int for n in partition_declared)):
                raise AdapterError(f"{self.company}: incomplete Oracle partition inventory")
            declared = sum(partition_declared)
            if declared != inventory_total or len(ids) != sum(partition_counts):
                raise AdapterError(
                    f"{self.company}: Oracle partitions returned {len(ids)} unique / "
                    f"{declared} declared, expected {inventory_total}")
            self.pause()
            final = self._item(self.http.get_json(self.url(self.locations()[0], 0, limit=1)))
            final_total = final.get("TotalJobsCount")
            if type(final_total) is not int or final_total < 0:
                raise AdapterError(f"{self.company}: missing/invalid Oracle total")
            self._validate_partition_cover(final, final_total)
            if final_total != inventory_total:
                log.warning("%s: Oracle inventory count changed %d -> %d; restarting",
                            self.company, inventory_total, final_total)
                return FetchResult([], complete=False, cursor=restart_cursor(), restart=True)
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
