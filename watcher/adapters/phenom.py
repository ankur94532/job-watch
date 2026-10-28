"""Phenom CareerConnect's public, zero-auth job-search widget.

Branded Phenom sites expose ``POST /widgets``.  Unlike their sitemap, the
response carries stable job ids, titles, dates, and locations, and accepts a
country facet plus 100-row pagination.  Quest Global is the verified tenant.
"""

import html
import logging
import re
import time
from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, as_list

PAGE = 100
MAX_PAGES = 64
MAX_WINDOW = 1000
MAX_PARTITION_REPLAYS = 2

log = logging.getLogger(__name__)


def clean(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def slugify(value):
    return re.sub(r"[^A-Za-z0-9]+", "-", clean(value)).strip("-") or "job"


class PhenomAdapter(Adapter):
    """Phenom widget inventory with exact-count paging and optional facet partitions.

    Large boards are split only when configured/published facets form an exact,
    disjoint cover of the declared result set and every partition fits inside
    the service's verified result window.
    """
    ats = "phenom"
    required = ("host",)
    paginated = True

    def api(self):
        return f"https://{self.cfg['host']}/widgets"

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        seen = set(cur.get("ids") or [])
        out, requests, pending = [], 0, None
        if cur.get("partitions"):
            partitions = list(cur["partitions"])
            counts = list(cur["partition_counts"])
            partition = int(cur.get("partition") or 0)
            offset = int(cur.get("offset") or 0)
            part_seen = int(cur.get("part_seen") or 0)
            part_ids = set(cur.get("part_ids") or [])
            pass_ids = set(cur.get("pass_ids") or cur.get("part_ids") or [])
            partition_replays = int(cur.get("partition_replays") or 0)
            total = int(cur["total"])
        else:
            pending = self.http.post_json(self.api(), self.payload(0))
            requests = 1
            result, _, total = self.page(pending)
            max_window = int(self.cfg.get("max_window") or MAX_WINDOW)
            partition_threshold = int(self.cfg.get("partition_threshold") or max_window)
            if not 0 < partition_threshold <= max_window:
                raise AdapterError(f"{self.company}: partition_threshold must be within max_window")
            if total > partition_threshold:
                # Phenom/Elasticsearch caps one result window at 1,000. Split
                # only across configured single-valued facets whose published
                # bucket counts add back to the total. Never use city:
                # multi-location jobs can appear in several city buckets.
                requested_fields = as_list(self.cfg.get("partition_fields"), ["category"])
                aggregations = {a.get("field"): a.get("value")
                                for a in (result.get("data") or {}).get("aggregations") or []}
                buckets = [(field, value, count)
                           for field in requested_fields
                           for value, count in (aggregations.get(field) or {}).items()]
                if (not buckets or sum(count for _, _, count in buckets) != total
                        or max((count for _, _, count in buckets), default=0) > max_window):
                    raise AdapterError(f"{self.company}: {total} jobs exceed Phenom's {max_window}-row "
                                       "window and configured facets cannot partition them safely")
                # Keep the first configured window of unpartitioned results too. Facet
                # aggregations can omit an uncategorized job or double-count a
                # multi-category job; unioning them with the full search window
                # closes that one-row gap while still avoiding the deep window.
                partitions = [None, *({"field": field, "value": value}
                                      for field, value, _ in buckets)]
                counts = [min(total, max_window), *(count for _, _, count in buckets)]
            else:
                partitions, counts = [None], [total]
            partition, offset, part_seen, part_ids = 0, 0, 0, set()
            pass_ids, partition_replays = set(), 0

        pages = 0
        while partition < len(partitions):
            if pending is None:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False, cursor={
                        "partitions": partitions, "partition_counts": counts, "partition": partition,
                        "offset": offset, "part_seen": part_seen, "part_ids": sorted(part_ids),
                        "pass_ids": sorted(pass_ids), "partition_replays": partition_replays,
                        "total": total, "ids": sorted(seen),
                    })
                if requests:
                    self.pause()
                pending = self.http.post_json(self.api(), self.payload(offset, partitions[partition]))
                requests += 1
            _, rows, page_total = self.page(pending)
            pending = None
            expected_page_total = total if partitions[partition] is None else counts[partition]
            if page_total != expected_page_total:
                # The saved sweep is no longer coherent. Raising would keep the
                # cursor, so every retry would compare against the same stale
                # count and fail until the cursor aged out (three hours on
                # 2026-09-22). Restart instead: a fresh epoch, no closures.
                log.warning("%s: Phenom %s count changed during sweep (%d to %d); restarting",
                            self.company, partitions[partition] or "board",
                            expected_page_total, page_total)
                return FetchResult([], complete=False, restart=True)

            for raw in rows:
                posting = self.posting(raw)
                if posting.job_id not in pass_ids:
                    pass_ids.add(posting.job_id)
                part_ids.add(posting.job_id)
                if posting.job_id in seen:
                    continue
                seen.add(posting.job_id)
                out.append(posting)
            if len(seen) == total:
                return FetchResult(out)
            offset += len(rows)
            part_seen += len(rows)
            pages += 1
            if pages >= MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {len(seen)}/{total}")
            if not rows or part_seen >= counts[partition] or len(rows) < PAGE:
                if part_seen != counts[partition]:
                    raise AdapterError(f"{self.company}: got {part_seen} of {counts[partition]} Phenom jobs "
                                       f"for {partitions[partition] or 'the board'}")
                # Phenom's default relevance order can reshuffle between
                # offsets.  A partition may therefore return its declared
                # number of rows while repeating IDs at page boundaries.  A
                # bounded replay is safe: we still require exactly the
                # published number of unique IDs before calling the partition
                # complete, and fail closed if the repeats do not fill the
                # gaps.  The unpartitioned window is only redundant coverage,
                # so replay real facet partitions rather than that sample.
                expected = counts[partition]
                replay_limit = int(self.cfg.get("partition_replays") or MAX_PARTITION_REPLAYS)
                if (partitions[partition] is not None and len(part_ids) < expected
                        and partition_replays < replay_limit):
                    partition_replays += 1
                    offset = part_seen = 0
                    pass_ids = set()
                    continue
                # Every slot the facet promised was served (checked above), so
                # when fewer DISTINCT ids come back the board is repeating a
                # row in its own bookkeeping rather than hiding one from us.
                # Measured on IDFC FIRST 2026-10-14: the failing facet served
                # 235 slots holding 230 distinct ids, and two consecutive
                # passes returned an IDENTICAL set -- a stable inventory, the
                # same shape as Arm. Replays cannot help a stable repeat, and
                # failing closed meant the board never completed, so no closed
                # role was ever retired. `repeat_tolerance` is opt-in per
                # company and defaults to 0, so every other Phenom board stays
                # strict. If the warning's number grows, or the stored count
                # starts oscillating between sweeps, re-measure stability
                # before raising it: a VARYING missing set would retire real
                # jobs on alternating sweeps, which is the one outcome worse
                # than stale data.
                shortfall = expected - len(part_ids)
                if (partitions[partition] is not None and shortfall
                        and shortfall <= int(self.cfg.get("repeat_tolerance") or 0)):
                    log.warning("%s: %s served %d slots holding %d distinct jobs",
                                self.company, partitions[partition], expected, len(part_ids))
                elif partitions[partition] is not None and len(part_ids) != expected:
                    raise AdapterError(
                        f"{self.company}: got {len(part_ids)} unique of {expected} Phenom jobs "
                        f"for {partitions[partition]} after {partition_replays + 1} pass(es)")
                partition += 1
                offset = part_seen = 0
                part_ids = set()
                pass_ids = set()
                partition_replays = 0

        # Same reasoning as the per-partition check: repeated rows inside the
        # facets leave the board total short by the same amount.
        if len(seen) != total:
            shortfall = total - len(seen)
            if not (0 < shortfall <= int(self.cfg.get("repeat_tolerance") or 0)):
                raise AdapterError(
                    f"{self.company}: got {len(seen)} unique Phenom jobs but expected {total}")
            log.warning("%s: board declared %d; stored %d distinct jobs",
                        self.company, total, len(seen))
        return FetchResult(out)

    def page(self, data):
        result = data.get("refineSearch") if isinstance(data, dict) else None
        rows = ((result.get("data") or {}).get("jobs") if isinstance(result, dict) else None)
        if not isinstance(rows, list):
            raise AdapterError(f"{self.company}: Phenom response has no refineSearch jobs list")
        total = result.get("totalHits")
        if not isinstance(total, int):
            raise AdapterError(f"{self.company}: Phenom response has no numeric totalHits")
        return result, rows, total

    def payload(self, offset, partition=None):
        locations = as_list(self.cfg.get("locations"))
        selected = {"country": locations} if locations else {}
        if partition is not None:
            # String cursors are from the original category-only format.
            field = partition.get("field") if isinstance(partition, dict) else "category"
            value = partition.get("value") if isinstance(partition, dict) else partition
            selected[field] = [value]
        partition_fields = as_list(self.cfg.get("partition_fields"), ["category"])
        return {
            "lang": self.cfg.get("lang") or "en_global",
            "deviceType": "desktop",
            "country": self.cfg.get("country") or "global",
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "",
            "subsearch": "",
            "from": offset,
            "jobs": True,
            "counts": True,
            "all_fields": list(dict.fromkeys(["category", "country", "city", *partition_fields])),
            "size": PAGE,
            "clearAll": False,
            "jdsource": "facets",
            "isSliderEnable": False,
            "pageId": "page3",
            "siteType": "external",
            "keywords": "",
            "global": True,
            "selected_fields": selected,
            "locationData": {},
        }

    def posting(self, raw):
        job_id = str(raw.get("jobId") or raw.get("reqId") or "")
        title = clean(raw.get("title"))
        if not job_id or not title:
            raise AdapterError(f"{self.company}: Phenom job is missing jobId or title")
        location = clean(raw.get("location") or raw.get("cityStateCountry") or raw.get("cityState"))
        if not location:
            location = ", ".join(dict.fromkeys(clean(raw.get(k)) for k in ("city", "state", "country")
                                                if clean(raw.get(k))))
        prefix = str(self.cfg.get("url_prefix") or "global/en").strip("/")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=title,
            location=location,
            remote="remote" in location.lower(),
            posted_at=str(raw.get("postedDate") or raw.get("dateCreated") or ""),
            url=f"https://{self.cfg['host']}/{prefix}/job/{quote(job_id, safe='')}/{slugify(title)}",
            raw={k: raw.get(k) for k in ("jobId", "reqId", "title", "location", "city", "state",
                                           "country", "postedDate") if raw.get(k) is not None},
            req_id=str(raw.get("reqId") or job_id),
        )
