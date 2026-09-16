"""Atlassian's own careers endpoint (their iCIMS board, published as JSON).

Atlassian runs iCIMS (globalcareers-atlassian.icims.com), and iCIMS itself has
nothing queryable here — `/jobs/search` answers HTML whatever you ask for, and
`?format=json` returns the same 470KB page. But Atlassian publishes the whole
board as JSON on their own domain, which is what their careers page reads:

`GET https://www.atlassian.com/endpoint/careers/listings`

Verified 2026-09-16: one request, 285 listings, ETag honoured (→ 304).
Per listing: `id` (the iCIMS job id, also the req number), `title`,
`locations[]`, `category`, `applyUrl`, and `portalJobPost.portalUrl` /
`portalJobPost.updatedDate`.

Single-company by nature: the path is Atlassian's, not a product. If another
company turns out to expose the same shape, generalise then, not now.
"""

from .base import Adapter, FetchContext, FetchResult, Posting, join_locations

API = "https://www.atlassian.com/endpoint/careers/listings"


class AtlassianAdapter(Adapter):
    ats = "atlassian"

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(API, cache=ctx.cache)
        out = []
        for j in data:
            post = j.get("portalJobPost") or {}
            location = join_locations(j.get("locations") or [])
            out.append(
                Posting(
                    company=self.company,
                    ats=self.ats,
                    job_id=str(j.get("id") or post.get("id")),
                    title=(j.get("title") or "").strip(),
                    location=location,
                    remote="remote" in location.lower(),
                    posted_at=(post.get("updatedDate") or "").split(" ")[0],
                    url=post.get("portalUrl") or j.get("applyUrl") or "",
                    raw=j,
                    req_id=str(j.get("id") or ""),
                )
            )
        return FetchResult(out)
