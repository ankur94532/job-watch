from .base import Adapter, FetchContext, FetchResult, Posting, join_locations

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyAdapter(Adapter):
    ats = "ashby"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(API.format(token=self.cfg["token"]), cache=ctx.cache)
        out = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            secondary = [s.get("location") for s in j.get("secondaryLocations") or []]
            location = join_locations(j.get("location"), secondary)
            out.append(
                Posting(
                    company=self.company,
                    ats=self.ats,
                    job_id=j["id"],
                    title=(j.get("title") or "").strip(),
                    location=location,
                    remote=bool(j.get("isRemote")),
                    posted_at=j.get("publishedAt") or "",
                    url=j.get("jobUrl") or j.get("applyUrl") or "",
                    raw=j,
                )
            )
        return FetchResult(out)
