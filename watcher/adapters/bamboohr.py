"""Public BambooHR careers JSON, never the robots-disallowed embed feed."""

from .base import AdapterError, FetchContext, FetchResult, Posting, join_locations
from .embedded import RobotsAware


class BambooHRAdapter(RobotsAware):
    ats = "bamboohr"
    required = ("host",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        base = "https://" + self.cfg["host"] + "/careers"
        self.check_robots(base + "/list")
        data = self.http.get_json(base + "/list", cache=ctx.cache)
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise AdapterError(f"{self.company}: missing BambooHR result array")
        rows = data["result"]
        total = (data.get("meta") or {}).get("totalCount")
        if type(total) is not int or total != len(rows):
            raise AdapterError(f"{self.company}: incomplete BambooHR list")
        out, ids = [], set()
        for row in rows:
            job_id = str(row.get("id") or "")
            title = (row.get("jobOpeningName") or "").strip()
            places = []
            for field in ("location", "atsLocation"):
                loc = row.get(field) or {}
                if not isinstance(loc, dict):
                    raise AdapterError(f"{self.company}: invalid BambooHR location")
                places.extend(loc.get(k) for k in ("city", "state", "province", "country"))
            location = join_locations(places)
            if not job_id.isdigit() or job_id in ids or not title or not location:
                raise AdapterError(f"{self.company}: missing/duplicate BambooHR identity or location")
            ids.add(job_id)
            # Remote is a work arrangement, not worldwide eligibility. Keep
            # geographic restrictions and never append a generic remote label.
            out.append(Posting(self.company, self.ats, job_id, title, location,
                               row.get("isRemote") is True, "", base + "/" + job_id))
        return FetchResult(out)
