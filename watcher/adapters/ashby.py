from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, join_locations

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyAdapter(Adapter):
    ats = "ashby"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(API.format(token=self.cfg["token"]), cache=ctx.cache)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise AdapterError(f"{self.company}: missing Ashby jobs array")
        out, ids = [], set()
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or j["id"] in ids or not j.get("title"):
                raise AdapterError(f"{self.company}: missing/duplicate Ashby identity/title")
            ids.add(j["id"])
            if j.get("isListed") is False:
                continue
            secondary = [s.get("location") for s in j.get("secondaryLocations") or []]
            location = join_locations(j.get("location"), secondary)
            if not location or not (j.get("jobUrl") or j.get("applyUrl")):
                raise AdapterError(f"{self.company}: missing Ashby location/link")
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
