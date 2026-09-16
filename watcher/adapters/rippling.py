"""Rippling ATS — the job board Rippling sells, and runs its own hiring on.

`GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs`

Multi-tenant (`ats.rippling.com/{token}/jobs` is the public board), keyless, and
the whole board comes back as a bare JSON array. Verified 2026-09-16 against
`rippling`: 647 jobs, 90 of them in Bangalore.

Per job there are only five fields: `uuid`, `name`, `department{id,label}`,
`url`, `workLocation{label,id}`. Notably **no posted date and no requisition
number** — so alert ranking falls back to first_seen for these, and there is no
req ID to quote in a referral, just the posting URL.

**One row per (job, location).** Rippling's 647 rows are 334 distinct `uuid`s;
a multi-location posting repeats, once per city. Keeping the first row and
dropping the rest silently loses locations — on the day this was written, one
posting whose first row was a US state also had a Bangalore row. Rows are merged
by `uuid` with their locations joined instead.

No ETag either (`cache-control: no-store`), so every poll transfers the full
list; it is one request, so this stays on the default 15-minute cadence.
"""

from .base import Adapter, FetchContext, FetchResult, Posting, join_locations

API = "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs"
VIEW = "https://ats.rippling.com/{token}/jobs/{uuid}"


class RipplingAdapter(Adapter):
    ats = "rippling"
    required = ("token",)

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        data = self.http.get_json(API.format(token=self.cfg["token"]))
        out, by_uuid = [], {}
        for j in data:
            location = ((j.get("workLocation") or {}).get("label") or "").strip()
            seen = by_uuid.get(j["uuid"])
            if seen is not None:
                seen.location = join_locations(seen.location.split(" | "), location)
                seen.remote = seen.remote or "remote" in location.lower()
                seen.raw.setdefault("_locations", []).append(location)
                continue
            p = Posting(
                company=self.company,
                ats=self.ats,
                job_id=j["uuid"],
                title=(j.get("name") or "").strip(),
                location=location,
                remote="remote" in location.lower(),
                posted_at="",              # the board does not publish one
                url=j.get("url") or VIEW.format(token=self.cfg["token"], uuid=j["uuid"]),
                raw=dict(j, _locations=[location]),
            )
            by_uuid[j["uuid"]] = p
            out.append(p)
        return FetchResult(out)
