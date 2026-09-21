"""McKinsey's anonymous, counted first-party careers search.

Fetch the global inventory in one response to avoid changing page boundaries.
If it outgrows the verified 800-row request, fail closed for review. Dates in
this feed describe LinkedIn publication, so they are not used as posting dates.
"""

import re
from urllib.parse import parse_qs, urlsplit

from .base import AdapterError, FetchResult, Posting, join_locations
from .embedded import RobotsAware

URL = ("https://gateway.mckinsey.com/apigw-x0cceuow60/v1/api/jobs/search"
       "?pageSize=800&start=1&lang=en")


class McKinseyAdapter(RobotsAware):
    ats = "mckinsey"

    def fetch(self, ctx=None):
        if ctx and ctx.cursor:
            raise AdapterError(f"{self.company}: unexpected McKinsey cursor")
        self.check_robots(URL)
        payload = self.http.get_json(URL)
        if (not isinstance(payload, dict) or payload.get("httpStatus") != "OK"
                or not isinstance(payload.get("docs"), list)
                or type(payload.get("numFound")) is not int
                or not 0 <= payload["numFound"] <= 800
                or payload["numFound"] != len(payload["docs"])):
            raise AdapterError(f"{self.company}: incomplete/invalid McKinsey inventory")
        rows = [self._posting(row) for row in payload["docs"]]
        if len({p.job_id for p in rows}) != len(rows):
            raise AdapterError(f"{self.company}: duplicate McKinsey identity")
        return FetchResult(rows)

    def _posting(self, row):
        if not isinstance(row, dict):
            raise AdapterError(f"{self.company}: invalid McKinsey job")
        job_id, title, slug, apply = (row.get(k) for k in
                                     ("jobID", "title", "friendlyURL", "jobApplyURL"))
        if (not isinstance(job_id, str) or not re.fullmatch(r"[0-9]+", job_id)
                or not isinstance(title, str) or not title.strip()
                or not isinstance(slug, str)
                or not re.fullmatch(r"[a-zA-Z0-9-]+-" + job_id, slug)
                or not isinstance(apply, str)):
            raise AdapterError(f"{self.company}: missing McKinsey identity/title/link")
        parts = urlsplit(apply)
        if (parts.scheme != "https" or parts.netloc != "mckinsey.avature.net"
                or parts.path != "/careers/ApplicationMethods"
                or parse_qs(parts.query).get("folderId") != [job_id]):
            raise AdapterError(f"{self.company}: mismatched McKinsey application identity")
        cities, countries = row.get("cities"), row.get("countries")
        if (not isinstance(cities, list) or not cities
                or not isinstance(countries, list) or len(cities) != len(countries)
                or any(not isinstance(v, str) or not v.strip() for v in cities + countries)):
            raise AdapterError(f"{self.company}: invalid McKinsey location alignment")
        location = join_locations([f"{city.strip()}, {country.strip()}"
                                   for city, country in zip(cities, countries)])
        return Posting(self.company, self.ats, job_id, title.strip(), location,
                       False, "", "https://www.mckinsey.com/careers/search-jobs/jobs/" + slug,
                       req_id=job_id)
