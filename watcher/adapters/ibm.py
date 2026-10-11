"""IBM's first-party careers search service, country-filtered and paged.

This read the whole India inventory in one request until 2026-10-11, when it
stopped completing: India had grown past the service's own ceiling. The API
caps a response at 1,000 rows however large `nr` is (asking for 2,000 still
returns 1,000), so a single request can no longer be complete, and the old
`numresults == totalresults` check failed closed every hour for five days.

`fr` is the row offset and `startindex` echoes it back, so the sweep pages:
1,253 India jobs come back as 1,000 + 253 with no overlap. Each page is
checked against the declared total, the echoed offset and the ids already
seen, so a shifting result window fails loudly rather than silently losing
rows. Requesting only the four metadata fields keeps 1,253 rows near 1 MB
rather than 9.5 MB.
"""

import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

API = "https://www-api.ibm.com/search/api/v1/ibmcom/appid/careers/responseFormat/json"
PAGE = 1000               # the service's own ceiling: a larger `nr` is truncated, not honoured
MAX_PAGES = 20
METADATA = "field_keyword_05,field_keyword_19,field_keyword_17,dcdate"
ROW_ID = re.compile(r"[0-9a-f]{64}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class IBMAdapter(RobotsAware):
    ats = "ibm"
    paginated = True

    def url(self, offset=0):
        return API + "?" + urlencode({
            "scope": "careers2",
            "rmdt": METADATA,
            "appid": "careers",
            "sortby": "-dcdate",
            "nr": PAGE,
            "fr": offset,
            "page": offset // PAGE + 1,
            "query": "",
            "filter": "field_keyword_05:India",
        })

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        offset, total = cur.get("offset", 0), cur.get("total")
        ids = set(cur.get("ids", []))
        if (type(offset) is not int or offset < 0 or offset % PAGE
                or (offset and (type(total) is not int or len(ids) != offset))):
            raise AdapterError(f"{self.company}: invalid IBM cursor")
        out, pages = [], 0
        self.check_robots(self.url(offset))

        while True:
            if pages:
                self.pause()
            # Only the first page of a sweep may revalidate: a 304 on a later
            # page would silently end the sweep short of the declared total.
            data = self.http.get_json(self.url(offset),
                                      cache=ctx.cache if offset == 0 else None)
            pages += 1
            result = data.get("resultset") if isinstance(data, dict) else None
            query = result.get("searchquery") if isinstance(result, dict) else None
            search = result.get("searchresults") if isinstance(result, dict) else None
            if (not isinstance(query, dict) or query.get("appid") != "careers"
                    or query.get("collection") != "ibmcom"
                    or query.get("userquery") != ""
                    or query.get("filter") != "field_keyword_05:India"
                    or not isinstance(query.get("sources"), str) or not query["sources"]
                    or not isinstance(search, dict)
                    or type(search.get("totalresults")) is not int
                    or search["totalresults"] < 0
                    or search.get("startindex") != offset
                    or not isinstance(search.get("searchresultlist"), list)
                    or search.get("numresults") != len(search["searchresultlist"])):
                raise AdapterError(f"{self.company}: incomplete or mismatched IBM search result")

            count = search["totalresults"]
            # The window moved under us: the remaining offsets no longer mean
            # what they did, so start again rather than stitch two inventories.
            if total is not None and count != total:
                return FetchResult([], complete=False, restart=True)
            total = count
            rows = [self._posting(raw, number) for number, raw
                    in enumerate(search["searchresultlist"])]
            if len(rows) != min(PAGE, total - len(ids)):
                raise AdapterError(
                    f"{self.company}: incomplete IBM page at {offset}/{total}")
            keys = [row.job_id for row in rows]
            raw_ids = [row.raw["search_id"] for row in rows]
            if len(keys) != len(set(keys)) or len(raw_ids) != len(set(raw_ids)):
                raise AdapterError(f"{self.company}: duplicate IBM search identity")
            if ids.intersection(keys):
                return FetchResult([], complete=False, restart=True)
            ids.update(keys)
            out.extend(rows)
            offset += len(rows)
            if len(ids) >= total:
                return FetchResult(out, declared=total)
            if pages >= MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {offset}/{total}")
            if ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                return FetchResult(out, complete=False,
                                   cursor={"offset": offset, "total": total, "ids": sorted(ids)})

    def _posting(self, raw, number):
        if not isinstance(raw, dict) or raw.get("resultnum") != number:
            raise AdapterError(f"{self.company}: invalid IBM result sequence")
        search_id, title, url = (raw.get(key) for key in ("id", "title", "url"))
        if (not isinstance(search_id, str) or not ROW_ID.fullmatch(search_id)
                or not isinstance(title, str) or not title.strip()
                or not isinstance(url, str)):
            raise AdapterError(f"{self.company}: IBM job is missing identity/title/url")
        attributes = raw.get("docattributes")
        if not isinstance(attributes, list) or any(not isinstance(item, dict)
                                                   for item in attributes):
            raise AdapterError(f"{self.company}: invalid IBM job metadata")

        def one(key):
            values = [item[key] for item in attributes if key in item]
            if len(values) != 1 or not isinstance(values[0], str):
                raise AdapterError(f"{self.company}: missing/duplicate IBM {key}")
            return values[0].strip()

        country = one("field_keyword_05")
        location = one("field_keyword_19")
        workstyle = one("field_keyword_17")
        posted = one("dcdate")
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        job_ids = query.get("jobId") or []
        if (country != "India" or not location or not DATE.fullmatch(posted)
                or parts.scheme != "https" or parts.netloc != "careers.ibm.com"
                or parts.path != "/careers/JobDetail" or set(query) != {"jobId"}
                or len(job_ids) != 1 or not job_ids[0].isdigit()):
            raise AdapterError(f"{self.company}: mismatched IBM country/location/job URL")
        job_id = job_ids[0]
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=job_id,
            title=title.strip(),
            location=join_locations(location, country),
            remote="remote" in workstyle.lower(),
            posted_at=posted,
            url=url,
            raw={"search_id": search_id, "country": country, "location": location,
                 "workstyle": workstyle},
            req_id=job_id,
        )
