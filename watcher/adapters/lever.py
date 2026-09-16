from datetime import datetime, timezone

from .base import Adapter, FetchContext, FetchResult, Posting, join_locations

API = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverAdapter(Adapter):
    ats = "lever"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        # Without `limit`, Lever returns the whole board in one response.
        data = self.http.get_json(API.format(token=self.cfg["token"]), cache=ctx.cache)
        out = []
        for p in data:
            cats = p.get("categories") or {}
            location = join_locations(cats.get("location"), cats.get("allLocations") or [])
            created = p.get("createdAt")
            posted = (
                datetime.fromtimestamp(created / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                if created
                else ""
            )
            out.append(
                Posting(
                    company=self.company,
                    ats=self.ats,
                    job_id=p["id"],
                    title=(p.get("text") or "").strip(),
                    location=location,
                    remote=p.get("workplaceType") == "remote" or "remote" in location.lower(),
                    posted_at=posted,
                    url=p.get("hostedUrl") or p.get("applyUrl") or "",
                    raw=p,
                )
            )
        return FetchResult(out)
