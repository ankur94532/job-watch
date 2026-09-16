from .base import Adapter, FetchContext, FetchResult, Posting, join_locations

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseAdapter(Adapter):
    ats = "greenhouse"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(API.format(token=self.cfg["token"]), cache=ctx.cache)
        out = []
        for j in data.get("jobs", []):
            offices = [o.get("name") for o in j.get("offices") or []]
            location = join_locations((j.get("location") or {}).get("name"), offices)
            out.append(
                Posting(
                    company=self.company,
                    ats=self.ats,
                    job_id=str(j["id"]),
                    title=(j.get("title") or "").strip(),
                    location=location,
                    remote="remote" in location.lower(),
                    posted_at=j.get("first_published") or j.get("updated_at") or "",
                    url=j.get("absolute_url") or "",
                    raw=j,
                    req_id=str(j.get("requisition_id") or ""),
                )
            )
        return FetchResult(out)
