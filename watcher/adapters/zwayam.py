"""The read-only search used by public Zwayam careers sites.

The frontend posts multipart fields, not JSON. Persist validated page progress;
only a stable total, unique IDs and consistent hasMoreData can finish a sweep.
"""

import base64
import json
import re
import time

from ..http import HttpStatusError
from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware

API = "https://public.zwayam.com/jobs/search"


class ZwayamAdapter(RobotsAware):
    ats = "zwayam"
    required = ("company_id", "host", "board_path")
    paginated = True

    def search(self, offset):
        fields = {
            "companyId": base64.b64encode(str(self.cfg["company_id"]).encode()).decode(),
            "domain": self.cfg["host"],
            "filterCri": json.dumps({"paginationStartNo": offset, "selectedCall": "sort",
                                     "sortCriteria": {"name": "modifiedDate", "isAscending": False},
                                     "anyOfTheseWords": ""}),
        }
        boundary = "jobwatch-public-search"
        body = "".join('--' + boundary + '\r\nContent-Disposition: form-data; name="' + k +
                       '"\r\n\r\n' + v + '\r\n' for k, v in fields.items())
        body += '--' + boundary + '--\r\n'
        raw, _ = self.http.request("POST", API, data=body.encode(), headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise AdapterError(f"{self.company}: invalid Zwayam JSON") from e
        if not isinstance(data, dict) or data.get("code") != 200:
            raise AdapterError(f"{self.company}: unsuccessful Zwayam search")
        return data.get("data")

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        self.check_robots(API)
        cur = ctx.cursor or {}
        ids = set(cur.get("ids", []))
        offset, total = cur.get("offset", 0), cur.get("total")
        if len(ids) != offset:
            raise AdapterError(f"{self.company}: invalid Zwayam resume count")
        out, pages = [], 0

        def cursor():
            return {"offset": offset, "total": total, "ids": sorted(ids)}

        while offset < 10000:
            if pages and ((ctx.deadline is not None and time.monotonic() >= ctx.deadline)
                          or pages >= int(self.cfg.get("max_pages_per_run", 12))):
                return FetchResult(out, complete=False, cursor=cursor())
            if pages:
                self.pause()
            try:
                data = self.search(offset)
            except HttpStatusError as e:
                if e.code in (403, 429):
                    return self.partial_on_throttle(e, out, cursor())
                raise
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise AdapterError(f"{self.company}: missing Zwayam data array")
            count, more = data.get("totalCount"), data.get("hasMoreData")
            if type(count) is not int or count < 0 or type(more) is not bool:
                raise AdapterError(f"{self.company}: missing Zwayam pagination metadata")
            if total is not None and total != count:
                # Restart from zero next run; never close against a moving list.
                return FetchResult([], complete=False, restart=True)
            total = count
            page = [self.posting(row) for row in data["data"]]
            keys = [p.job_id for p in page]
            if len(set(keys)) != len(keys) or ids.intersection(keys):
                return FetchResult([], complete=False, restart=True)
            end = offset + len(page)
            if end > total or more != (end < total) or (not page and end < total):
                raise AdapterError(f"{self.company}: incomplete Zwayam page at {offset}/{total}")
            ids.update(keys)
            out.extend(page)
            offset, pages = end, pages + 1
            if not more:
                return FetchResult(out)
        raise AdapterError(f"{self.company}: Zwayam safety cap exceeded")

    def posting(self, row):
        j = row.get("_source") if isinstance(row, dict) else None
        if not isinstance(j, dict) or str(j.get("companyId")) != str(self.cfg["company_id"]):
            raise AdapterError(f"{self.company}: wrong Zwayam company")
        job_id, title, slug = str(j.get("id") or ""), j.get("jobTitle"), j.get("jobUrl") or ""
        if not job_id.isdigit() or not title or not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            raise AdapterError(f"{self.company}: missing Zwayam identity/title/link")
        if j.get("displayStatus") != "Open":
            raise AdapterError(f"{self.company}: unexpected public Zwayam display status")
        locations = [j.get("location")]
        for loc in j.get("jobLocationRecord") or []:
            locations.extend(loc.get(k) for k in ("formattedLocation", "city", "state", "country"))
        location = join_locations(locations)
        if not location:
            raise AdapterError(f"{self.company}: missing Zwayam location")
        # Modified date is search order, not publication date.
        created = j.get("jobCreatedDate") or j.get("createdDate")
        posted = time.strftime("%Y-%m-%d", time.gmtime(created / 1000)) if isinstance(created, (int, float)) else ""
        return Posting(self.company, self.ats, job_id, title.strip(), location,
                       False, posted, "https://" + self.cfg["host"] + "/" +
                       self.cfg["board_path"].strip("/") + "/jobview/" + slug,
                       req_id=str(j.get("refNumber") or j.get("referenceNumber") or ""))
