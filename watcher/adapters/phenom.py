"""Phenom CareerConnect's public, zero-auth job-search widget.

Branded Phenom sites expose ``POST /widgets``.  Unlike their sitemap, the
response carries stable job ids, titles, dates, and locations, and accepts a
country facet plus 100-row pagination.  Quest Global is the verified tenant.
"""

import html
import re
import time
from urllib.parse import quote

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, as_list

PAGE = 100
MAX_PAGES = 50
MAX_WINDOW = 1000


def clean(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def slugify(value):
    return re.sub(r"[^A-Za-z0-9]+", "-", clean(value)).strip("-") or "job"


class PhenomAdapter(Adapter):
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
            total = int(cur["total"])
        else:
            pending = self.http.post_json(self.api(), self.payload(0))
            requests = 1
            result, _, total = self.page(pending)
            if total > MAX_WINDOW:
                # Phenom/Elasticsearch caps one result window at 1,000.  Split
                # on the single-valued job category, not city: multi-location
                # jobs appear in several city buckets and cannot be counted
                # back to a complete set reliably.
                values = next((a.get("value") for a in (result.get("data") or {}).get("aggregations") or []
                               if a.get("field") == "category"), None)
                if not isinstance(values, dict) or sum(values.values()) != total \
                        or max(values.values(), default=0) > MAX_WINDOW:
                    raise AdapterError(f"{self.company}: {total} jobs exceed Phenom's {MAX_WINDOW}-row "
                                       "window and category facets cannot partition them safely")
                # Keep the first 1,000 unpartitioned results as well.  Facet
                # aggregations can omit an uncategorized job or double-count a
                # multi-category job; unioning them with the full search window
                # closes that one-row gap while still avoiding offset 1,000.
                partitions = [None, *values]
                counts = [MAX_WINDOW, *values.values()]
            else:
                partitions, counts = [None], [total]
            partition, offset, part_seen, part_ids = 0, 0, 0, set()

        pages = 0
        while partition < len(partitions):
            if pending is None:
                if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                    return FetchResult(out, complete=False, cursor={
                        "partitions": partitions, "partition_counts": counts, "partition": partition,
                        "offset": offset, "part_seen": part_seen, "part_ids": sorted(part_ids),
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
                raise AdapterError(f"{self.company}: Phenom {partitions[partition] or 'board'} count changed "
                                   f"during sweep ({expected_page_total} to {page_total})")

            fresh = page_fresh = 0
            for raw in rows:
                posting = self.posting(raw)
                if posting.job_id not in part_ids:
                    part_ids.add(posting.job_id)
                    page_fresh += 1
                if posting.job_id in seen:
                    continue
                seen.add(posting.job_id)
                out.append(posting)
                fresh += 1
            if len(seen) == total:
                return FetchResult(out)
            if rows and not page_fresh:
                raise AdapterError(f"{self.company}: Phenom ignored offset {offset} and repeated a page")
            offset += len(rows)
            part_seen += len(rows)
            pages += 1
            if pages >= MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {len(seen)}/{total}")
            if not rows or part_seen >= counts[partition] or len(rows) < PAGE:
                if part_seen != counts[partition]:
                    raise AdapterError(f"{self.company}: got {part_seen} of {counts[partition]} Phenom jobs "
                                       f"for {partitions[partition] or 'the board'}")
                partition += 1
                offset = part_seen = 0
                part_ids = set()

        if len(seen) != total:
            raise AdapterError(f"{self.company}: got {len(seen)} unique Phenom jobs but expected {total}")
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

    def payload(self, offset, category=None):
        locations = as_list(self.cfg.get("locations"))
        selected = {"country": locations} if locations else {}
        if category is not None:
            selected["category"] = [category]
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
            "all_fields": ["category", "country", "city"],
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
