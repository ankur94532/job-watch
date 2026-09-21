"""Navi India's first-party careers feed, not the unrelated Ashby startup.

The public careers page consumes this complete JSON response and paginates in
the browser. Application links point to TurboHire, but we never fetch that
robots-disallowed host. Counts and company identity must validate before any
posting can be marked closed.
"""

import json
from urllib.parse import urlsplit

from .base import AdapterError, FetchContext, FetchResult, Posting
from .embedded import RobotsAware


class NaviAdapter(RobotsAware):
    ats = "navi"

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        url = "https://navi.com/api/careers/jobs"
        self.check_robots(url)
        data = self.http.get_json(url, cache=ctx.cache)
        if not isinstance(data, dict) or not isinstance(data.get("Jobs"), list):
            raise AdapterError(f"{self.company}: missing Navi Jobs array")
        jobs = data["Jobs"]
        if any(type(data.get(k)) is not int or data[k] != len(jobs)
               for k in ("Total", "FilteredCount")):
            raise AdapterError(f"{self.company}: incomplete Navi job list")
        out, seen = [], set()
        for row in jobs:
            if urlsplit(row.get("CompanyUrl") or "").hostname not in ("navi.com", "www.navi.com"):
                raise AdapterError(f"{self.company}: wrong company in Navi response")
            job_id, title, apply = row.get("JobId"), row.get("JobTitle"), row.get("ApplyUrl") or ""
            if not job_id or job_id in seen or not title:
                raise AdapterError(f"{self.company}: missing/duplicate Navi job identity")
            parsed = urlsplit(apply)
            if parsed.scheme != "https" or parsed.hostname != "navi.turbohire.co" or parsed.path != f"/job/publicjobs/{job_id}":
                raise AdapterError(f"{self.company}: unexpected Navi application URL")
            try:
                locations = json.loads(row.get("Location") or "null")
                if not isinstance(locations, list) or not locations:
                    raise ValueError("missing locations")
                names = [loc["Address"].strip() for loc in locations]
                if not all(names):
                    raise ValueError("empty location")
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                raise AdapterError(f"{self.company}: invalid Navi locations for {job_id}") from e
            location = " | ".join(dict.fromkeys(names))
            seen.add(job_id)
            out.append(Posting(company=self.company, ats=self.ats, job_id=job_id,
                               title=title, location=location, remote="remote" in location.lower(),
                               posted_at=(row.get("PublishedDate") or "")[:10], url=apply,
                               req_id=row.get("JobCode") or ""))
        return FetchResult(out)
