"""Spire2Grow ("ies") — the ATS behind Myntra's board, found by tracing the page.

Nothing on `jobs.myntra.com` names it: the page is a client-rendered SPA, the
served HTML has no ATS fingerprint, and its bundles carry no URL literal for the
listings. `watcher trace` drove a browser once and caught the XHR:

`GET https://io.spire2grow.com/ies/v1/p/requisition/_search`
`  ?page=N&size=50&selectedSortOrder=desc&selectedSortField=postedOn`
`  header workspaceid: MYNTRA-93as3`

Verified 2026-09-18: HTTP 200, `total` 59, rows under `entities`.

* **`workspaceid` is the whole auth story.** Without it the API answers 401; with
  it, no cookie, token or session is needed. The browser also sends a
  `workflowid` that looks session-scoped — it is not checked at all (a bogus
  value returns the same 59 rows), so it is not sent.
* Per row: `id`, `displayId` (the number the recruiter quotes), `jobTitle`,
  `jobLocation[].fqLocationName` ("Bangalore, Karnataka, India"),
  `jobPosting.startDate` (epoch ms), `departmentName`, `jobType`.
* **No deep link.** The board is a single-route SPA: every path returns the same
  shell, the cards are not anchors, and the bundle defines no job route. Rows
  therefore link to the board itself and carry `displayId` as the requisition
  number — enough to find the posting once there. If a real permalink shape
  turns up, only `_posting` changes.
* Multi-tenant by construction: another Spire2Grow customer is a `workspace_id`
  and a `board_url` away.
"""

import time

from .base import Adapter, AdapterError, FetchContext, FetchResult, Posting, as_list, join_locations

API = ("https://io.spire2grow.com/ies/v1/p/requisition/_search"
       "?page={page}&size={size}&selectedSortOrder=desc&selectedSortField=postedOn")
PAGE = 50
MAX_PAGES = 40


class Spire2GrowAdapter(Adapter):
    ats = "spire2grow"
    required = ("workspace_id", "board_url")
    paginated = True

    def headers(self):
        return {
            "content-type": "application/json",
            "language": "en",
            "referer": self.cfg["board_url"],
            "workspaceid": self.cfg["workspace_id"],
        }

    def fetch(self, ctx=None):
        ctx = ctx or FetchContext()
        cur = ctx.cursor or {}
        page = cur.get("page", 1)
        ids = set(cur.get("ids", []))
        out, total, requests = [], cur.get("total"), 0

        while True:
            if requests and ctx.deadline is not None and time.monotonic() >= ctx.deadline:
                return FetchResult(out, complete=False,
                                   cursor={"page": page, "total": total, "ids": sorted(ids)})
            if requests:
                self.pause()
            data = self.http.get_json(API.format(page=page, size=PAGE), headers=self.headers())
            requests += 1
            rows = data.get("entities")
            if rows is None:
                raise AdapterError(f"{self.company}: response has no 'entities' — API shape changed?")
            if total is None:
                total = data.get("total")
            for raw in rows:
                p = self._posting(raw)
                if p.job_id not in ids:
                    ids.add(p.job_id)
                    out.append(p)
            if not rows or (total is not None and len(ids) >= total):
                break
            page += 1
            if page > MAX_PAGES:
                raise AdapterError(f"{self.company}: hit {MAX_PAGES}-page cap at {len(ids)}/{total}")
        return FetchResult(out)

    def _posting(self, raw):
        places = []
        for loc in as_list(raw.get("jobLocation") or []):
            if isinstance(loc, dict):
                places.append(loc.get("fqLocationName") or
                              ", ".join(x for x in (loc.get("city"), loc.get("country")) if x))
        posting = raw.get("jobPosting") or {}
        started = posting.get("startDate")
        return Posting(
            company=self.company,
            ats=self.ats,
            job_id=str(raw.get("id")),
            title=(raw.get("jobTitle") or "").strip(),
            location=join_locations(places),
            remote="remote" in (raw.get("jobType") or "").lower(),
            posted_at=time.strftime("%Y-%m-%d", time.gmtime(started / 1000)) if started else "",
            url=self.cfg["board_url"],          # see the module docstring: no permalink exists
            raw={k: v for k, v in raw.items() if k not in ("jobDescription", "aboutCompany")},
            req_id=str(raw.get("displayId") or ""),
        )
